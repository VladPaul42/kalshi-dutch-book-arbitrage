"""Decimal-exact pricing and sizing for the four mutually-exclusive baskets.

The scorer uses the conservative one-bundle edge. The live trader additionally
uses unnetted capital, time-to-close, top-of-book bottleneck and a per-event
capital cap. Keeping both paths here prevents fee or route formulas drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from kalshi.fixedpoint import to_decimal

# Same assumptions as Dashboard/data_store.py. Check the fee schedule for a
# series before treating a number derived from these as real.
TAKER_FEE_RATE = Decimal("0.07")
ASSUMED_SERIES_MULTIPLIER = Decimal("1")
CENTS_PER_DOLLAR = Decimal(100)
SETTLEMENT_CENTS = Decimal(100)
DAYS_PER_YEAR = Decimal(365)
SECONDS_PER_DAY = Decimal(86400)

# Tie-break order when two routes share the highest net.
ROUTE_IDS = ("buy_no", "buy_yes", "sell_yes", "sell_no")


@dataclass(frozen=True)
class BestEdge:
    """The highest one-bundle net, plus optional live sizing information."""

    net_per_bundle: Decimal | None
    route_id: str | None
    gross_per_bundle: Decimal | None = None
    prices: tuple[Decimal, ...] = ()
    sizes: tuple[Decimal, ...] = ()
    capital_unnetted_per_bundle: Decimal | None = None
    bottleneck_bundles: int | None = None
    annualized_pct: Decimal | None = None
    bundles: int | None = None
    net_total: Decimal | None = None
    capital_total: Decimal | None = None


def _taker_fee_cents(price_cents: Decimal, contracts: Decimal) -> Decimal:
    """Estimated taker fee in whole cents, rounded up once for the whole order."""
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


def route_executable(
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


def _route_net(gross: Decimal, prices: list[Decimal | None]) -> Decimal:
    traded = [price for price in prices if price is not None]
    fees = sum((_taker_fee_cents(price, Decimal(1)) for price in traded), Decimal(0))
    return gross - fees


def _parse_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _whole_bottleneck(sizes: list[Decimal | None]) -> int | None:
    if not sizes or any(size is None for size in sizes):
        return None
    smallest = min(size for size in sizes if size is not None)
    whole = int(smallest.to_integral_value(rounding=ROUND_FLOOR))
    return whole if whole > 0 else None


def _annualized_pct(
    net: Decimal,
    capital: Decimal,
    close_time: datetime | None,
    reference_time: datetime | None,
) -> Decimal | None:
    if close_time is None or reference_time is None or capital <= 0:
        return None
    delta = close_time - reference_time
    seconds = Decimal(delta.days * 86400 + delta.seconds)
    if seconds <= 0:
        return None
    days = seconds / SECONDS_PER_DAY
    return net / capital * (DAYS_PER_YEAR / days) * Decimal(100)


def _sized_net(gross: Decimal, prices: tuple[Decimal, ...], bundles: int) -> Decimal:
    contracts = Decimal(bundles)
    fees = sum(
        (_taker_fee_cents(price, contracts) for price in prices), Decimal(0)
    )
    return gross * contracts - fees


def _priced_routes(
    event: dict[str, Any],
) -> dict[
    str,
    tuple[Decimal, list[Decimal | None], list[Decimal | None], Decimal] | None,
]:
    """The four ME routes, or None when that route cannot be hit.

    An event that is not mutually exclusive prices no routes. A route with a
    0/100 price or a missing/non-positive size is unpriced, not a zero edge.
    """
    routes: dict[
        str,
        tuple[Decimal, list[Decimal | None], list[Decimal | None], Decimal] | None,
    ] = {route_id: None for route_id in ROUTE_IDS}
    if event.get("mutually_exclusive") is not True:
        return routes

    markets = event.get("markets") or []
    yes_bids = [to_decimal(market.get("yes_bid_cents")) for market in markets]
    yes_asks = [to_decimal(market.get("yes_ask_cents")) for market in markets]
    no_bids = [to_decimal(market.get("no_bid_cents")) for market in markets]
    no_asks = [to_decimal(market.get("no_ask_cents")) for market in markets]
    yes_bid_sizes = [
        to_decimal(market.get("yes_bid_size_contracts")) for market in markets
    ]
    yes_ask_sizes = [
        to_decimal(market.get("yes_ask_size_contracts")) for market in markets
    ]
    no_bid_sizes = [
        to_decimal(market.get("no_bid_size_contracts")) for market in markets
    ]
    no_ask_sizes = [
        to_decimal(market.get("no_ask_size_contracts")) for market in markets
    ]

    n_legs = len(markets)
    others_payout = SETTLEMENT_CENTS * Decimal(max(n_legs - 1, 0))

    if route_executable(yes_bids, yes_bid_sizes):
        gross = sum(yes_bids, Decimal(0)) - SETTLEMENT_CENTS
        routes["sell_yes"] = (
            gross,
            yes_bids,
            yes_bid_sizes,
            sum(
                (SETTLEMENT_CENTS - bid for bid in yes_bids if bid is not None),
                Decimal(0),
            ),
        )
    if route_executable(no_asks, no_ask_sizes):
        routes["buy_no"] = (
            others_payout - sum(no_asks, Decimal(0)),
            no_asks,
            no_ask_sizes,
            sum(no_asks, Decimal(0)),
        )
    if route_executable(yes_asks, yes_ask_sizes):
        routes["buy_yes"] = (
            SETTLEMENT_CENTS - sum(yes_asks, Decimal(0)),
            yes_asks,
            yes_ask_sizes,
            sum(yes_asks, Decimal(0)),
        )
    if route_executable(no_bids, no_bid_sizes):
        gross = sum(no_bids, Decimal(0)) - others_payout
        routes["sell_no"] = (
            gross,
            no_bids,
            no_bid_sizes,
            sum(
                (SETTLEMENT_CENTS - bid for bid in no_bids if bid is not None),
                Decimal(0),
            ),
        )
    return routes


def route_nets(event: dict[str, Any]) -> dict[str, Decimal | None]:
    """After-fee one-bundle net for each route. None when that route is unpriced."""
    routes = _priced_routes(event)
    return {
        route_id: None if route is None else _route_net(route[0], route[1])
        for route_id, route in routes.items()
    }


def quote_best_edge(
    event: dict[str, Any],
    *,
    reference_time: datetime | None = None,
    capital_cap_cents: Decimal | None = None,
) -> BestEdge:
    """Price the four ME basket trades and return the best 1-bundle net.

    Returns ``BestEdge(None, None)`` when the event is not mutually exclusive
    or when no route can be priced. Negative nets are valid; they are not
    treated as missing. A route with a 0/100 price or missing/non-positive
    size is unpriced, not a zero edge.
    """
    routes = _priced_routes(event)
    best_id: str | None = None
    best_net: Decimal | None = None
    for route_id in ROUTE_IDS:
        route = routes[route_id]
        if route is None:
            continue
        net = _route_net(route[0], route[1])
        if best_net is None or net > best_net:
            best_net = net
            best_id = route_id
    if best_id is None or best_net is None:
        return BestEdge(None, None)

    gross, raw_prices, raw_sizes, capital = routes[best_id]  # type: ignore[misc]
    prices = tuple(price for price in raw_prices if price is not None)
    sizes = tuple(size for size in raw_sizes if size is not None)
    bottleneck = _whole_bottleneck(raw_sizes)
    close_time = _parse_utc(event.get("close_time"))
    now = reference_time.astimezone(timezone.utc) if reference_time else None
    annualized = _annualized_pct(best_net, capital, close_time, now)

    bundles: int | None = None
    net_total: Decimal | None = None
    capital_total: Decimal | None = None
    if capital_cap_cents is not None and bottleneck is not None and capital > 0:
        by_cap = int(
            (capital_cap_cents / capital).to_integral_value(rounding=ROUND_FLOOR)
        )
        bundles = min(by_cap, bottleneck)
        if bundles > 0:
            net_total = _sized_net(gross, prices, bundles)
            capital_total = capital * Decimal(bundles)

    return BestEdge(
        net_per_bundle=best_net,
        route_id=best_id,
        gross_per_bundle=gross,
        prices=prices,
        sizes=sizes,
        capital_unnetted_per_bundle=capital,
        bottleneck_bundles=bottleneck,
        annualized_pct=annualized,
        bundles=bundles,
        net_total=net_total,
        capital_total=capital_total,
    )


def best_net_cents_per_bundle(event: dict[str, Any]) -> Decimal | None:
    """``best_net_cents_per_bundle`` for one candidate event, or None if unpriced."""
    return quote_best_edge(event).net_per_bundle


def _edge_from_route(
    event: dict[str, Any],
    route_id: str,
    route: tuple[Decimal, list[Decimal | None], list[Decimal | None], Decimal],
    *,
    reference_time: datetime | None,
) -> BestEdge:
    gross, raw_prices, raw_sizes, capital = route
    prices = tuple(price for price in raw_prices if price is not None)
    sizes = tuple(size for size in raw_sizes if size is not None)
    net = _route_net(gross, raw_prices)
    close_time = _parse_utc(event.get("close_time"))
    now = reference_time.astimezone(timezone.utc) if reference_time else None
    return BestEdge(
        net_per_bundle=net,
        route_id=route_id,
        gross_per_bundle=gross,
        prices=prices,
        sizes=sizes,
        capital_unnetted_per_bundle=capital,
        bottleneck_bundles=_whole_bottleneck(raw_sizes),
        annualized_pct=_annualized_pct(net, capital, close_time, now),
    )


def quote_route(
    event: dict[str, Any],
    route_id: str,
    *,
    reference_time: datetime | None = None,
) -> BestEdge:
    """Price one route. ``BestEdge(None, None)`` when that route cannot be hit."""
    route = _priced_routes(event).get(route_id)
    if route is None:
        return BestEdge(None, None)
    return _edge_from_route(event, route_id, route, reference_time=reference_time)


def route_quote_prices(event: dict[str, Any], route_id: str) -> list[Decimal | None] | None:
    """Current traded price per leg, ignoring size.

    ``None`` when the event is not mutually exclusive or the route id is unknown.
    A leg is ``None`` when its price is missing or outside (0, 100).
    """
    if event.get("mutually_exclusive") is not True or route_id not in ROUTE_IDS:
        return None
    markets = event.get("markets") or []
    if route_id == "sell_yes":
        raw = [to_decimal(market.get("yes_bid_cents")) for market in markets]
    elif route_id == "buy_no":
        raw = [to_decimal(market.get("no_ask_cents")) for market in markets]
    elif route_id == "buy_yes":
        raw = [to_decimal(market.get("yes_ask_cents")) for market in markets]
    else:
        raw = [to_decimal(market.get("no_bid_cents")) for market in markets]
    return [
        price if price is not None and Decimal(0) < price < SETTLEMENT_CENTS else None
        for price in raw
    ]


def fresh_clip_bundles(
    capital_per_bundle: Decimal | None,
    bottleneck: int | None,
    shot_cap_cents: Decimal,
    remaining_cents: Decimal,
) -> int:
    """Whole bundles that fit the book, one shot, and unused event capital."""
    if (
        capital_per_bundle is None
        or capital_per_bundle <= 0
        or bottleneck is None
        or shot_cap_cents <= 0
        or remaining_cents <= 0
    ):
        return 0
    by_shot = int(
        (shot_cap_cents / capital_per_bundle).to_integral_value(rounding=ROUND_FLOOR)
    )
    by_remaining = int(
        (remaining_cents / capital_per_bundle).to_integral_value(rounding=ROUND_FLOOR)
    )
    return max(min(bottleneck, by_shot, by_remaining), 0)


def unnetted_capital_cents(route_id: str, price_cents: Decimal, count: Decimal) -> Decimal:
    """Capital a fill locks, using the same unnetted definition as the route quote."""
    if count <= 0:
        return Decimal(0)
    if route_id in ("buy_yes", "buy_no"):
        return price_cents * count
    if route_id in ("sell_yes", "sell_no"):
        return (SETTLEMENT_CENTS - price_cents) * count
    raise ValueError(f"unknown route {route_id!r}")


def fresh_clip_economics(
    route_id: str,
    prices: tuple[Decimal, ...] | list[Decimal],
    bundles: int,
) -> tuple[Decimal, Decimal] | None:
    """After-fee net and unnetted capital for ``bundles`` equal contracts per leg.

    Fee is ceiled once per leg at the clip size, matching ``_sized_net``. Capital
    is the route's unnetted capital per bundle times the clip size.
    """
    if route_id not in ROUTE_IDS or bundles <= 0 or not prices:
        return None
    priced = tuple(prices)
    if any(not (Decimal(0) < price < SETTLEMENT_CENTS) for price in priced):
        return None
    traded = sum(priced, Decimal(0))
    others = SETTLEMENT_CENTS * Decimal(max(len(priced) - 1, 0))
    if route_id == "buy_yes":
        gross = SETTLEMENT_CENTS - traded
    elif route_id == "buy_no":
        gross = others - traded
    elif route_id == "sell_yes":
        gross = traded - SETTLEMENT_CENTS
    else:
        gross = traded - others
    net = _sized_net(gross, priced, bundles)
    capital = sum(
        (
            unnetted_capital_cents(route_id, price, Decimal(bundles))
            for price in priced
        ),
        Decimal(0),
    )
    return net, capital


def completion_package_net(
    route_id: str,
    n_legs: int,
    lots_per_leg: list[list[tuple[Decimal, Decimal]]],
) -> Decimal | None:
    """After-fee net of one balanced package.

    Each leg is a list of ``(price_cents, count)`` lots. Sunk fills stay separate
    orders so the taker fee ceilings once per lot. Returns ``None`` when the leg
    counts are not equal or the route is unknown.
    """
    if route_id not in ROUTE_IDS or not lots_per_leg:
        return None
    totals: list[Decimal] = []
    for lots in lots_per_leg:
        totals.append(sum((count for _, count in lots), Decimal(0)))
    package = totals[0]
    if package <= 0 or any(total != package for total in totals):
        return None
    traded = sum(
        (price * count for lots in lots_per_leg for price, count in lots),
        Decimal(0),
    )
    others = SETTLEMENT_CENTS * Decimal(max(n_legs - 1, 0))
    if route_id == "buy_yes":
        gross = package * SETTLEMENT_CENTS - traded
    elif route_id == "sell_yes":
        gross = traded - package * SETTLEMENT_CENTS
    elif route_id == "buy_no":
        gross = package * others - traded
    else:
        gross = traded - package * others
    fees = sum(
        (
            _taker_fee_cents(price, count)
            for lots in lots_per_leg
            for price, count in lots
            if count > 0
        ),
        Decimal(0),
    )
    return gross - fees
