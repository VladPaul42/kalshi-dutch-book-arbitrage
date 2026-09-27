"""Read-only loading, caching and aggregation of the pipeline's candidates files.

This module is the entire read side of the dashboard. It opens
``candidates_*.json`` files produced by the research pipeline, parses them once,
and precomputes everything the UI needs. It never writes to the candidates
directory and never imports from the pipeline package: the JSON schema is the
only contract between the two halves of the project.

Prices and quantities in those files are decimal *strings* in cents/contracts
(e.g. ``"706.15"``). They are parsed with :class:`~decimal.Decimal` and never go
through ``float``, because comparing and summing order-book quantities as
binary floats silently loses the exact values the pipeline took care to emit.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("dashboard.data")

RUN_GLOB = "candidates_*.json"

# A run id is the file's stem. The character class alone already excludes path
# separators and a bare "..", but resolve_run_path() re-checks the resolved
# parent anyway so a symlink cannot point outside the configured directory.
_RUN_ID_RE = re.compile(r"^candidates_[A-Za-z0-9._-]+$")

# Full parsed runs are heavy (a 477-event file is ~500 KB of JSON), so only a
# few are kept. Summaries are tiny, so /api/runs stays cheap even with a long
# history of run files.
_MAX_CACHED_RUNS = 4
_MAX_CACHED_SUMMARIES = 256

# Errors that mean "this file is not a usable run" rather than "the server is
# broken": truncated writes, wrong encoding, or a shape we cannot read.
_UNUSABLE = (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError)

SORT_FIELDS = (
    "sell_yes_net",
    "buy_no_net",
    "buy_yes_net",
    "sell_no_net",
    "max_volume",
    "max_open_interest",
    "max_bundles",
    "market_count",
)

# Four mutually exclusive basket routes, in the order used to break ties for
# "best". Each is a full round of the same trade on every leg, top of book only.
# NO prices are never inferred from YES. A route is unpriced unless every leg
# is hittable: price in (0, 100) and size > 0. 0 and 100 are empty-book
# sentinels, not tradable quotes.
ROUTE_IDS = ("buy_no", "buy_yes", "sell_yes", "sell_no")
ROUTE_SIZE_FIELDS = {
    "sell_yes": "yes_bid_size_contracts",
    "buy_no": "no_ask_size_contracts",
    "buy_yes": "yes_ask_size_contracts",
    "sell_no": "no_bid_size_contracts",
}

# --- the trades being modelled -------------------------------------------
#
# On a mutually exclusive event, at most one leg settles YES (pays 100¢). Four
# top-of-book baskets can be priced from that:
#
#   sell YES @ yes_bid every leg   gross = Σ yes_bid − 100
#   buy  NO  @ no_ask  every leg   gross = (N−1)×100 − Σ no_ask
#   buy  YES @ yes_ask every leg   gross = 100 − Σ yes_ask
#   sell NO  @ no_bid  every leg   gross = Σ no_bid − (N−1)×100
#
# Kalshi's published taker fee, in dollars:
#
#     fee = ceil_to_next_cent(M * 0.07 * C * P * (1 - P))
#
# with P the price in DOLLARS of the contract actually traded (the bid you hit
# or the ask you lift), C the contract count and M the series multiplier. The
# rounding up happens once per ORDER, not per contract, which is why the same
# trade costs less per bundle at size and why two net edge figures exist rather
# than one.
#
# These are ASSUMPTIONS and are surfaced as such through the API. Kalshi sets
# series-specific multipliers and charges maker fees on some series (sports, CPI,
# Fed and similar); neither is modelled here, and the trade is assumed to cross
# the spread on every leg. Check the current fee schedule for a series before
# treating any number derived from these as real.
TAKER_FEE_RATE = Decimal("0.07")
ASSUMED_SERIES_MULTIPLIER = Decimal("1")
ASSUMED_SIDE = "taker on every leg"

CENTS_PER_DOLLAR = Decimal(100)

# What one YES contract pays on settlement, and therefore the line a ladder's bid
# sum has to clear before the basket is worth selling.
SETTLEMENT_CENTS = Decimal(100)

DAYS_PER_YEAR = Decimal(365)

FEE_MODEL = {
    "taker_fee_rate": format(TAKER_FEE_RATE, "f"),
    "assumed_series_multiplier": format(ASSUMED_SERIES_MULTIPLIER, "f"),
    "assumed_side": ASSUMED_SIDE,
    "formula": "fee_dollars = ceil_to_next_cent(M * 0.07 * C * P * (1 - P)), P in dollars",
    "rounding": "up to a whole cent once per order, not per contract",
    "verified_against_fee_schedule": False,
    "ignores": [
        "series-specific multipliers",
        "maker fees (sports, CPI, Fed and similar series)",
        "depth beyond the top of book",
    ],
}

# Figures that come out of a division carry four decimals, matching the price
# quantum the pipeline writes, instead of trailing 28 digits of context noise.
_EDGE_QUANTUM = Decimal("0.0001")
_MONEY_QUANTUM = Decimal("0.01")
_PCT_QUANTUM = Decimal("0.01")
_DAYS_QUANTUM = Decimal("0.01")

_SECONDS_PER_DAY = Decimal(86400)


class RunNotFoundError(Exception):
    """Raised when a run id does not resolve to a readable file in the run directory."""


@dataclass(frozen=True)
class EventRow:
    """One candidate event, flattened for the table plus keys used to sort it.

    ``payload`` is what crosses the API (all numbers as strings); the sort
    fields stay as native Python values so ordering is exact.
    """

    payload: dict[str, Any]
    search_blob: str
    close_time: datetime | None
    market_count: int
    max_volume: Decimal
    max_open_interest: Decimal
    # Unlike the liquidity aggregates above these stay None when unknown rather
    # than collapsing to zero: "no computable edge" and "no edge" rank
    # differently, and only the first should be sorted out of the way.
    best_net_per_bundle: Decimal | None = None
    sell_yes_net: Decimal | None = None
    buy_no_net: Decimal | None = None
    buy_yes_net: Decimal | None = None
    sell_no_net: Decimal | None = None
    # Best-route executable size. None when no route could be priced, so a
    # missing figure sorts last rather than as zero bundles.
    max_bundles: int | None = None
    bid_delta: Decimal | None = None
    ask_delta: Decimal | None = None
    net_edge_at_max_size: Decimal | None = None
    max_profit: Decimal | None = None
    annualized_return: Decimal | None = None


@dataclass(frozen=True)
class ParsedRun:
    """A fully parsed run file: metadata, funnel, table rows and event details."""

    run_id: str
    filename: str
    meta: dict[str, Any]
    gates: dict[str, Any]
    funnel: list[dict[str, Any]]
    rows: list[EventRow]
    details: dict[str, dict[str, Any]] = field(repr=False)

    def summary(self) -> dict[str, Any]:
        """The compact shape used by the run picker."""
        return {
            "id": self.run_id,
            "filename": self.filename,
            "environment": self.meta.get("environment"),
            "filtered_at_utc": self.meta.get("filtered_at_utc"),
            "source_snapshot_captured_at_utc": self.meta.get("source_snapshot_captured_at_utc"),
            "candidate_count": self.meta.get("candidate_count"),
        }


def _dec(raw: Any) -> Decimal | None:
    """Parse a pipeline decimal string, returning None for missing/unusable values.

    Bids, asks and sizes are documented as decimal strings but are allowed to be
    null, and one unreadable leg must not sink a whole run.
    """
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ArithmeticError):
        return None


def _fmt(value: Decimal | None) -> str | None:
    """Render a computed Decimal as a plain (never exponent-notation) string."""
    if value is None:
        return None
    return format(value, "f")


def _parse_utc(raw: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from the pipeline, tolerating null and 'Z'."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive timestamps would break comparisons against aware ones; the pipeline
    # emits UTC everywhere, so assume UTC when the offset is absent.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _extreme(
    markets: Iterable[dict[str, Any]], key: str, *, largest: bool
) -> tuple[Decimal | None, str | None]:
    """Find the min/max of one numeric field across an event's markets.

    Returns the value both as a Decimal (for sorting) and as the *original*
    string from the file, so the UI can show exactly what the pipeline wrote
    instead of a reformatted approximation.
    """
    best: Decimal | None = None
    best_raw: str | None = None
    for market in markets:
        value = _dec(market.get(key))
        if value is None:
            continue
        if best is None or (value > best if largest else value < best):
            best = value
            best_raw = str(market.get(key))
    return best, best_raw


def _taker_fee_cents(price_cents: Decimal, contracts: Decimal) -> Decimal:
    """Estimated taker fee, in whole cents, for ``contracts`` traded at ``price_cents``.

    ``price_cents`` is the price actually traded (the bid you hit or the ask you
    lift). Rounded up to a whole cent once for the whole order, which is what
    makes the per-contract cost fall as the order grows.
    """
    price = price_cents / CENTS_PER_DOLLAR
    fee = (
        ASSUMED_SERIES_MULTIPLIER
        * TAKER_FEE_RATE
        * CENTS_PER_DOLLAR
        * contracts
        * price
        * (Decimal(1) - price)
    )
    return fee.to_integral_value(rounding=ROUND_CEILING)


REASON_NOT_ME = "event is not flagged mutually exclusive"
REASON_MISSING = {
    "sell_yes": "at least one leg has no hittable yes bid (price not in (0, 100) or size ≤ 0)",
    "buy_no": "at least one leg has no hittable no ask (price not in (0, 100) or size ≤ 0)",
    "buy_yes": "at least one leg has no hittable yes ask (price not in (0, 100) or size ≤ 0)",
    "sell_no": "at least one leg has no hittable no bid (price not in (0, 100) or size ≤ 0)",
}


@dataclass(frozen=True)
class RouteQuote:
    """One of the four basket trades, priced or explicitly skipped."""

    route_id: str
    applicable: bool
    reason: str | None
    gross: Decimal | None
    net_per_bundle: Decimal | None
    net_at_max: Decimal | None
    max_bundles: int | None
    bottleneck_index: int | None
    bottleneck_ticker: Any
    max_profit: Decimal | None
    capital_unnetted: Decimal | None
    capital_netted: Decimal | None
    annualized: Decimal | None

    @property
    def net_edge_positive(self) -> bool:
        return self.net_per_bundle is not None and self.net_per_bundle > 0


@dataclass(frozen=True)
class Mispricing:
    """Four-route edge for one event: API payload, per-leg extras and sort keys.

    ``legs`` is positionally aligned with the event's ``markets`` list. Book
    totals live here so the ladder and the routes cannot disagree about sums.
    """

    payload: dict[str, Any]
    legs: list[dict[str, Any]]
    yes_bid_sum: Decimal
    yes_bid_complete: bool
    yes_ask_sum: Decimal
    yes_ask_complete: bool
    no_bid_sum: Decimal
    no_bid_complete: bool
    no_ask_sum: Decimal
    no_ask_complete: bool
    best_net_per_bundle: Decimal | None
    sell_yes_net: Decimal | None
    buy_no_net: Decimal | None
    buy_yes_net: Decimal | None
    sell_no_net: Decimal | None
    bid_delta: Decimal | None
    ask_delta: Decimal | None
    net_edge_at_max_size: Decimal | None
    max_profit: Decimal | None
    annualized_return: Decimal | None


def _book_complete(values: list[Decimal | None]) -> bool:
    return bool(values) and all(value is not None for value in values)


def _route_executable(
    prices: list[Decimal | None], sizes: list[Decimal | None]
) -> bool:
    """True when every leg can actually be hit: price in (0, 100) and size > 0."""
    if not prices or len(prices) != len(sizes):
        return False
    return all(
        price is not None
        and Decimal(0) < price < SETTLEMENT_CENTS
        and size is not None
        and size > 0
        for price, size in zip(prices, sizes)
    )


def _book_sum(values: list[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None), Decimal(0))


def _days_to_close(
    close_time: datetime | None, reference_time: datetime | None
) -> Decimal | None:
    """Days from snapshot time to close. None when either timestamp is missing."""
    if close_time is None or reference_time is None:
        return None
    delta = close_time - reference_time
    # total_seconds() returns a float, which is banned here; timedelta's
    # integer components give the same answer exactly.
    return Decimal(delta.days) + Decimal(delta.seconds) / _SECONDS_PER_DAY


def _bottleneck(sizes: list[Decimal | None]) -> tuple[int | None, int | None]:
    """Whole-contract min size and the index of the first such leg.

    Floored because a fractional bundle is not the trade being modelled, and
    flooring can only understate the size. Missing any size, or a size that
    floors to 0, leaves both None. Callers only quote a route when every size
    is already > 0, so this is the max-size figure, not a second executability
    check.
    """
    present = [size for size in sizes if size is not None]
    if not present or len(present) != len(sizes):
        return None, None
    smallest = min(present)
    whole = int(smallest.to_integral_value(rounding=ROUND_FLOOR))
    if whole <= 0:
        return None, None
    return whole, sizes.index(smallest)


def _size_nets(
    gross: Decimal, prices: list[Decimal], max_bundles: int
) -> tuple[Decimal, Decimal]:
    """Net per bundle at max size, and dollar profit for that many bundles."""
    contracts = Decimal(max_bundles)
    fees_at_size = sum((_taker_fee_cents(price, contracts) for price in prices), Decimal(0))
    net_at_max = (gross - fees_at_size / contracts).quantize(
        _EDGE_QUANTUM, rounding=ROUND_FLOOR
    )
    max_profit = (net_at_max * contracts / CENTS_PER_DOLLAR).quantize(
        _MONEY_QUANTUM, rounding=ROUND_FLOOR
    )
    return net_at_max, max_profit


def _annualized_pct(
    net_per_bundle: Decimal, capital_unnetted: Decimal, days: Decimal | None
) -> Decimal | None:
    """Conservative net over unnetted capital, annualized from snapshot time."""
    if days is None or days <= 0 or capital_unnetted <= 0:
        return None
    return (
        net_per_bundle / capital_unnetted * (DAYS_PER_YEAR / days) * Decimal(100)
    ).quantize(_PCT_QUANTUM, rounding=ROUND_FLOOR)


def _skipped_route(route_id: str, reason: str) -> RouteQuote:
    return RouteQuote(
        route_id=route_id,
        applicable=False,
        reason=reason,
        gross=None,
        net_per_bundle=None,
        net_at_max=None,
        max_bundles=None,
        bottleneck_index=None,
        bottleneck_ticker=None,
        max_profit=None,
        capital_unnetted=None,
        capital_netted=None,
        annualized=None,
    )


def _quote_route(
    *,
    route_id: str,
    prices: list[Decimal | None],
    sizes: list[Decimal | None],
    markets: list[dict[str, Any]],
    gross: Decimal,
    capital_unnetted: Decimal,
    capital_netted: Decimal,
    days: Decimal | None,
) -> RouteQuote:
    """Price one bundle trade at the given leg prices. Caller has already gated."""
    # prices is complete here: every entry is a Decimal.
    traded = [price for price in prices if price is not None]
    fees = sum((_taker_fee_cents(price, Decimal(1)) for price in traded), Decimal(0))
    net_per_bundle = gross - fees
    max_bundles, bottleneck_index = _bottleneck(sizes)
    net_at_max: Decimal | None = None
    max_profit: Decimal | None = None
    bottleneck_ticker: Any = None
    if max_bundles is not None and bottleneck_index is not None:
        net_at_max, max_profit = _size_nets(gross, traded, max_bundles)
        bottleneck_ticker = markets[bottleneck_index].get("ticker")
    return RouteQuote(
        route_id=route_id,
        applicable=True,
        reason=None,
        gross=gross,
        net_per_bundle=net_per_bundle,
        net_at_max=net_at_max,
        max_bundles=max_bundles,
        bottleneck_index=bottleneck_index,
        bottleneck_ticker=bottleneck_ticker,
        max_profit=max_profit,
        capital_unnetted=capital_unnetted,
        capital_netted=capital_netted,
        annualized=_annualized_pct(net_per_bundle, capital_unnetted, days),
    )


def _route_payload(quote: RouteQuote) -> dict[str, Any]:
    """Detail-shaped route object. Gross is for hover; the table shows net."""
    return {
        "applicable": quote.applicable,
        "reason": quote.reason,
        "net_edge_positive": quote.net_edge_positive,
        "gross_cents": _fmt(quote.gross),
        "net_cents_per_bundle": _fmt(quote.net_per_bundle),
        "net_cents_at_max_size": _fmt(quote.net_at_max),
        "max_bundles": quote.max_bundles,
        "bottleneck_ticker": quote.bottleneck_ticker,
        "max_profit_dollars": _fmt(quote.max_profit),
        "capital_per_bundle_unnetted_cents": _fmt(quote.capital_unnetted),
        "capital_per_bundle_netted_cents": _fmt(quote.capital_netted),
        "annualized_return_pct": _fmt(quote.annualized),
    }


def _slim_route_from_payload(route: dict[str, Any]) -> dict[str, Any]:
    """Drop size/capital fields so a table row cannot render them as cells."""
    return {
        "net_cents_per_bundle": route["net_cents_per_bundle"],
        "gross_cents": route["gross_cents"],
        "net_edge_positive": route["net_edge_positive"],
    }


def _pick_best(quotes: dict[str, RouteQuote]) -> str | None:
    """Route id of the highest net-per-bundle; ROUTE_IDS order breaks ties."""
    best_id: str | None = None
    best_net: Decimal | None = None
    for route_id in ROUTE_IDS:
        net = quotes[route_id].net_per_bundle
        if net is None:
            continue
        if best_net is None or net > best_net:
            best_net = net
            best_id = route_id
    return best_id


def _mispricing(event: dict[str, Any], reference_time: datetime | None) -> Mispricing:
    """Price the four mutually exclusive basket trades for one event.

    ``reference_time`` is when the snapshot was taken, not now: the result is
    then a pure function of the run file, which matters because parsed runs are
    cached until the file changes and a wall-clock "now" frozen at parse time
    would quietly go stale.
    """
    markets = event.get("markets") or []
    yes_bids = [_dec(market.get("yes_bid_cents")) for market in markets]
    yes_asks = [_dec(market.get("yes_ask_cents")) for market in markets]
    no_bids = [_dec(market.get("no_bid_cents")) for market in markets]
    no_asks = [_dec(market.get("no_ask_cents")) for market in markets]
    yes_bid_sizes = [_dec(market.get("yes_bid_size_contracts")) for market in markets]
    yes_ask_sizes = [_dec(market.get("yes_ask_size_contracts")) for market in markets]
    no_bid_sizes = [_dec(market.get("no_bid_size_contracts")) for market in markets]
    no_ask_sizes = [_dec(market.get("no_ask_size_contracts")) for market in markets]

    yes_bid_complete = _book_complete(yes_bids)
    yes_ask_complete = _book_complete(yes_asks)
    no_bid_complete = _book_complete(no_bids)
    no_ask_complete = _book_complete(no_asks)
    sell_yes_executable = _route_executable(yes_bids, yes_bid_sizes)
    buy_no_executable = _route_executable(no_asks, no_ask_sizes)
    buy_yes_executable = _route_executable(yes_asks, yes_ask_sizes)
    sell_no_executable = _route_executable(no_bids, no_bid_sizes)
    yes_bid_sum = _book_sum(yes_bids)
    yes_ask_sum = _book_sum(yes_asks)
    no_bid_sum = _book_sum(no_bids)
    no_ask_sum = _book_sum(no_asks)

    n_legs = len(markets)
    others_payout = SETTLEMENT_CENTS * Decimal(max(n_legs - 1, 0))

    close_time = _parse_utc(event.get("close_time"))
    days_raw = _days_to_close(close_time, reference_time)
    days_display = (
        days_raw.quantize(_DAYS_QUANTUM, rounding=ROUND_FLOOR) if days_raw is not None else None
    )

    # Worth showing even when a given route cannot run: a per-leg fee at the
    # YES bid explains what crossing that one spread would cost on its own.
    legs = [
        {
            "est_fee_cents_per_contract": (
                _fmt(_taker_fee_cents(yes_bid, Decimal(1))) if yes_bid is not None else None
            ),
            "is_bottleneck": False,
            "bottleneck_size_field": None,
            "yes_bid_plus_no_ask_cents": (
                _fmt(yes_bid + no_ask)
                if yes_bid is not None and no_ask is not None
                else None
            ),
            "yes_ask_plus_no_bid_cents": (
                _fmt(yes_ask + no_bid)
                if yes_ask is not None and no_bid is not None
                else None
            ),
        }
        for yes_bid, yes_ask, no_bid, no_ask in zip(
            yes_bids, yes_asks, no_bids, no_asks, strict=False
        )
    ]

    mutually_exclusive = event.get("mutually_exclusive") is True
    not_me = None if mutually_exclusive else REASON_NOT_ME

    quotes: dict[str, RouteQuote] = {}

    if not_me or not sell_yes_executable:
        quotes["sell_yes"] = _skipped_route(
            "sell_yes", not_me or REASON_MISSING["sell_yes"]
        )
    else:
        quotes["sell_yes"] = _quote_route(
            route_id="sell_yes",
            prices=yes_bids,
            sizes=yes_bid_sizes,
            markets=markets,
            gross=yes_bid_sum - SETTLEMENT_CENTS,
            # Unnetted is what each short YES costs on its own; netted is what
            # the basket costs if the exchange recognises that only one leg can
            # pay out. It is not established which Kalshi does.
            capital_unnetted=sum(
                (SETTLEMENT_CENTS - bid for bid in yes_bids if bid is not None),
                Decimal(0),
            ),
            capital_netted=max(Decimal(0), SETTLEMENT_CENTS - yes_bid_sum),
            days=days_raw,
        )

    if not_me or not buy_no_executable:
        quotes["buy_no"] = _skipped_route(
            "buy_no", not_me or REASON_MISSING["buy_no"]
        )
    else:
        quotes["buy_no"] = _quote_route(
            route_id="buy_no",
            prices=no_asks,
            sizes=no_ask_sizes,
            markets=markets,
            gross=others_payout - no_ask_sum,
            capital_unnetted=no_ask_sum,
            capital_netted=no_ask_sum,
            days=days_raw,
        )

    if not_me or not buy_yes_executable:
        quotes["buy_yes"] = _skipped_route(
            "buy_yes", not_me or REASON_MISSING["buy_yes"]
        )
    else:
        quotes["buy_yes"] = _quote_route(
            route_id="buy_yes",
            prices=yes_asks,
            sizes=yes_ask_sizes,
            markets=markets,
            gross=SETTLEMENT_CENTS - yes_ask_sum,
            capital_unnetted=yes_ask_sum,
            capital_netted=yes_ask_sum,
            days=days_raw,
        )

    if not_me or not sell_no_executable:
        quotes["sell_no"] = _skipped_route(
            "sell_no", not_me or REASON_MISSING["sell_no"]
        )
    else:
        quotes["sell_no"] = _quote_route(
            route_id="sell_no",
            prices=no_bids,
            sizes=no_bid_sizes,
            markets=markets,
            gross=no_bid_sum - others_payout,
            capital_unnetted=sum(
                (SETTLEMENT_CENTS - bid for bid in no_bids if bid is not None),
                Decimal(0),
            ),
            capital_netted=max(Decimal(0), others_payout - no_bid_sum),
            days=days_raw,
        )

    sell_yes_net = quotes["sell_yes"].net_per_bundle
    buy_no_net = quotes["buy_no"].net_per_bundle
    buy_yes_net = quotes["buy_yes"].net_per_bundle
    sell_no_net = quotes["sell_no"].net_per_bundle
    # A missing side is null, not zero: a delta of 0 would claim the two routes
    # are equal when one of them could not be priced at all.
    bid_delta = (
        sell_yes_net - buy_no_net
        if sell_yes_net is not None and buy_no_net is not None
        else None
    )
    ask_delta = (
        buy_yes_net - sell_no_net
        if buy_yes_net is not None and sell_no_net is not None
        else None
    )

    best_id = _pick_best(quotes)
    best = quotes[best_id] if best_id is not None else None
    if best is not None and best.bottleneck_index is not None:
        legs[best.bottleneck_index]["is_bottleneck"] = True
        legs[best.bottleneck_index]["bottleneck_size_field"] = ROUTE_SIZE_FIELDS[best.route_id]

    applicable = best is not None
    # Decided here rather than in the browser: the comparison is against an
    # exact cent value, and JavaScript would have to parse the string as a
    # binary float to make it.
    net_edge_positive = bool(best and best.net_edge_positive)
    top_reason = None
    if not applicable:
        top_reason = not_me or "no route could be computed"

    payload: dict[str, Any] = {
        "applicable": applicable,
        "reason": top_reason,
        "net_edge_positive": net_edge_positive,
        "routes": {route_id: _route_payload(quotes[route_id]) for route_id in ROUTE_IDS},
        "bid_delta_cents": _fmt(bid_delta),
        "ask_delta_cents": _fmt(ask_delta),
        "bid_delta_legs": {
            "sell_yes_net_cents": _fmt(sell_yes_net),
            "sell_yes_gross_cents": _fmt(quotes["sell_yes"].gross),
            "buy_no_net_cents": _fmt(buy_no_net),
            "buy_no_gross_cents": _fmt(quotes["buy_no"].gross),
        },
        "ask_delta_legs": {
            "buy_yes_net_cents": _fmt(buy_yes_net),
            "buy_yes_gross_cents": _fmt(quotes["buy_yes"].gross),
            "sell_no_net_cents": _fmt(sell_no_net),
            "sell_no_gross_cents": _fmt(quotes["sell_no"].gross),
        },
        "best_route": best_id,
        "best_net_cents_per_bundle": _fmt(best.net_per_bundle) if best else None,
        "best_gross_cents": _fmt(best.gross) if best else None,
        "max_bundles": best.max_bundles if best else None,
        "bottleneck_ticker": best.bottleneck_ticker if best else None,
        "max_profit_dollars": _fmt(best.max_profit) if best else None,
        "annualized_return_pct": _fmt(best.annualized) if best else None,
        "days_to_close": _fmt(days_display),
    }

    return Mispricing(
        payload=payload,
        legs=legs,
        yes_bid_sum=yes_bid_sum,
        yes_bid_complete=yes_bid_complete,
        yes_ask_sum=yes_ask_sum,
        yes_ask_complete=yes_ask_complete,
        no_bid_sum=no_bid_sum,
        no_bid_complete=no_bid_complete,
        no_ask_sum=no_ask_sum,
        no_ask_complete=no_ask_complete,
        best_net_per_bundle=best.net_per_bundle if best else None,
        sell_yes_net=sell_yes_net,
        buy_no_net=buy_no_net,
        buy_yes_net=buy_yes_net,
        sell_no_net=sell_no_net,
        bid_delta=bid_delta,
        ask_delta=ask_delta,
        net_edge_at_max_size=best.net_at_max if best else None,
        max_profit=best.max_profit if best else None,
        annualized_return=best.annualized if best else None,
    )


def _build_row(event: dict[str, Any], mispricing: Mispricing) -> EventRow:
    """Flatten one event into a table row with its liquidity aggregates."""
    markets = event.get("markets") or []
    ticker = str(event.get("event_ticker") or "")
    title = str(event.get("title") or "")

    max_vol, max_vol_raw = _extreme(markets, "volume_24h_contracts", largest=True)
    max_oi, max_oi_raw = _extreme(markets, "open_interest_contracts", largest=True)
    _, min_bid_raw = _extreme(markets, "yes_bid_cents", largest=False)
    _, max_bid_raw = _extreme(markets, "yes_bid_cents", largest=True)

    # The widest bid/ask spread across the ladder's legs: the single number that
    # says whether the whole ladder is quotable, since one untradeable leg is
    # enough to make a mutually exclusive basket unworkable.
    widest: Decimal | None = None
    for market in markets:
        bid = _dec(market.get("yes_bid_cents"))
        ask = _dec(market.get("yes_ask_cents"))
        if bid is None or ask is None:
            continue
        spread = ask - bid
        if widest is None or spread > widest:
            widest = spread

    close_time = _parse_utc(event.get("close_time"))
    market_count = event.get("market_count")
    if not isinstance(market_count, int):
        market_count = len(markets)

    payload = mispricing.payload
    routes = payload["routes"]
    return EventRow(
        payload={
            "event_ticker": ticker,
            "title": title,
            "sub_title": event.get("sub_title"),
            "market_count": market_count,
            "close_time": event.get("close_time"),
            "max_volume_24h_contracts": max_vol_raw,
            "max_open_interest_contracts": max_oi_raw,
            "min_yes_bid_cents": min_bid_raw,
            "max_yes_bid_cents": max_bid_raw,
            "widest_spread_cents": _fmt(widest),
            "net_edge_positive": payload["net_edge_positive"],
            "routes": {
                route_id: _slim_route_from_payload(routes[route_id])
                for route_id in ROUTE_IDS
            },
            "bid_delta_cents": payload["bid_delta_cents"],
            "ask_delta_cents": payload["ask_delta_cents"],
            "bid_delta_legs": payload["bid_delta_legs"],
            "ask_delta_legs": payload["ask_delta_legs"],
            "best_route": payload["best_route"],
            "best_net_cents_per_bundle": payload["best_net_cents_per_bundle"],
            "best_gross_cents": payload["best_gross_cents"],
            "max_bundles": payload["max_bundles"],
            "max_profit_dollars": payload["max_profit_dollars"],
            "days_to_close": payload["days_to_close"],
            "annualized_return_pct": payload["annualized_return_pct"],
        },
        search_blob=f"{ticker}\n{title}".lower(),
        close_time=close_time,
        market_count=market_count,
        # Events with no readable size sort as zero rather than dropping out.
        max_volume=max_vol if max_vol is not None else Decimal(0),
        max_open_interest=max_oi if max_oi is not None else Decimal(0),
        best_net_per_bundle=mispricing.best_net_per_bundle,
        sell_yes_net=mispricing.sell_yes_net,
        buy_no_net=mispricing.buy_no_net,
        buy_yes_net=mispricing.buy_yes_net,
        sell_no_net=mispricing.sell_no_net,
        max_bundles=payload["max_bundles"],
        bid_delta=mispricing.bid_delta,
        ask_delta=mispricing.ask_delta,
        net_edge_at_max_size=mispricing.net_edge_at_max_size,
        max_profit=mispricing.max_profit,
        annualized_return=mispricing.annualized_return,
    )


def _build_detail(event: dict[str, Any], mispricing: Mispricing) -> dict[str, Any]:
    """Copy one event and enrich it with per-leg spreads, totals and edge.

    The arithmetic is done here rather than in the browser because JavaScript
    numbers are binary floats; summing cents there would reintroduce exactly the
    precision loss the string encoding exists to avoid.
    """
    detail = dict(event)

    legs: list[dict[str, Any]] = []

    for index, market in enumerate(event.get("markets") or []):
        bid = _dec(market.get("yes_bid_cents"))
        ask = _dec(market.get("yes_ask_cents"))
        leg = dict(market)
        leg["spread_cents"] = _fmt(ask - bid) if bid is not None and ask is not None else None
        if index < len(mispricing.legs):
            leg.update(mispricing.legs[index])
        legs.append(leg)

    detail["markets"] = legs
    # For a mutually exclusive ladder YES bid/ask sums should straddle 100 cents.
    # NO sums are shown when present; they are never synthesised from YES.
    detail["ladder_totals"] = {
        "yes_bid_sum_cents": (
            _fmt(mispricing.yes_bid_sum) if legs and mispricing.yes_bid_complete else None
        ),
        "yes_ask_sum_cents": (
            _fmt(mispricing.yes_ask_sum) if legs and mispricing.yes_ask_complete else None
        ),
        "no_bid_sum_cents": (
            _fmt(mispricing.no_bid_sum) if legs and mispricing.no_bid_complete else None
        ),
        "no_ask_sum_cents": (
            _fmt(mispricing.no_ask_sum) if legs and mispricing.no_ask_complete else None
        ),
    }
    # Four-route nets, sizes and capital. Gross stays in the payload for hover.
    detail["mispricing"] = mispricing.payload
    detail["fee_model"] = FEE_MODEL
    return detail


def _parse_run(path: Path, raw: dict[str, Any]) -> ParsedRun:
    """Turn the decoded JSON of one run file into a :class:`ParsedRun`."""
    if not isinstance(raw, dict):
        raise TypeError(f"{path.name}: top level is {type(raw).__name__}, expected object")

    events = raw.get("events")
    if not isinstance(events, list):
        raise TypeError(f"{path.name}: 'events' is missing or not a list")

    funnel = decorate_funnel(raw.get("funnel") if isinstance(raw.get("funnel"), list) else [])

    meta = {key: value for key, value in raw.items() if key not in ("events", "funnel", "gates")}
    meta["filename"] = path.name
    gates = raw.get("gates") if isinstance(raw.get("gates"), dict) else {}

    # Time-to-close is measured from when the snapshot was taken rather than from
    # now, so every derived figure is reproducible from the file alone. Parsed
    # runs are cached until the file changes, so a "now" captured here would
    # otherwise be frozen at an arbitrary moment.
    reference_time = _parse_utc(raw.get("source_snapshot_captured_at_utc")) or _parse_utc(
        raw.get("filtered_at_utc")
    )

    rows: list[EventRow] = []
    details: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        mispricing = _mispricing(event, reference_time)
        row = _build_row(event, mispricing)
        rows.append(row)
        details[row.payload["event_ticker"]] = _build_detail(event, mispricing)

    return ParsedRun(
        run_id=path.stem,
        filename=path.name,
        meta=meta,
        gates=gates,
        funnel=funnel,
        rows=rows,
        details=details,
    )


class RunStore:
    """Lists and loads run files from one candidates directory, with caching.

    Cache keys include mtime and size, so re-running the pipeline invalidates the
    entry for free: a refreshed browser picks up the new file without a server
    restart. The directory listing itself is never cached.
    """

    def __init__(self, candidates_dir: Path) -> None:
        self.candidates_dir = candidates_dir
        self._runs: OrderedDict[tuple[str, int, int], ParsedRun] = OrderedDict()
        self._summaries: OrderedDict[tuple[str, int, int], dict[str, Any]] = OrderedDict()
        self._warned: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def run_files(self) -> list[Path]:
        """Every run file currently in the directory, unsorted. Empty if absent."""
        if not self.candidates_dir.is_dir():
            return []
        return [path for path in self.candidates_dir.glob(RUN_GLOB) if path.is_file()]

    def resolve_run_path(self, run_id: str) -> Path:
        """Map a run id to a file inside the candidates directory, or raise.

        Path traversal is rejected twice over: the id must match a strict
        pattern, and the resolved file's parent must be the configured directory
        (which also catches a symlink pointing elsewhere).
        """
        if not _RUN_ID_RE.match(run_id):
            raise RunNotFoundError(run_id)
        path = (self.candidates_dir / f"{run_id}.json").resolve()
        if path.parent != self.candidates_dir.resolve() or not path.is_file():
            raise RunNotFoundError(run_id)
        return path

    def load(self, run_id: str) -> ParsedRun:
        """Load one run by id, from cache when the file has not changed."""
        return self._load_path(self.resolve_run_path(run_id))

    def list_summaries(self) -> list[dict[str, Any]]:
        """Summaries of all readable runs, newest first.

        Unreadable files are skipped with a one-off warning instead of failing
        the request; a half-written file from a pipeline run in progress should
        not take the dashboard down.
        """
        summaries: list[tuple[datetime, str, dict[str, Any]]] = []
        for path in self.run_files():
            try:
                key = self._cache_key(path)
            except OSError as exc:
                self._warn_once(path, exc)
                continue
            with self._lock:
                cached = self._summaries.get(key)
            if cached is None:
                try:
                    cached = self._load_path(path, key=key).summary()
                except _UNUSABLE as exc:
                    self._warn_once(path, exc)
                    continue
                with self._lock:
                    self._summaries[key] = cached
                    while len(self._summaries) > _MAX_CACHED_SUMMARIES:
                        self._summaries.popitem(last=False)
            # Runs without a usable timestamp still sort deterministically, just
            # at the bottom, ordered by filename.
            when = _parse_utc(cached.get("filtered_at_utc")) or datetime.min.replace(
                tzinfo=timezone.utc
            )
            summaries.append((when, path.name, cached))

        summaries.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in summaries]

    def _cache_key(self, path: Path) -> tuple[str, int, int]:
        stat = path.stat()
        return (str(path.resolve()), stat.st_mtime_ns, stat.st_size)

    def _load_path(self, path: Path, key: tuple[str, int, int] | None = None) -> ParsedRun:
        cache_key = key or self._cache_key(path)
        with self._lock:
            cached = self._runs.get(cache_key)
            if cached is not None:
                self._runs.move_to_end(cache_key)
                return cached

        # Encoding is explicit because Windows would otherwise decode as cp1252.
        # utf-8-sig rather than utf-8: the pipeline writes no BOM, but any Windows
        # tool that re-saves a run file (Notepad, Set-Content, Excel) adds one, and
        # strict utf-8 would then make that file disappear from the run list.
        with path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        parsed = _parse_run(path, raw)

        with self._lock:
            self._runs[cache_key] = parsed
            self._summaries[cache_key] = parsed.summary()
            while len(self._runs) > _MAX_CACHED_RUNS:
                self._runs.popitem(last=False)
            while len(self._summaries) > _MAX_CACHED_SUMMARIES:
                self._summaries.popitem(last=False)
        return parsed

    def _warn_once(self, path: Path, exc: BaseException) -> None:
        """Warn about an unusable file once per version of that file."""
        try:
            marker = (str(path), path.stat().st_mtime_ns)
        except OSError:
            marker = (str(path), 0)
        if marker in self._warned:
            return
        self._warned.add(marker)
        logger.warning("skipping unreadable run file %s: %s", path.name, exc)


WATCHLIST_GLOB = "watchlist_*.json"
_WATCHLIST_ID_RE = re.compile(r"^watchlist_[A-Za-z0-9._-]+$")


def decorate_funnel(stages: Iterable[Any]) -> list[dict[str, Any]]:
    """Add deltas and bar widths to a pipeline funnel, snapshot or scoring."""
    rows = [stage for stage in stages if isinstance(stage, dict)]
    peak = max((stage.get("surviving") or 0 for stage in rows), default=0)
    peak = peak if isinstance(peak, int) else 0
    funnel: list[dict[str, Any]] = []
    previous: int | None = None
    for stage in rows:
        surviving = stage.get("surviving")
        surviving = surviving if isinstance(surviving, int) else 0
        funnel.append(
            {
                "gate": stage.get("gate"),
                "surviving": surviving,
                "dropped": stage.get("dropped"),
                "delta": None if previous is None else surviving - previous,
                "bar_pct": round(surviving / peak * 100, 3) if peak else 0.0,
            }
        )
        previous = surviving
    return funnel


def watchlist_id_for_run(run_id: str) -> str | None:
    if not run_id.startswith("candidates_"):
        return None
    return "watchlist_" + run_id[len("candidates_") :]


class WatchlistStore:
    """Read-only loader for ``watchlist_*.json`` files written by score.py."""

    def __init__(self, watchlist_dir: Path) -> None:
        self.watchlist_dir = watchlist_dir

    def files(self) -> list[Path]:
        if not self.watchlist_dir.is_dir():
            return []
        return [path for path in self.watchlist_dir.glob(WATCHLIST_GLOB) if path.is_file()]

    def resolve(self, watchlist_id: str) -> Path | None:
        if not _WATCHLIST_ID_RE.match(watchlist_id):
            return None
        path = (self.watchlist_dir / f"{watchlist_id}.json").resolve()
        if path.parent != self.watchlist_dir.resolve() or not path.is_file():
            return None
        return path

    def _load_path(self, path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except _UNUSABLE:
            return None
        if not isinstance(raw, dict):
            return None
        payload = dict(raw)
        payload["id"] = path.stem
        payload["filename"] = path.name
        if isinstance(payload.get("funnel"), list):
            payload["funnel"] = decorate_funnel(payload["funnel"])
        return payload

    def newest(self) -> dict[str, Any] | None:
        files = sorted(self.files(), key=lambda path: path.stat().st_mtime)
        if not files:
            return None
        return self._load_path(files[-1])

    def for_run(self, run_id: str) -> dict[str, Any] | None:
        watchlist_id = watchlist_id_for_run(run_id)
        if watchlist_id is None:
            return None
        path = self.resolve(watchlist_id)
        if path is None:
            return None
        return self._load_path(path)


def filter_rows(
    rows: list[EventRow],
    query: str,
    min_net_edge_cents: Decimal | None = None,
) -> list[EventRow]:
    """Apply the case-insensitive substring search and the min-edge filter."""
    needle = query.strip().lower()
    selected = rows
    if needle:
        selected = [row for row in selected if needle in row.search_blob]
    if min_net_edge_cents is not None:
        # Filtered on the best route's conservative (single-bundle) net, the
        # same figure the table's Best edge column shows. An event whose best
        # edge could not be computed is unknown rather than zero, so it drops
        # out instead of ranking as break-even.
        selected = [
            row
            for row in selected
            if row.best_net_per_bundle is not None
            and row.best_net_per_bundle >= min_net_edge_cents
        ]
    return selected


# Sort keys that can legitimately be absent: a route that could not be priced,
# or no executable size on the best route. Kept apart from the always-present
# keys below so that missing data can be pushed to the end instead of sorting
# as zero.
_OPTIONAL_SORT_KEYS = {
    "sell_yes_net": lambda row: row.sell_yes_net,
    "buy_no_net": lambda row: row.buy_no_net,
    "buy_yes_net": lambda row: row.buy_yes_net,
    "sell_no_net": lambda row: row.sell_no_net,
    "max_bundles": lambda row: row.max_bundles,
}

_SORT_KEYS = {
    "market_count": lambda row: row.market_count,
    "max_volume": lambda row: row.max_volume,
    "max_open_interest": lambda row: row.max_open_interest,
}


def sort_rows(rows: list[EventRow], sort: str, order: str) -> list[EventRow]:
    """Sort rows by one of :data:`SORT_FIELDS`.

    Events missing the value being sorted on are always placed last, in either
    direction, so that missing data never occupies the first page.
    """
    descending = order == "desc"
    optional = _OPTIONAL_SORT_KEYS.get(sort)
    if optional is not None:
        present = [row for row in rows if optional(row) is not None]
        missing = [row for row in rows if optional(row) is None]
        present.sort(
            key=lambda row: (optional(row), row.payload["event_ticker"]), reverse=descending
        )
        return present + missing

    key = _SORT_KEYS[sort]
    return sorted(rows, key=lambda row: (key(row), row.payload["event_ticker"]), reverse=descending)
