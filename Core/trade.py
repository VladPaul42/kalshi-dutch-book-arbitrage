"""Long-running live watchlist trader.

Prices come from Kalshi's ticker WebSocket and stay in memory. A ticker update
checks that game immediately. Every 2 seconds, and again as soon as a new
watchlist is loaded, the trader also checks every game from the quotes it
already holds. The saved watchlist only selects events; its matching candidates
file supplies leg tickers and close times. Dry mode is the default and is
controlled only by Core/.env.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi.config import (
    CANDIDATES_DIR,
    TRADER_DIR,
    WATCHLIST_DIR,
    ConfigError,
    TraderConfig,
    enable_utf8_stdout,
    load_api_config,
    load_trader_config,
)
from kalshi.client import KalshiReadOnlyClient
from kalshi.fixedpoint import (
    CENT_QUANTUM,
    CENTS_PER_DOLLAR,
    contract_count,
    dollars_from_cents,
    price_cents,
    to_decimal,
    to_json,
)
from kalshi.mispricing import (
    SETTLEMENT_CENTS,
    BestEdge,
    completion_package_net,
    fresh_clip_bundles,
    fresh_clip_economics,
    quote_best_edge,
    quote_route,
    route_quote_prices,
    unnetted_capital_cents,
)
from kalshi.orders import KalshiOrderClient
from kalshi.ws import KalshiTickerWebSocket

STATUS_PATH = TRADER_DIR / "status.json"
POLL_SECONDS = 2
SAMPLE_SECONDS = 2
# Ask Kalshi which events are still open. This stays off the quote path.
RECONCILE_SECONDS = 60
MAX_TRADES = 10
MAX_EDGE_SAMPLES = 30
MARKET_LOOKUP_BATCH = 100


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def short_duration(seconds: int) -> str:
    seconds = max(seconds, 0)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@dataclass
class Universe:
    watchlist_id: str
    signature: tuple[str, int, int]
    events: dict[str, dict[str, Any]]
    market_to_event: dict[str, str]

    @property
    def market_tickers(self) -> set[str]:
        return set(self.market_to_event)


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def newest_watchlist(environment: str) -> tuple[Path, dict[str, Any]] | None:
    if not WATCHLIST_DIR.is_dir():
        return None
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for path in WATCHLIST_DIR.glob("watchlist_*.json"):
        payload = _read_object(path)
        if payload is None or payload.get("environment") != environment:
            continue
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified, path, payload))
    if not candidates:
        return None
    _, path, payload = max(candidates, key=lambda row: (row[0], row[1].name))
    return path, payload


def load_universe(environment: str) -> Universe | None:
    selected = newest_watchlist(environment)
    if selected is None:
        return None
    watchlist_path, watchlist = selected
    source_name = watchlist.get("source_candidates")
    if not isinstance(source_name, str) or not source_name:
        return None
    candidates_path = (CANDIDATES_DIR / source_name).resolve()
    if candidates_path.parent != CANDIDATES_DIR.resolve():
        return None
    candidates = _read_object(candidates_path)
    if candidates is None or candidates.get("environment") != environment:
        return None

    wanted = {
        row.get("event_ticker")
        for row in watchlist.get("watchlist") or []
        if isinstance(row, dict) and isinstance(row.get("event_ticker"), str)
    }
    events: dict[str, dict[str, Any]] = {}
    market_to_event: dict[str, str] = {}
    for source_event in candidates.get("events") or []:
        if not isinstance(source_event, dict):
            continue
        event_ticker = source_event.get("event_ticker")
        if event_ticker not in wanted:
            continue
        event = copy.deepcopy(source_event)
        markets = [
            market
            for market in event.get("markets") or []
            if isinstance(market, dict)
            and isinstance(market.get("ticker"), str)
            and market.get("ticker")
        ]
        event["markets"] = markets
        for market in markets:
            ticker = market.get("ticker")
            if not isinstance(ticker, str) or not ticker:
                continue
            # Snapshot values select the universe but must never trigger a trade.
            for field in (
                "yes_bid_cents",
                "yes_ask_cents",
                "no_bid_cents",
                "no_ask_cents",
                "yes_bid_size_contracts",
                "yes_ask_size_contracts",
                "no_bid_size_contracts",
                "no_ask_size_contracts",
            ):
                market[field] = None
            market_to_event[ticker] = event_ticker
        events[event_ticker] = event

    stat = watchlist_path.stat()
    return Universe(
        watchlist_id=watchlist_path.stem,
        signature=(str(watchlist_path.resolve()), stat.st_mtime_ns, stat.st_size),
        events=events,
        market_to_event=market_to_event,
    )


def _book_side(route_id: str) -> str:
    """``bid`` for routes that hit the YES bid, ``ask`` for the YES ask."""
    if route_id in ("sell_yes", "buy_no"):
        return "bid"
    return "ask"


def _size_cap_key(side: str) -> str:
    return "_size_cap_yes_bid" if side == "bid" else "_size_cap_yes_ask"


def _size_keys(side: str) -> tuple[str, str]:
    if side == "bid":
        return "yes_bid_size_contracts", "no_ask_size_contracts"
    return "yes_ask_size_contracts", "no_bid_size_contracts"


def _price_key(side: str) -> str:
    return "yes_bid_cents" if side == "bid" else "yes_ask_cents"


def _apply_shown_size(
    market: dict[str, Any],
    side: str,
    incoming: Decimal | None,
    *,
    price_changed: bool,
) -> None:
    """Write a ticker size unless it would restore contracts already taken.

    A same-price size larger than the cap left after our fills is ignored.
    A smaller size, an empty size, or a new price replaces the top.
    """
    primary, mirror = _size_keys(side)
    cap_key = _size_cap_key(side)
    if price_changed or incoming is None:
        market.pop(cap_key, None)
        market[primary] = to_json(incoming)
        market[mirror] = to_json(incoming)
        return
    price = to_decimal(market.get(_price_key(side)))
    cap = market.get(cap_key)
    if isinstance(cap, dict):
        cap_price = to_decimal(cap.get("price"))
        cap_size = to_decimal(cap.get("size"))
        if (
            cap_price is not None
            and price is not None
            and cap_price == price
            and cap_size is not None
            and incoming > cap_size
        ):
            return
        if (
            cap_price == price
            and cap_size is not None
            and incoming <= cap_size
        ):
            cap["size"] = to_json(incoming)
    market[primary] = to_json(incoming)
    market[mirror] = to_json(incoming)


def patch_ticker(universe: Universe, message: dict[str, Any]) -> str | None:
    ticker = message.get("market_ticker") or message.get("ticker")
    if not isinstance(ticker, str):
        return None
    event_ticker = universe.market_to_event.get(ticker)
    if event_ticker is None:
        return None
    event = universe.events[event_ticker]
    market = next(
        (row for row in event["markets"] if row.get("ticker") == ticker), None
    )
    if market is None:
        return None

    old_bid = to_decimal(market.get("yes_bid_cents"))
    old_ask = to_decimal(market.get("yes_ask_cents"))
    if "yes_bid_dollars" in message or "yes_bid" in message:
        bid = price_cents(message, "yes_bid_dollars")
        market["yes_bid_cents"] = to_json(bid)
        market["no_ask_cents"] = (
            to_json(SETTLEMENT_CENTS - bid) if bid is not None else None
        )
        if bid != old_bid:
            market.pop(_size_cap_key("bid"), None)
    if "yes_ask_dollars" in message or "yes_ask" in message:
        ask = price_cents(message, "yes_ask_dollars")
        market["yes_ask_cents"] = to_json(ask)
        market["no_bid_cents"] = (
            to_json(SETTLEMENT_CENTS - ask) if ask is not None else None
        )
        if ask != old_ask:
            market.pop(_size_cap_key("ask"), None)
    if "yes_bid_size_fp" in message or "yes_bid_size" in message:
        bid_size = contract_count(message, "yes_bid_size_fp")
        _apply_shown_size(
            market,
            "bid",
            bid_size,
            price_changed=to_decimal(market.get("yes_bid_cents")) != old_bid,
        )
    if "yes_ask_size_fp" in message or "yes_ask_size" in message:
        ask_size = contract_count(message, "yes_ask_size_fp")
        _apply_shown_size(
            market,
            "ask",
            ask_size,
            price_changed=to_decimal(market.get("yes_ask_cents")) != old_ask,
        )
    return event_ticker


class StatusFile:
    def __init__(self, environment: str, config: TraderConfig) -> None:
        existing = _read_object(STATUS_PATH) or {}
        same_environment = existing.get("environment") == environment
        positions = existing.get("positions") if same_environment else {}
        in_flight = existing.get("in_flight") if same_environment else None
        shard_cash = existing.get("shard_cash") if same_environment else None
        self.payload: dict[str, Any] = {
            "dry_mode": config.dry_mode,
            "max_capital_per_event_cents": format(config.max_capital_per_event_cents, "f"),
            "environment": environment,
            "watchlist_id": None,
            "ws_connected": False,
            "updated_at_utc": None,
            "last_trades": (
                list(existing.get("last_trades") or [])[:MAX_TRADES]
                if same_environment
                else []
            ),
            "positions": positions if isinstance(positions, dict) else {},
            "shard_cash": shard_cash if isinstance(shard_cash, dict) else None,
            "in_flight": in_flight if isinstance(in_flight, dict) else None,
            "live_edges": {},
        }
        self.write()

    def expire_positions_past_close(self, keep_event: str | None = None) -> list[str]:
        """Drop rows whose stored latest close has passed.

        Used only when Kalshi cannot be read. A missing close stays, and so
        does ``keep_event`` (the in-flight reservation's event).
        """
        now = utc_now()
        kept: dict[str, Any] = {}
        dropped: list[str] = []
        for ticker, position in self.payload["positions"].items():
            if not isinstance(position, dict):
                continue
            if keep_event is not None and ticker == keep_event:
                kept[ticker] = position
                continue
            close = parse_utc(position.get("close_time"))
            if close is not None and close <= now:
                dropped.append(ticker)
                continue
            kept[ticker] = position
        self.payload["positions"] = kept
        return dropped

    def position(self, event_ticker: str) -> dict[str, Any] | None:
        position = self.payload["positions"].get(event_ticker)
        return position if isinstance(position, dict) else None

    def filled_capital_cents(self, event_ticker: str) -> Decimal:
        position = self.position(event_ticker)
        if position is None:
            return Decimal(0)
        parsed = to_decimal(position.get("filled_capital_cents"))
        return parsed if parsed is not None and parsed > 0 else Decimal(0)

    def remaining_cents(self, event_ticker: str, config: TraderConfig) -> Decimal:
        return config.max_capital_per_event_cents - self.filled_capital_cents(event_ticker)

    def set_connection(self, connected: bool, watchlist_id: str | None) -> None:
        self.payload["ws_connected"] = connected
        self.payload["watchlist_id"] = watchlist_id
        self.write()

    def add_trade(self, trade: dict[str, Any]) -> None:
        self.payload["last_trades"] = [
            trade,
            *self.payload.get("last_trades", []),
        ][:MAX_TRADES]
        self.write()

    def write(self) -> None:
        TRADER_DIR.mkdir(parents=True, exist_ok=True)
        self.payload["updated_at_utc"] = utc_now().isoformat(timespec="seconds")
        temp = STATUS_PATH.with_name(STATUS_PATH.name + ".partial")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(self.payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, STATUS_PATH)


def yes_order(route_id: str, route_price: Decimal) -> tuple[str, Decimal]:
    """V2 book side and YES price for one route price."""
    if route_id == "sell_yes":
        return "ask", route_price
    if route_id == "buy_no":
        return "ask", SETTLEMENT_CENTS - route_price
    if route_id == "buy_yes":
        return "bid", route_price
    # sell_no: buying YES is the V2 representation of selling NO.
    return "bid", SETTLEMENT_CENTS - route_price


def route_price_from_yes(route_id: str, yes_cents: Decimal) -> Decimal:
    if route_id in ("sell_yes", "buy_yes"):
        return yes_cents
    return SETTLEMENT_CENTS - yes_cents


def orders_for_counts(
    event: dict[str, Any],
    route_id: str,
    counts: dict[str, Decimal],
    prices: dict[str, Decimal],
) -> list[dict[str, Any]]:
    """One IOC leg per ticker with a positive count. Ids are assigned here."""
    orders: list[dict[str, Any]] = []
    for market in event.get("markets") or []:
        ticker = market.get("ticker")
        if not isinstance(ticker, str):
            continue
        count = counts.get(ticker, Decimal(0))
        price = prices.get(ticker)
        if count <= 0 or price is None:
            continue
        side, yes_price = yes_order(route_id, price)
        orders.append(
            {
                "ticker": ticker,
                "side": side,
                "price_dollars": dollars_from_cents(yes_price),
                "count": count,
                "price_cents": price,
                "client_order_id": str(uuid.uuid4()),
            }
        )
    return orders


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _error_text(error: Any) -> str | None:
    if not error:
        return None
    if isinstance(error, str):
        text = error.strip()
        return text or None
    if isinstance(error, dict):
        message = error.get("message") or error.get("error") or error.get("code")
        if message:
            return str(message)
        return json.dumps(error, ensure_ascii=False)
    return str(error)


def _reply_fields(order: dict[str, Any] | None) -> dict[str, Any]:
    """Fields copied from one aligned batch leg. Fill count stays with accounting."""
    if not isinstance(order, dict):
        return {}
    fields: dict[str, Any] = {}
    remaining = order.get("remaining_count")
    if remaining is not None:
        parsed = to_decimal(remaining)
        fields["remaining_count"] = _decimal_text(parsed) if parsed is not None else remaining
    order_id = order.get("order_id")
    if order_id:
        fields["order_id"] = order_id
    error = _error_text(order.get("error"))
    if error:
        fields["error"] = error
    response = order.get("response")
    if isinstance(response, dict) and response:
        fields["response"] = response
    return fields


def _attach_batch_reply(trade: dict[str, Any], execution: dict[str, Any]) -> None:
    """Store the batch reply on the trade. Does not change fill accounting."""
    orders = execution.get("orders")
    if not isinstance(orders, list):
        orders = []
    by_id = {
        order.get("client_order_id"): order
        for order in orders
        if isinstance(order, dict) and order.get("client_order_id")
    }
    legs = trade.get("legs")
    if not isinstance(legs, list):
        legs = []
    for index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue
        reply = by_id.get(leg.get("client_order_id"))
        if not isinstance(reply, dict) and index < len(orders) and isinstance(orders[index], dict):
            reply = orders[index]
        leg.update(_reply_fields(reply if isinstance(reply, dict) else None))
    if any(isinstance(leg, dict) and isinstance(leg.get("response"), dict) for leg in legs):
        return
    if execution.get("response_body") is not None:
        trade["response"] = execution["response_body"]


def _fill_count_of(payload: dict[str, Any]) -> Decimal | None:
    raw = payload.get("fill_count_fp")
    if raw is None:
        raw = payload.get("fill_count")
    return to_decimal(raw)


def _fill_quantity(payload: dict[str, Any]) -> Decimal | None:
    raw = payload.get("count_fp")
    if raw is None:
        raw = payload.get("count")
    return to_decimal(raw)


def _fill_price_cents(route_id: str, payload: dict[str, Any]) -> Decimal | None:
    """Route cents from ``average_fill_price`` (YES dollars).

    None when that price is missing. Only this field is a YES fill price.
    """
    average = to_decimal(payload.get("average_fill_price"))
    if average is None or average <= 0:
        return None
    return route_price_from_yes(route_id, average * Decimal(100))


def _remember_yes_fill(
    leg: dict[str, Any], count: Decimal, yes_dollars: Decimal | None
) -> None:
    """Keep the YES fill price on the reservation, ahead of the ledger write."""
    leg["fill_count"] = _decimal_text(count)
    if count > 0 and yes_dollars is not None:
        leg["average_fill_price"] = _decimal_text(yes_dollars)
    else:
        leg.pop("average_fill_price", None)


def _yes_vwap_dollars(
    fills: list[dict[str, Any]], order_count: Decimal
) -> Decimal | None:
    """YES dollar VWAP when fill quantities add up to the order's fill count."""
    if order_count <= 0:
        return None
    total_count = Decimal(0)
    total_notional = Decimal(0)
    for fill in fills:
        if not isinstance(fill, dict):
            return None
        count = _fill_quantity(fill)
        if count is None or count < 0:
            return None
        if count == 0:
            continue
        price = to_decimal(fill.get("yes_price_dollars"))
        if price is None or price <= 0:
            return None
        total_count += count
        total_notional += price * count
    if total_count != order_count:
        return None
    return total_notional / total_count


def _fills_from_saved(
    route_id: str, reserved: list[dict[str, Any]]
) -> dict[str, tuple[Decimal, Decimal]] | None:
    """Prices saved on the reservation. None when any leg is still incomplete."""
    fills: dict[str, tuple[Decimal, Decimal]] = {}
    for leg in reserved:
        client_order_id = leg.get("client_order_id")
        if not isinstance(client_order_id, str) or "fill_count" not in leg:
            return None
        count = to_decimal(leg.get("fill_count"))
        if count is None or count < 0:
            return None
        reserved_cents = to_decimal(leg.get("price_cents")) or Decimal(0)
        if count == 0:
            fills[client_order_id] = (Decimal(0), reserved_cents)
            continue
        price = _fill_price_cents(route_id, leg)
        if price is None:
            return None
        fills[client_order_id] = (count, price)
    return fills


@dataclass(frozen=True)
class _HeldMarket:
    ticker: str
    count: Decimal | None
    exposure_dollars: Decimal | None
    close: datetime | None


def _open_position_tickers(market_positions: list[dict[str, Any]]) -> list[str]:
    """Tickers with a non-zero or unreadable position, in first-seen order."""
    tickers: list[str] = []
    seen: set[str] = set()
    for row in market_positions:
        if not isinstance(row, dict):
            continue
        count = to_decimal(row.get("position_fp"))
        if count == 0:
            continue
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker or ticker in seen:
            continue
        seen.add(ticker)
        tickers.append(ticker)
    return tickers


def _import_skip_reason(legs: list[_HeldMarket]) -> str | None:
    """Why an event cannot be imported, or None when every leg is usable.

    A zero leg is usable on its own. A non-zero leg still needs a readable
    count, a positive exposure, and a close time. A close already in the past
    does not skip the event: a later leg can still be open.
    """
    seen: set[str] = set()
    for leg in legs:
        if leg.ticker in seen:
            return f"{leg.ticker} duplicate ticker"
        seen.add(leg.ticker)
        if leg.count is None:
            return f"{leg.ticker} count unreadable"
        if leg.count == 0:
            continue
        if leg.exposure_dollars is None or leg.exposure_dollars <= 0:
            return f"{leg.ticker} exposure unreadable"
        if leg.close is None:
            return f"{leg.ticker} close is missing"
    return None


def _route_for_counts(counts: list[Decimal]) -> str | None:
    """Buy route for a uniform YES or NO book. Mixed signs have no route."""
    if all(count > 0 for count in counts):
        return "buy_yes"
    if all(count < 0 for count in counts):
        return "buy_no"
    return None


def _position_from_held(legs: list[_HeldMarket]) -> dict[str, Any]:
    """Ledger position for one event, including legs that filled nothing.

    Route and capital come only from the non-zero legs, so an empty leg does
    not clear the route or block completion.
    """
    ordered = sorted(legs, key=lambda leg: leg.ticker)
    counts: list[Decimal] = []
    stored: list[dict[str, Any]] = []
    capital = Decimal(0)
    closes: list[datetime] = []
    for leg in ordered:
        count = leg.count
        if count is None:
            raise ValueError(f"incomplete held market {leg.ticker}")
        if count == 0:
            stored.append(
                {
                    "ticker": leg.ticker,
                    "filled": _decimal_text(Decimal(0)),
                    "lots": [],
                }
            )
            continue
        exposure = leg.exposure_dollars
        close = leg.close
        if exposure is None or close is None:
            raise ValueError(f"incomplete held market {leg.ticker}")
        filled = abs(count)
        price = (exposure / filled * CENTS_PER_DOLLAR).quantize(CENT_QUANTUM)
        stored.append(
            {
                "ticker": leg.ticker,
                "filled": _decimal_text(filled),
                "lots": [
                    {
                        "price_cents": _decimal_text(price),
                        "count": _decimal_text(filled),
                    }
                ],
            }
        )
        counts.append(count)
        capital += exposure * CENTS_PER_DOLLAR
        closes.append(close)
    if not closes:
        raise ValueError("no held market with a close")
    return {
        "route": _route_for_counts(counts),
        "close_time": max(closes).isoformat(timespec="seconds"),
        "filled_capital_cents": _decimal_text(capital),
        "legs": stored,
    }


def _event_tickers_in(markets_by_ticker: dict[str, dict[str, Any]]) -> list[str]:
    """Event tickers in first-seen order."""
    tickers: list[str] = []
    seen: set[str] = set()
    for market in markets_by_ticker.values():
        event_ticker = market.get("event_ticker")
        if not isinstance(event_ticker, str) or not event_ticker or event_ticker in seen:
            continue
        seen.add(event_ticker)
        tickers.append(event_ticker)
    return tickers


def _merge_event_markets(
    markets_by_ticker: dict[str, dict[str, Any]],
    event_ticker: str,
    markets: list[dict[str, Any]],
) -> None:
    """Add an event's markets that the position lookup did not already return."""
    for market in markets:
        if not isinstance(market, dict):
            continue
        ticker = market.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            continue
        if not isinstance(market.get("event_ticker"), str) or not market.get("event_ticker"):
            market = dict(market)
            market["event_ticker"] = event_ticker
        existing = markets_by_ticker.get(ticker)
        if existing is None:
            markets_by_ticker[ticker] = market
            continue
        if not isinstance(existing.get("event_ticker"), str) or not existing.get("event_ticker"):
            existing["event_ticker"] = event_ticker


def _position_row_count(row: dict[str, Any] | None) -> Decimal | None:
    """Position size for one market. Missing rows are empty legs, not unreadable."""
    if row is None:
        return Decimal(0)
    return to_decimal(row.get("position_fp"))


def positions_from_kalshi(
    market_positions: list[dict[str, Any]],
    markets_by_ticker: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Local position ledger from Kalshi market positions.

    An event with any non-zero position stores every market on that event,
    including markets with no position. A non-zero leg skips the whole event
    when its count is unreadable, its exposure is not positive, or its close
    is missing. A close already in the past does not. A zero leg does not.
    Markets that cannot be tied to an event are skipped on their own. Events
    with no non-zero leg are not imported. The stored close is the latest
    close among the non-zero legs.
    """
    notes: list[str] = []
    held_by_ticker: dict[str, dict[str, Any]] = {}
    for row in market_positions:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            if _position_row_count(row) != 0:
                notes.append("skipped a position with no ticker")
            continue
        held_by_ticker[ticker] = row

    markets_by_event: dict[str, list[dict[str, Any]]] = {}
    for ticker, market in markets_by_ticker.items():
        if not isinstance(market, dict):
            continue
        event_ticker = market.get("event_ticker")
        if not isinstance(event_ticker, str) or not event_ticker:
            if _position_row_count(held_by_ticker.get(ticker)) != 0:
                notes.append(f"skipped {ticker}: no event_ticker")
            continue
        markets_by_event.setdefault(event_ticker, []).append(market)
    for ticker, row in held_by_ticker.items():
        if ticker in markets_by_ticker:
            continue
        if _position_row_count(row) != 0:
            notes.append(f"skipped {ticker}: no event_ticker")

    positions: dict[str, Any] = {}
    for event_ticker, markets in markets_by_event.items():
        legs: list[_HeldMarket] = []
        any_open = False
        for market in markets:
            ticker = market.get("ticker")
            if not isinstance(ticker, str) or not ticker:
                continue
            row = held_by_ticker.get(ticker)
            count = _position_row_count(row)
            if count is None or count != 0:
                any_open = True
            if count == 0:
                exposure = Decimal(0)
            else:
                exposure = (
                    to_decimal(row.get("market_exposure_dollars"))
                    if row is not None
                    else None
                )
            legs.append(
                _HeldMarket(
                    ticker=ticker,
                    count=count,
                    exposure_dollars=exposure,
                    close=parse_utc(market.get("close_time")),
                )
            )
        if not any_open:
            continue
        reason = _import_skip_reason(legs)
        if reason is not None:
            notes.append(f"skipped {event_ticker}: {reason}")
            continue
        positions[event_ticker] = _position_from_held(legs)
    return positions, notes


def _in_flight_event(reservation: Any) -> str | None:
    if not isinstance(reservation, dict):
        return None
    event_ticker = reservation.get("event_ticker")
    return event_ticker if isinstance(event_ticker, str) and event_ticker else None


def _held_rows_by_ticker(
    market_positions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Open position rows keyed by ticker. Rows with no ticker are omitted."""
    held: dict[str, dict[str, Any]] = {}
    for row in market_positions:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        if isinstance(ticker, str) and ticker:
            held[ticker] = row
    return held


def _stored_leg_tickers(position: dict[str, Any]) -> list[str]:
    legs = position.get("legs")
    if not isinstance(legs, list):
        return []
    tickers: list[str] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        ticker = leg.get("ticker")
        if isinstance(ticker, str) and ticker:
            tickers.append(ticker)
    return tickers


def _stored_legs_flat(
    position: dict[str, Any],
    held_by_ticker: dict[str, dict[str, Any]],
) -> bool | None:
    """True when every stored leg is zero or absent from the open book.

    None when a returned count is unreadable, or when the row has no tickers
    to check. The positions endpoint lists only non-zero markets, so a ticker
    missing from that book is zero.
    """
    tickers = _stored_leg_tickers(position)
    if not tickers:
        return None
    for ticker in tickers:
        row = held_by_ticker.get(ticker)
        if row is None:
            continue
        count = to_decimal(row.get("position_fp"))
        if count is None:
            return None
        if count != 0:
            return False
    return True


def _remember_later_close(position: dict[str, Any], close_text: str | None) -> bool:
    """Move the ledger close later. An earlier timestamp is left in place."""
    latest = parse_utc(close_text)
    if latest is None:
        return False
    current = parse_utc(position.get("close_time"))
    if current is not None and latest <= current:
        return False
    position["close_time"] = latest.isoformat(timespec="seconds")
    return True


def _load_position_markets(
    client: KalshiReadOnlyClient,
    rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Markets for open positions, plus each event's other legs.

    One event whose market list cannot be loaded keeps only the markets
    already resolved. A failure of the ticker lookup itself propagates.
    """
    markets: list[dict[str, Any]] = []
    tickers = _open_position_tickers(rows)
    for start in range(0, len(tickers), MARKET_LOOKUP_BATCH):
        markets.extend(client.get_markets(tickers[start : start + MARKET_LOOKUP_BATCH]))
    by_ticker = {
        market["ticker"]: market
        for market in markets
        if isinstance(market, dict) and isinstance(market.get("ticker"), str)
    }
    fetch_notes: list[str] = []
    for event_ticker in _event_tickers_in(by_ticker):
        try:
            siblings = client.get_event_markets(event_ticker)
        except Exception as exc:
            fetch_notes.append(f"{event_ticker}: event markets unavailable ({exc})")
            continue
        if not siblings:
            fetch_notes.append(f"{event_ticker}: event markets unavailable")
            continue
        _merge_event_markets(by_ticker, event_ticker, siblings)
    return by_ticker, fetch_notes


class Trader:
    def __init__(self) -> None:
        self.api = load_api_config()
        self.config = load_trader_config(self.api.environment)
        self.status = StatusFile(self.api.environment, self.config)
        self.universe: Universe | None = None
        self._last_edge_sample: float | None = None
        self._last_reconcile: float | None = None
        self._reconcile_if_due()

    def _reconcile_if_due(self) -> None:
        now_mono = time.monotonic()
        if (
            self._last_reconcile is not None
            and now_mono - self._last_reconcile < RECONCILE_SECONDS
        ):
            return
        self.reconcile_positions()
        self._last_reconcile = time.monotonic()

    def reconcile_positions(self) -> None:
        """Drop events Kalshi shows flat, and import events it still holds.

        Fills and filled capital on a still-open row are left in place. A
        missing row is filled from Kalshi, so an event forgotten at an earlier
        close comes back while any leg is still open. A failed positions read
        does not treat the book as flat: only a row whose stored latest close
        has already passed is dropped, and a missing close stays. The in-flight
        event is left alone either way. Shard cash is read in the same pass.
        A failed balance read keeps the previous figures. A failed positions
        read skips the balance read too.
        """
        in_flight_event = _in_flight_event(self.status.payload.get("in_flight"))
        try:
            with KalshiReadOnlyClient(self.api) as client:
                rows = client.get_positions()
                try:
                    by_ticker, fetch_notes = _load_position_markets(client, rows)
                except Exception as exc:
                    print(f"position reconcile: market lookup failed ({exc})")
                    by_ticker = None
                    fetch_notes = []
                try:
                    balances = client.get_subaccount_balances()
                except Exception as exc:
                    print(f"shard cash: read failed ({exc})")
                    balances = None
        except Exception as exc:
            print(f"position reconcile failed: {exc}")
            dropped = self.status.expire_positions_past_close(keep_event=in_flight_event)
            for event_ticker in dropped:
                print(
                    f"position reconcile: dropped {event_ticker} "
                    "(latest close passed; Kalshi unread)"
                )
            if dropped:
                self.status.write()
            return

        if by_ticker is None:
            built = {}
            notes = []
        else:
            built, notes = positions_from_kalshi(rows, by_ticker)
        for note in [*fetch_notes, *notes]:
            print(f"position reconcile: {note}")
        held = _held_rows_by_ticker(rows)
        local = self.status.payload.get("positions")
        if not isinstance(local, dict):
            local = {}
        kept: dict[str, Any] = {}
        changed = False
        for event_ticker, position in local.items():
            if not isinstance(position, dict):
                changed = True
                continue
            if event_ticker == in_flight_event:
                kept[event_ticker] = position
                continue
            imported = built.get(event_ticker)
            if isinstance(imported, dict):
                latest = imported.get("close_time")
                if _remember_later_close(
                    position, latest if isinstance(latest, str) else None
                ):
                    changed = True
                    print(f"position reconcile: raised close for {event_ticker}")
                kept[event_ticker] = position
                continue
            flat = _stored_legs_flat(position, held)
            if flat is True:
                changed = True
                print(f"position reconcile: dropped {event_ticker} (flat at Kalshi)")
                continue
            kept[event_ticker] = position
        for event_ticker, imported in built.items():
            if event_ticker == in_flight_event or event_ticker in kept:
                continue
            kept[event_ticker] = imported
            changed = True
            print(f"position reconcile: imported {event_ticker}")
        cash_stored = False
        if balances is not None:
            self.status.payload["shard_cash"] = {
                "updated_at_utc": utc_now().isoformat(timespec="seconds"),
                "shards": [
                    {
                        "exchange_index": shard,
                        "cash_cents": format(amount * CENTS_PER_DOLLAR, "f"),
                    }
                    for shard, amount in sorted(balances.items())
                ],
            }
            cash_stored = True
        if changed:
            self.status.payload["positions"] = kept
        if changed or cash_stored:
            self.status.write()

    def _position_legs(self, event_ticker: str) -> list[dict[str, Any]]:
        position = self.status.position(event_ticker)
        if position is None:
            return []
        legs = position.get("legs")
        return [leg for leg in legs if isinstance(leg, dict)] if isinstance(legs, list) else []

    def _leg_filled(self, leg: dict[str, Any]) -> Decimal:
        parsed = to_decimal(leg.get("filled"))
        return parsed if parsed is not None and parsed > 0 else Decimal(0)

    def _balanced(self, event_ticker: str) -> bool:
        legs = self._position_legs(event_ticker)
        if not legs:
            return True
        counts = [self._leg_filled(leg) for leg in legs]
        return all(count == counts[0] for count in counts)

    def _pinned_route(self, event_ticker: str) -> str | None:
        position = self.status.position(event_ticker)
        if position is None:
            return None
        route = position.get("route")
        return route if isinstance(route, str) and route else None

    def _quote_for_event(self, event: dict[str, Any], event_ticker: str, now: datetime) -> BestEdge:
        pinned = self._pinned_route(event_ticker)
        if pinned:
            return quote_route(event, pinned, reference_time=now)
        return quote_best_edge(event, reference_time=now)

    def _fresh_bundles(self, event_ticker: str, quote: BestEdge) -> int:
        return fresh_clip_bundles(
            quote.capital_unnetted_per_bundle,
            quote.bottleneck_bundles,
            self.config.max_capital_per_shot_cents,
            self.status.remaining_cents(event_ticker, self.config),
        )

    def _filled_without_route(self, event_ticker: str) -> bool:
        if self._pinned_route(event_ticker) is not None:
            return False
        return any(self._leg_filled(leg) > 0 for leg in self._position_legs(event_ticker))

    def _stored_legs_are_full_event(self, event_ticker: str) -> bool:
        """True when there is no position, or its tickers are exactly the event's."""
        legs = self._position_legs(event_ticker)
        if not legs:
            return True
        if self.universe is None:
            return False
        event = self.universe.events.get(event_ticker)
        if not isinstance(event, dict):
            return False
        stored: set[str] = set()
        for leg in legs:
            ticker = leg.get("ticker")
            if isinstance(ticker, str) and ticker:
                stored.add(ticker)
        event_tickers: set[str] = set()
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker")
            if isinstance(ticker, str) and ticker:
                event_tickers.add(ticker)
        return bool(event_tickers) and stored == event_tickers

    def fresh_clip_allowed(self, event_ticker: str, quote: BestEdge) -> bool:
        if self.status.payload.get("in_flight"):
            return False
        if not self._stored_legs_are_full_event(event_ticker):
            return False
        if not self._balanced(event_ticker):
            return False
        if self._filled_without_route(event_ticker):
            return False
        return bool(
            quote.route_id
            and quote.annualized_pct is not None
            and quote.annualized_pct >= self.config.min_annualized_pct
            and self._fresh_bundles(event_ticker, quote) >= 1
        )

    def sample_live_edges(self) -> None:
        if self.universe is None:
            return
        now_mono = time.monotonic()
        if (
            self._last_edge_sample is not None
            and now_mono - self._last_edge_sample < SAMPLE_SECONDS
        ):
            return
        self._last_edge_sample = now_mono

        now = utc_now()
        previous = self.status.payload.get("live_edges") or {}
        if not isinstance(previous, dict):
            previous = {}

        live_edges: dict[str, list[dict[str, Any]]] = {}
        for event_ticker, event in self.universe.events.items():
            quote = self._quote_for_event(event, event_ticker, now)
            sample = {
                "time_utc": now.isoformat(timespec="seconds"),
                "route": quote.route_id,
                "net_per_bundle_cents": to_json(quote.net_per_bundle),
                "annualized_pct": to_json(quote.annualized_pct),
                "eligible": self.fresh_clip_allowed(event_ticker, quote),
            }
            prior = previous.get(event_ticker) or []
            if not isinstance(prior, list):
                prior = []
            live_edges[event_ticker] = [sample, *prior][:MAX_EDGE_SAMPLES]
        self.status.payload["live_edges"] = live_edges
        self.status.write()

    def reload_universe(self) -> bool:
        loaded = load_universe(self.api.environment)
        if loaded is None:
            changed = self.universe is not None
            self.universe = None
            self.status.set_connection(False, None)
            return changed
        if self.universe is not None and loaded.signature == self.universe.signature:
            return False
        if self.universe is not None:
            old_markets = {
                market.get("ticker"): market
                for event in self.universe.events.values()
                for market in event.get("markets") or []
                if isinstance(market.get("ticker"), str)
            }
            quote_fields = (
                "yes_bid_cents",
                "yes_ask_cents",
                "no_bid_cents",
                "no_ask_cents",
                "yes_bid_size_contracts",
                "yes_ask_size_contracts",
                "no_bid_size_contracts",
                "no_ask_size_contracts",
                "_size_cap_yes_bid",
                "_size_cap_yes_ask",
            )
            for event in loaded.events.values():
                for market in event.get("markets") or []:
                    previous = old_markets.get(market.get("ticker"))
                    if previous is not None:
                        for field in quote_fields:
                            value = previous.get(field)
                            market[field] = (
                                copy.deepcopy(value) if isinstance(value, dict) else value
                            )
        self.universe = loaded
        self.status.payload["watchlist_id"] = loaded.watchlist_id
        self.status.write()
        print(
            f"watchlist {loaded.watchlist_id}: {len(loaded.events)} events, "
            f"{len(loaded.market_tickers)} markets"
        )
        return True

    def _latest_universe_close(self, tickers: list[str]) -> str | None:
        """Latest per-market close in the loaded universe, not the event close.

        The event close is the earliest leg and stays the gate in ``evaluate``.
        Old candidate files have no per-market close; those return None.
        """
        if self.universe is None:
            return None
        closes: list[datetime] = []
        for ticker in tickers:
            event_ticker = self.universe.market_to_event.get(ticker)
            if event_ticker is None:
                continue
            event = self.universe.events.get(event_ticker)
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict) or market.get("ticker") != ticker:
                    continue
                close = parse_utc(market.get("close_time"))
                if close is not None:
                    closes.append(close)
                break
        if not closes:
            return None
        return max(closes).isoformat(timespec="seconds")

    def _ensure_position(
        self, event_ticker: str, route_id: str, tickers: list[str]
    ) -> dict[str, Any]:
        position = self.status.position(event_ticker)
        if position is None:
            position = {
                "route": None,
                "close_time": None,
                "filled_capital_cents": "0",
                "legs": [],
            }
            self.status.payload["positions"][event_ticker] = position
        by_ticker = {
            leg.get("ticker"): leg
            for leg in position.get("legs") or []
            if isinstance(leg, dict)
        }
        legs: list[dict[str, Any]] = []
        for ticker in tickers:
            leg = by_ticker.get(ticker)
            if leg is None:
                leg = {"ticker": ticker, "filled": "0", "lots": []}
            legs.append(leg)
        position["legs"] = legs
        _remember_later_close(position, self._latest_universe_close(tickers))
        if position.get("route") is None and route_id:
            # Route stays unset until the first non-zero fill.
            position["pending_route"] = route_id
        return position

    def _apply_fills(
        self,
        event_ticker: str,
        route_id: str,
        reserved_legs: list[dict[str, Any]],
        fills: dict[str, tuple[Decimal, Decimal]],
    ) -> Decimal:
        """Record fills. Returns capital charged for this attempt."""
        tickers = [str(leg["ticker"]) for leg in reserved_legs]
        existing = self._position_legs(event_ticker)
        for leg in existing:
            ticker = leg.get("ticker")
            if isinstance(ticker, str) and ticker not in tickers:
                tickers.append(ticker)
        position = self._ensure_position(event_ticker, route_id, tickers)
        charged = Decimal(0)
        any_fill = False
        by_ticker = {leg["ticker"]: leg for leg in position["legs"]}
        for reserved in reserved_legs:
            ticker = str(reserved["ticker"])
            leg = by_ticker[ticker]
            client_order_id = str(reserved["client_order_id"])
            fill_count, fill_price = fills.get(client_order_id, (Decimal(0), Decimal(0)))
            if fill_count <= 0:
                continue
            any_fill = True
            prior = self._leg_filled(leg)
            leg["filled"] = _decimal_text(prior + fill_count)
            lots = leg.get("lots")
            if not isinstance(lots, list):
                lots = []
                leg["lots"] = lots
            lots.append(
                {
                    "price_cents": _decimal_text(fill_price),
                    "count": _decimal_text(fill_count),
                }
            )
            charged += unnetted_capital_cents(route_id, fill_price, fill_count)
        if any_fill and not position.get("route"):
            position["route"] = route_id
        position.pop("pending_route", None)
        already = to_decimal(position.get("filled_capital_cents")) or Decimal(0)
        position["filled_capital_cents"] = _decimal_text(already + charged)
        if not any_fill and all(self._leg_filled(leg) == 0 for leg in position["legs"]):
            self.status.payload["positions"].pop(event_ticker, None)
        return charged

    def _authoritative_fills(
        self, route_id: str, execution: dict[str, Any], reserved: list[dict[str, Any]]
    ) -> dict[str, tuple[Decimal, Decimal]] | None:
        if not execution.get("ok"):
            return None
        orders = execution.get("orders")
        if not isinstance(orders, list):
            return None
        by_id = {
            order.get("client_order_id"): order
            for order in orders
            if isinstance(order, dict)
        }
        fills: dict[str, tuple[Decimal, Decimal]] = {}
        remembered: list[tuple[dict[str, Any], Decimal, Decimal | None]] = []
        for leg in reserved:
            client_order_id = leg["client_order_id"]
            order = by_id.get(client_order_id)
            if not isinstance(order, dict) or order.get("error"):
                return None
            count = _fill_count_of(order)
            if count is None or count < 0:
                return None
            reserved_cents = to_decimal(leg.get("price_cents")) or Decimal(0)
            if count == 0:
                fills[client_order_id] = (Decimal(0), reserved_cents)
                remembered.append((leg, Decimal(0), None))
                continue
            yes = to_decimal(order.get("average_fill_price"))
            price = _fill_price_cents(route_id, order)
            if yes is None or price is None:
                return None
            fills[client_order_id] = (count, price)
            remembered.append((leg, count, yes))
        for leg, count, yes in remembered:
            _remember_yes_fill(leg, count, yes)
        return fills

    def _lookup_fills(
        self, route_id: str, reserved: list[dict[str, Any]]
    ) -> dict[str, tuple[Decimal, Decimal]] | None:
        try:
            with KalshiOrderClient(self.api) as client:
                found = client.lookup_by_client_order_ids(reserved)
                if found is None:
                    return None
                fills: dict[str, tuple[Decimal, Decimal]] = {}
                remembered: list[tuple[dict[str, Any], Decimal, Decimal | None]] = []
                for leg in reserved:
                    client_order_id = str(leg["client_order_id"])
                    reserved_cents = to_decimal(leg.get("price_cents")) or Decimal(0)
                    order = found.get(client_order_id)
                    if order is None:
                        fills[client_order_id] = (Decimal(0), reserved_cents)
                        remembered.append((leg, Decimal(0), None))
                        continue
                    count = _fill_count_of(order)
                    if count is None or count < 0:
                        return None
                    if count == 0:
                        fills[client_order_id] = (Decimal(0), reserved_cents)
                        remembered.append((leg, Decimal(0), None))
                        continue
                    order_id = order.get("order_id")
                    if not isinstance(order_id, str) or not order_id:
                        return None
                    raw_fills = client.lookup_fills_by_order_id(order_id)
                    if raw_fills is None:
                        return None
                    yes = _yes_vwap_dollars(raw_fills, count)
                    if yes is None:
                        return None
                    price = _fill_price_cents(route_id, {"average_fill_price": yes})
                    if price is None:
                        return None
                    fills[client_order_id] = (count, price)
                    remembered.append((leg, count, yes))
        except Exception as exc:
            print(f"order lookup failed: {exc}")
            return None
        for leg, count, yes in remembered:
            _remember_yes_fill(leg, count, yes)
        return fills

    def resolve_in_flight(self) -> bool:
        """Apply a saved reservation. False means a live lookup is still unknown."""
        reservation = self.status.payload.get("in_flight")
        if not isinstance(reservation, dict):
            return True
        if reservation.get("dry_mode") is True:
            self.status.payload["in_flight"] = None
            self.status.write()
            return True
        route_id = reservation.get("route")
        event_ticker = reservation.get("event_ticker")
        close_text = reservation.get("close_time")
        legs = reservation.get("legs")
        if (
            not isinstance(route_id, str)
            or not isinstance(event_ticker, str)
            or not isinstance(close_text, str)
            or not isinstance(legs, list)
        ):
            self.status.payload["in_flight"] = None
            self.status.write()
            return True
        reserved = [leg for leg in legs if isinstance(leg, dict)]
        fills = _fills_from_saved(route_id, reserved)
        if fills is None:
            fills = self._lookup_fills(route_id, reserved)
        if fills is None:
            return False
        self.status.write()
        charged = self._apply_fills(event_ticker, route_id, reserved, fills)
        self.status.payload["in_flight"] = None
        self.status.write()
        print(
            f"resolved in-flight {event_ticker} {route_id} "
            f"filled_capital_cents={_decimal_text(charged)}"
        )
        return True

    def _reserve(
        self,
        event_ticker: str,
        route_id: str,
        close_text: str,
        orders: list[dict[str, Any]],
        *,
        kind: str,
    ) -> None:
        self.status.payload["in_flight"] = {
            "event_ticker": event_ticker,
            "route": route_id,
            "dry_mode": self.config.dry_mode,
            "close_time": close_text,
            "kind": kind,
            "legs": [
                {
                    "ticker": order["ticker"],
                    "side": order["side"],
                    "count": _decimal_text(Decimal(order["count"])),
                    "price_cents": _decimal_text(Decimal(order["price_cents"])),
                    "client_order_id": order["client_order_id"],
                }
                for order in orders
            ],
        }
        self.status.write()

    def _send_reserved(
        self,
        event: dict[str, Any],
        event_ticker: str,
        route_id: str,
        close_text: str,
        orders: list[dict[str, Any]],
        *,
        kind: str,
        annualized: Decimal | None,
        bundles: int | None,
        expected_pnl: Decimal | None,
        expected_capital: Decimal | None,
    ) -> bool:
        self._reserve(event_ticker, route_id, close_text, orders, kind=kind)
        reserved = self.status.payload["in_flight"]["legs"]
        now = utc_now()
        close = parse_utc(close_text) or now
        lock_seconds = max(int((close - now).total_seconds()), 0)
        trade: dict[str, Any] = {
            "event_ticker": event_ticker,
            "title": event.get("title"),
            "route": route_id,
            "kind": kind,
            "time_utc": now.isoformat(timespec="seconds"),
            "close_time_utc": close_text,
            "lockup_seconds": lock_seconds,
            "lockup_text": short_duration(lock_seconds),
            "annualized_pct": to_json(annualized),
            "bundles": bundles,
            "expected_pnl_cents": to_json(expected_pnl),
            "expected_capital_locked_cents": to_json(expected_capital),
            "mode": "dry" if self.config.dry_mode else "live",
            "legs": [
                {
                    "ticker": order["ticker"],
                    "count": _decimal_text(Decimal(order["count"])),
                    "client_order_id": order["client_order_id"],
                }
                for order in orders
            ],
        }
        if self.config.dry_mode:
            self.status.payload["in_flight"] = None
            self.status.add_trade(trade)
            print(f"DRY {event_ticker} {route_id} {kind} x{len(orders)} (no POST)")
            return False

        execution: dict[str, Any]
        try:
            with KalshiOrderClient(self.api) as client:
                execution = client.create_batch_ioc(orders)
        except Exception as exc:
            execution = {"ok": False, "error": str(exc), "orders": []}

        fills = self._authoritative_fills(route_id, execution, reserved)
        if fills is None:
            fills = self._lookup_fills(route_id, reserved)
        if fills is None:
            trade["execution_ok"] = False
            trade["error"] = execution.get("error") or "fill unknown; reservation kept"
            _attach_batch_reply(trade, execution)
            self.status.add_trade(trade)
            print(f"LIVE {event_ticker} {route_id} {kind} unresolved; not sending again")
            return False

        self.status.write()
        charged = self._apply_fills(event_ticker, route_id, reserved, fills)
        self.status.payload["in_flight"] = None
        trade["execution_ok"] = execution.get("ok")
        trade["filled_capital_cents"] = _decimal_text(charged)
        trade["legs"] = [
            {
                "ticker": leg["ticker"],
                "client_order_id": leg["client_order_id"],
                "fill_count": _decimal_text(fills[leg["client_order_id"]][0]),
            }
            for leg in reserved
        ]
        if execution.get("error"):
            trade["error"] = execution["error"]
        _attach_batch_reply(trade, execution)
        self.status.add_trade(trade)
        print(
            f"LIVE {event_ticker} {route_id} {kind} "
            f"filled_capital_cents={_decimal_text(charged)}"
        )
        still_shown = self._shrink_shown_top(
            event, event_ticker, route_id, reserved, fills
        )
        if kind != "fresh":
            return False
        return still_shown

    def _shrink_shown_top(
        self,
        event: dict[str, Any],
        event_ticker: str,
        route_id: str,
        reserved: list[dict[str, Any]],
        fills: dict[str, tuple[Decimal, Decimal]],
    ) -> bool:
        """Reduce the shown top by live fills. True when that same price still has size."""
        side = _book_side(route_id)
        price_key = _price_key(side)
        primary, mirror = _size_keys(side)
        cap_key = _size_cap_key(side)
        by_ticker = {
            market.get("ticker"): market
            for market in event.get("markets") or []
            if isinstance(market, dict)
        }
        saw_fill = False
        for leg in reserved:
            fill_count = fills.get(str(leg.get("client_order_id")), (Decimal(0), Decimal(0)))[0]
            if fill_count <= 0:
                continue
            saw_fill = True
            market = by_ticker.get(leg.get("ticker"))
            if not isinstance(market, dict):
                continue
            current = to_decimal(market.get(primary)) or Decimal(0)
            remaining = current - fill_count
            if remaining < 0:
                remaining = Decimal(0)
            market[primary] = to_json(remaining)
            market[mirror] = to_json(remaining)
            market[cap_key] = {
                "price": to_json(to_decimal(market.get(price_key))),
                "size": to_json(remaining),
            }
        if not saw_fill or not self._balanced(event_ticker):
            return False
        markets = [market for market in event.get("markets") or [] if isinstance(market, dict)]
        if not markets:
            return False
        for market in markets:
            size = to_decimal(market.get(primary))
            price = to_decimal(market.get(price_key))
            if price is None or size is None or size <= 0:
                return False
        return True

    def _shown_signature(
        self, event_ticker: str
    ) -> tuple[str, tuple[str | None, ...], tuple[str | None, ...]] | None:
        if self.universe is None:
            return None
        event = self.universe.events.get(event_ticker)
        if event is None:
            return None
        quote = self._quote_for_event(event, event_ticker, utc_now())
        if quote.route_id is None:
            return None
        return (
            quote.route_id,
            tuple(to_json(price) for price in quote.prices),
            tuple(to_json(size) for size in quote.sizes),
        )

    def _completion_orders(
        self, event: dict[str, Any], event_ticker: str, route_id: str
    ) -> tuple[list[dict[str, Any]], Decimal, Decimal] | None:
        legs = self._position_legs(event_ticker)
        if not legs:
            return None
        counts = [self._leg_filled(leg) for leg in legs]
        target = max(counts)
        if target <= 0 or all(count == target for count in counts):
            return None
        prices = route_quote_prices(event, route_id)
        markets = [
            market
            for market in event.get("markets") or []
            if isinstance(market, dict) and isinstance(market.get("ticker"), str)
        ]
        price_by_ticker = {
            market["ticker"]: price
            for market, price in zip(markets, prices or [], strict=False)
        }
        order_counts: dict[str, Decimal] = {}
        order_prices: dict[str, Decimal] = {}
        package: list[list[tuple[Decimal, Decimal]]] = []
        additional = Decimal(0)
        for leg in legs:
            ticker = leg.get("ticker")
            if not isinstance(ticker, str):
                return None
            filled = self._leg_filled(leg)
            shortfall = target - filled
            lots: list[tuple[Decimal, Decimal]] = []
            for lot in leg.get("lots") or []:
                if not isinstance(lot, dict):
                    continue
                price = to_decimal(lot.get("price_cents"))
                count = to_decimal(lot.get("count"))
                if price is None or count is None or count <= 0:
                    continue
                lots.append((price, count))
            if shortfall > 0:
                current = price_by_ticker.get(ticker)
                if current is None:
                    return None
                lots.append((current, shortfall))
                order_counts[ticker] = shortfall
                order_prices[ticker] = current
                additional += unnetted_capital_cents(route_id, current, shortfall)
            package.append(lots)
        if additional > self.status.remaining_cents(event_ticker, self.config):
            return None
        net = completion_package_net(route_id, len(legs), package)
        if net is None or net < 0:
            return None
        orders = orders_for_counts(event, route_id, order_counts, order_prices)
        if len(orders) != len(order_counts):
            return None
        return orders, net, additional

    def evaluate(self, event_ticker: str) -> bool:
        """Price one game and send at most one clip.

        True only when a live fresh fill left a smaller size at the same prices
        and another fresh clip is still possible. Callers that walk a stale
        quote use that to take the remainder. A ticker check ignores it.
        """
        if self.status.payload.get("in_flight"):
            self.resolve_in_flight()
            return False
        if self.universe is None:
            return False
        event = self.universe.events.get(event_ticker)
        if event is None:
            return False
        now = utc_now()
        close = parse_utc(event.get("close_time"))
        if close is None or close <= now:
            return False
        close_text = close.isoformat(timespec="seconds")
        pinned = self._pinned_route(event_ticker)
        if not self._balanced(event_ticker):
            if pinned is None:
                return False
            completed = self._completion_orders(event, event_ticker, pinned)
            if completed is None:
                return False
            orders, expected_pnl, expected_capital = completed
            self._send_reserved(
                event,
                event_ticker,
                pinned,
                close_text,
                orders,
                kind="completion",
                annualized=None,
                bundles=None,
                expected_pnl=expected_pnl,
                expected_capital=expected_capital,
            )
            return False

        quote = self._quote_for_event(event, event_ticker, now)
        if not self.fresh_clip_allowed(event_ticker, quote) or quote.route_id is None:
            return False
        bundles = self._fresh_bundles(event_ticker, quote)
        markets = event.get("markets") or []
        if len(markets) != len(quote.prices):
            return False
        counts = {
            market["ticker"]: Decimal(bundles)
            for market in markets
            if isinstance(market, dict) and isinstance(market.get("ticker"), str)
        }
        prices = {
            market["ticker"]: price
            for market, price in zip(markets, quote.prices, strict=True)
            if isinstance(market, dict) and isinstance(market.get("ticker"), str)
        }
        orders = orders_for_counts(event, quote.route_id, counts, prices)
        if len(orders) != len(quote.prices):
            return False
        economics = fresh_clip_economics(quote.route_id, quote.prices, bundles)
        if economics is None:
            return False
        expected_pnl, expected_capital = economics
        return self._send_reserved(
            event,
            event_ticker,
            quote.route_id,
            close_text,
            orders,
            kind="fresh",
            annualized=quote.annualized_pct,
            bundles=bundles,
            expected_pnl=expected_pnl,
            expected_capital=expected_capital,
        )

    def walk_shown_top(self, event_ticker: str) -> None:
        """Take the current top, then a smaller remainder at that same price.

        Stops when the shot does not fill, the price changes, the shown size is
        gone, or the existing gates refuse another clip. Does not invent the
        next price behind the top.
        """
        while True:
            before = self._shown_signature(event_ticker)
            if not self.evaluate(event_ticker):
                return
            after = self._shown_signature(event_ticker)
            if after is None or before is None:
                return
            if after[0] != before[0] or after[1] != before[1] or after[2] == before[2]:
                return

    def scan_watchlist(self) -> None:
        """Check every current watchlist game from the quotes already in memory."""
        if self.universe is None:
            return
        for event_ticker in list(self.universe.events):
            if self.status.payload.get("in_flight") and not self.resolve_in_flight():
                return
            try:
                self.walk_shown_top(event_ticker)
            except Exception as exc:
                print(f"evaluate {event_ticker} failed: {exc}")
            if self.status.payload.get("in_flight"):
                return

    async def run(self) -> None:
        self.resolve_in_flight()
        backoff = 1
        while True:
            self._reconcile_if_due()
            changed = self.reload_universe()
            if changed:
                self.scan_watchlist()
            if self.universe is None or not self.universe.market_tickers:
                await asyncio.sleep(POLL_SECONDS)
                continue

            socket = KalshiTickerWebSocket(self.api, self.config)
            try:
                await socket.connect(self.universe.market_tickers)
                self.status.set_connection(True, self.universe.watchlist_id)
                self._last_edge_sample = time.monotonic()
                print(f"WebSocket connected ({self.api.environment})")
                backoff = 1
                next_scan = time.monotonic() + POLL_SECONDS
                while True:
                    timeout = max(next_scan - time.monotonic(), 0.0)
                    try:
                        payload = await asyncio.wait_for(socket.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        payload = {}
                    message_type = payload.get("type")
                    if message_type == "error":
                        raise RuntimeError(f"WebSocket error: {payload.get('msg')}")
                    if self.status.payload.get("in_flight"):
                        self.resolve_in_flight()
                    elif message_type == "ticker" and self.universe is not None:
                        message = payload.get("msg")
                        if isinstance(message, dict):
                            event_ticker = patch_ticker(self.universe, message)
                            if event_ticker:
                                try:
                                    self.evaluate(event_ticker)
                                except Exception as exc:
                                    print(f"evaluate {event_ticker} failed: {exc}")
                    if time.monotonic() >= next_scan:
                        self.scan_watchlist()
                        next_scan = time.monotonic() + POLL_SECONDS
                    self._reconcile_if_due()
                    self.sample_live_edges()

                    previous = self.universe
                    changed = self.reload_universe()
                    if changed:
                        if self.universe is None:
                            break
                        await socket.update_markets(self.universe.market_tickers)
                        self.status.set_connection(True, self.universe.watchlist_id)
                        self.scan_watchlist()
                        next_scan = time.monotonic() + POLL_SECONDS
                    elif previous is None:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.set_connection(
                    False, self.universe.watchlist_id if self.universe else None
                )
                print(f"WebSocket disconnected: {exc}; retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                await socket.close()
                self.status.set_connection(
                    False, self.universe.watchlist_id if self.universe else None
                )


def main() -> int:
    enable_utf8_stdout()
    try:
        trader = Trader()
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 1
    mode = "DRY" if trader.config.dry_mode else "LIVE"
    print(
        f"Trader starting: {mode}, {trader.api.environment}, "
        f"minimum {trader.config.min_annualized_pct}% annualized, "
        f"${trader.config.max_capital_per_event_dollars} per event, "
        f"${trader.config.max_capital_per_shot_dollars} per shot"
    )
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
