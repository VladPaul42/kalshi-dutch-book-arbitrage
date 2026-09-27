"""Place orders on the Kalshi Trade API.

This is the write sibling of `client.py`. `filter_candidates.py` never imports
it, so the filter path cannot place an order. Only `trade.py` imports this
module; the dashboard never does.

POST is never retried: a second attempt with a new client_order_id would be a
second order. A lost response is resolved by reading the saved client_order_ids.
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any
from urllib.parse import quote

from .client import (
    KalshiAuthError,
    KalshiError,
    KalshiSignedSession,
    primary_shard_balances,
)
from .config import ApiConfig
from .fixedpoint import DOLLAR_QUANTUM, to_decimal
from .mispricing import _taker_fee_cents

logger = logging.getLogger("kalshi.orders")

CREATE_ORDER_PATH = "/portfolio/events/orders"
BATCH_ORDERS_PATH = "/portfolio/events/orders/batched"
LIST_ORDERS_PATH = "/portfolio/orders"
FILLS_PATH = "/portfolio/fills"
BALANCES_PATH = "/portfolio/subaccounts/balances"
MARKETS_PATH = "/markets"
TRANSFER_PATH = "/portfolio/intra_exchange_instance_transfer"
_LOOKUP_PAGE_LIMIT = 200
_FILLS_PAGE_LIMIT = 100
_LOOKUP_MAX_PAGES = 20
_CENTICENTS_PER_DOLLAR = Decimal(10000)
_CENTICENTS_PER_CENT = Decimal(100)
_TRANSFER_POLL_SECONDS = 0.25
_TRANSFER_TIMEOUT_SECONDS = 10


def _order_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    orders = body.get("orders")
    if isinstance(orders, list):
        return [row for row in orders if isinstance(row, dict)]
    if isinstance(body.get("ticker"), str):
        return [body]
    return []


def _order_cost_centicents(row: dict[str, Any]) -> int | None:
    """Cash the order posts, plus the existing taker-fee ceiling, in centicents."""
    side = row.get("side")
    price = to_decimal(row.get("price"))
    count = to_decimal(row.get("count"))
    if side not in ("bid", "ask") or price is None or count is None or count <= 0:
        return None
    if side == "bid":
        dollars = price * count
    else:
        dollars = (Decimal(1) - price) * count
    if dollars < 0:
        return None
    collateral = (dollars * _CENTICENTS_PER_DOLLAR).to_integral_value(
        rounding=ROUND_CEILING
    )
    fee_cents = _taker_fee_cents(price * Decimal(100), count)
    fee = (fee_cents * _CENTICENTS_PER_CENT).to_integral_value(rounding=ROUND_CEILING)
    return int(collateral + fee)


def _plan_transfers(
    available: dict[int, int], required: dict[int, int]
) -> list[tuple[int, int, int]] | None:
    """Donor moves that cover every short shard. None when the cash is not there.

    A shard's own order cost stays reserved, so it is not a donor. No transfer
    is returned when nothing is short.
    """
    free = {
        shard: available.get(shard, 0) - required.get(shard, 0)
        for shard in set(available) | set(required)
    }
    transfers: list[tuple[int, int, int]] = []
    for dest, need in required.items():
        short = need - available.get(dest, 0)
        if short <= 0:
            continue
        donors = sorted(
            (shard for shard, amount in free.items() if shard != dest and amount > 0),
            key=lambda shard: free[shard],
            reverse=True,
        )
        for donor in donors:
            if short <= 0:
                break
            move = min(short, free[donor])
            if move <= 0:
                continue
            transfers.append((donor, dest, move))
            free[donor] -= move
            short -= move
        if short > 0:
            return None
    return transfers


class KalshiOrderClient:
    """Signed V2 client for IOC orders and atomic basket submission."""

    def __init__(self, config: ApiConfig) -> None:
        self._config = config
        self._session = KalshiSignedSession(config, require_credentials=True)

    def __enter__(self) -> "KalshiOrderClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    @staticmethod
    def _ioc_body(
        ticker: str,
        side: str,
        price_dollars: Decimal,
        count: Decimal,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if side not in ("bid", "ask"):
            raise ValueError("side must be 'bid' or 'ask'")
        return {
            "ticker": ticker,
            "side": side,
            "count": format(count.normalize(), "f"),
            "price": format(price_dollars.quantize(DOLLAR_QUANTUM), "f"),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }

    def create_ioc(
        self,
        ticker: str,
        side: str,
        price_dollars: Decimal,
        *,
        count: Decimal = Decimal(1),
    ) -> dict[str, Any]:
        """POST one immediate-or-cancel V2 order. Never retries."""
        body = self._ioc_body(ticker, side, price_dollars, count)
        return self._post(CREATE_ORDER_PATH, body, ticker=ticker)

    def create_sell_yes_ioc(
        self,
        ticker: str,
        price_dollars: Decimal,
        *,
        count: Decimal = Decimal(1),
    ) -> dict[str, Any]:
        """Backward-compatible wrapper for the original single-order API."""
        return self.create_ioc(ticker, "ask", price_dollars, count=count)

    def create_batch_ioc(self, orders: list[dict[str, Any]]) -> dict[str, Any]:
        """Send all basket legs in one batch POST. The POST is never retried.

        Input rows require ``ticker``, ``side``, ``price_dollars`` and ``count``.
        The returned ``orders`` list is positionally aligned when Kalshi returns
        one result per request; absent fields remain ``None``.
        """
        bodies = [
            self._ioc_body(
                str(order["ticker"]),
                str(order["side"]),
                Decimal(order["price_dollars"]),
                Decimal(order["count"]),
                str(order["client_order_id"]) if order.get("client_order_id") else None,
            )
            for order in orders
        ]
        result = self._post(BATCH_ORDERS_PATH, {"orders": bodies}, ticker="basket")
        result["request_orders"] = bodies
        parsed = result.pop("_parsed", None)
        response_orders: Any = None
        if isinstance(parsed, dict):
            response_orders = parsed.get("orders") or parsed.get("responses")
        aligned: list[dict[str, Any]] = []
        saw_leg = False
        for index, request in enumerate(bodies):
            response = (
                response_orders[index]
                if isinstance(response_orders, list) and index < len(response_orders)
                and isinstance(response_orders[index], dict)
                else None
            )
            if isinstance(response, dict):
                saw_leg = True
            payload = response if isinstance(response, dict) else {}
            inner = payload.get("order") if isinstance(payload.get("order"), dict) else payload
            aligned.append(
                {
                    "ticker": request["ticker"],
                    "client_order_id": inner.get(
                        "client_order_id", request["client_order_id"]
                    ),
                    "order_id": inner.get("order_id"),
                    "fill_count": inner.get("fill_count"),
                    "remaining_count": inner.get("remaining_count"),
                    "average_fill_price": inner.get("average_fill_price"),
                    "error": payload.get("error") or inner.get("error") or payload.get("message"),
                    "response": response,
                }
            )
        result["orders"] = aligned
        if not saw_leg and parsed is not None:
            result["response_body"] = parsed
        return result

    def lookup_by_client_order_ids(
        self, legs: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]] | None:
        """Find saved orders. ``None`` when the read fails.

        A successful result omits ids Kalshi does not have. Those are zero
        fills. This never POSTs.
        """
        wanted: dict[str, str] = {}
        for leg in legs:
            client_order_id = leg.get("client_order_id")
            ticker = leg.get("ticker")
            if isinstance(client_order_id, str) and isinstance(ticker, str):
                wanted[client_order_id] = ticker
        if not wanted:
            return {}
        found: dict[str, dict[str, Any]] = {}
        try:
            for ticker in sorted(set(wanted.values())):
                ids = {cid for cid, owner in wanted.items() if owner == ticker}
                cursor: str | None = None
                for _page in range(_LOOKUP_MAX_PAGES):
                    params: dict[str, Any] = {
                        "ticker": ticker,
                        "limit": _LOOKUP_PAGE_LIMIT,
                    }
                    if cursor:
                        params["cursor"] = cursor
                    response = self._session.send("GET", LIST_ORDERS_PATH, params=params)
                    if response.status_code != 200:
                        logger.info(
                            "GET %s ticker=%s status=%s",
                            LIST_ORDERS_PATH,
                            ticker,
                            response.status_code,
                        )
                        return None
                    try:
                        payload = response.json()
                    except ValueError:
                        return None
                    if not isinstance(payload, dict):
                        return None
                    orders = payload.get("orders")
                    if not isinstance(orders, list):
                        return None
                    for order in orders:
                        if not isinstance(order, dict):
                            continue
                        client_order_id = order.get("client_order_id")
                        if client_order_id in ids:
                            found[str(client_order_id)] = order
                    if ids.issubset(found):
                        break
                    next_cursor = payload.get("cursor")
                    if not isinstance(next_cursor, str) or not next_cursor:
                        break
                    cursor = next_cursor
        except Exception as exc:
            logger.info("GET %s failed: %s", LIST_ORDERS_PATH, exc)
            return None
        return found

    def lookup_fills_by_order_id(self, order_id: str) -> list[dict[str, Any]] | None:
        """Fills for one order. ``None`` when the read fails or a page is left unread.

        This never POSTs. An empty list means Kalshi returned no fills.
        """
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        for _page in range(_LOOKUP_MAX_PAGES):
            params: dict[str, Any] = {
                "order_id": order_id,
                "limit": _FILLS_PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._get_json(FILLS_PATH, params=params)
            if payload is None:
                return None
            rows = payload.get("fills")
            if not isinstance(rows, list):
                return None
            for row in rows:
                if not isinstance(row, dict):
                    return None
                row_order = row.get("order_id")
                if isinstance(row_order, str) and row_order != order_id:
                    return None
                found.append(row)
            next_cursor = payload.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return found
            cursor = next_cursor
        return None

    def _get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        try:
            response = self._session.send("GET", path, params=params)
        except Exception as exc:
            logger.info("GET %s failed: %s", path, exc)
            return None
        if response.status_code != 200:
            logger.info("GET %s status=%s", path, response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _market_shards(self, tickers: list[str]) -> dict[str, int] | None:
        payload = self._get_json(MARKETS_PATH, params={"tickers": ",".join(tickers)})
        if payload is None:
            return None
        markets = payload.get("markets")
        if not isinstance(markets, list):
            return None
        shards: dict[str, int] = {}
        for market in markets:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker")
            index = market.get("exchange_index")
            if (
                isinstance(ticker, str)
                and isinstance(index, int)
                and not isinstance(index, bool)
            ):
                shards[ticker] = index
        return shards

    def _primary_balances(self) -> dict[int, int] | None:
        """Available centicents on subaccount 0, one entry per exchange shard."""
        balances = primary_shard_balances(self._get_json(BALANCES_PATH))
        if balances is None:
            return None
        return {
            shard: int(
                (amount * _CENTICENTS_PER_DOLLAR).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            for shard, amount in balances.items()
        }

    def _start_transfer(self, source: int, dest: int, amount: int) -> str | None:
        if source == dest or amount <= 0:
            return None
        body = {
            "source": "event_contract",
            "destination": "event_contract",
            "amount": amount,
            "source_exchange_shard": source,
            "destination_exchange_shard": dest,
            "source_subaccount": 0,
            "destination_subaccount": 0,
        }
        try:
            response = self._session.send("POST", TRANSFER_PATH, json=body)
        except Exception as exc:
            logger.info("POST %s failed: %s", TRANSFER_PATH, exc)
            return None
        if response.status_code != 200:
            logger.info(
                "POST %s status=%s body=%s",
                TRANSFER_PATH,
                response.status_code,
                response.text,
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        transfer_id = payload.get("transfer_id") if isinstance(payload, dict) else None
        if not isinstance(transfer_id, str) or not transfer_id:
            return None
        logger.info("shard transfer %s -> %s amount=%s", source, dest, amount)
        return transfer_id

    def _wait_transfer(self, transfer_id: str) -> bool:
        path = (
            "/portfolio/intra_exchange_instance_transfers/"
            + quote(transfer_id, safe="")
        )
        deadline = time.monotonic() + _TRANSFER_TIMEOUT_SECONDS
        while True:
            payload = self._get_json(path)
            status = None
            if payload is not None:
                transfer = payload.get("transfer")
                if isinstance(transfer, dict):
                    status = transfer.get("status")
            if status == "complete":
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_TRANSFER_POLL_SECONDS)

    def _ensure_shard_cash(self, body: dict[str, Any]) -> str | None:
        """Move primary cash onto each order's shard. None means the POST may proceed."""
        rows = _order_rows(body)
        if not rows:
            return None
        tickers: list[str] = []
        costs: list[int] = []
        for row in rows:
            ticker = row.get("ticker")
            cost = _order_cost_centicents(row)
            if not isinstance(ticker, str) or cost is None:
                return "shard funding: order cost unreadable"
            tickers.append(ticker)
            costs.append(cost)
        shards = self._market_shards(tickers)
        if shards is None:
            return "shard funding: market exchange_index unreadable"
        required: dict[int, int] = {}
        for ticker, cost in zip(tickers, costs):
            shard = shards.get(ticker)
            if shard is None:
                return f"shard funding: no exchange_index for {ticker}"
            required[shard] = required.get(shard, 0) + cost
        available = self._primary_balances()
        if available is None:
            return "shard funding: subaccount balances unreadable"
        if all(available.get(shard, 0) >= need for shard, need in required.items()):
            return None
        planned = _plan_transfers(available, required)
        if not planned:
            return "shard funding: destination shard short and no donor covers it"
        for source, dest, amount in planned:
            transfer_id = self._start_transfer(source, dest, amount)
            if transfer_id is None or not self._wait_transfer(transfer_id):
                return "shard funding: transfer failed"
        available = self._primary_balances()
        if available is None:
            return "shard funding: subaccount balances unreadable after transfer"
        for shard, need in required.items():
            if available.get(shard, 0) < need:
                return "shard funding: destination shard still short"
        return None

    def _post(
        self, path: str, body: dict[str, Any], *, ticker: str
    ) -> dict[str, Any]:
        client_order_id = body.get("client_order_id")
        result: dict[str, Any] = {
            "ok": False,
            "client_order_id": client_order_id,
            "order_id": None,
            "fill_count": None,
            "remaining_count": None,
            "status_code": None,
            "error": None,
        }

        funding_error = self._ensure_shard_cash(body)
        if funding_error is not None:
            result["error"] = funding_error
            return result

        try:
            response = self._session.send("POST", path, json=body)
        except KalshiAuthError as exc:
            logger.info(
                "POST %s ticker=%s client_order_id=%s auth error: %s",
                path,
                ticker,
                client_order_id,
                exc,
            )
            result["error"] = str(exc)
            return result
        except KalshiError as exc:
            logger.info(
                "POST %s ticker=%s client_order_id=%s error: %s",
                path,
                ticker,
                client_order_id,
                exc,
            )
            result["error"] = str(exc)
            return result
        except Exception as exc:
            logger.info(
                "POST %s ticker=%s client_order_id=%s transport error: %s",
                path,
                ticker,
                client_order_id,
                exc,
            )
            result["error"] = f"request failed: {exc}"
            return result

        result["status_code"] = response.status_code
        logger.info(
            "POST %s ticker=%s client_order_id=%s status=%s body=%s",
            path,
            ticker,
            client_order_id,
            response.status_code,
            response.text,
        )

        parsed: Any = None
        try:
            parsed = response.json()
        except ValueError:
            parsed = None

        result["_parsed"] = parsed
        if response.status_code in (200, 201):
            payload = parsed if isinstance(parsed, dict) else {}
            inner = payload.get("order") if isinstance(payload.get("order"), dict) else payload
            result["ok"] = True
            result["order_id"] = inner.get("order_id")
            result["fill_count"] = inner.get("fill_count")
            result["remaining_count"] = inner.get("remaining_count")
            if inner.get("client_order_id"):
                result["client_order_id"] = inner.get("client_order_id")
            return result

        if response.status_code in (401, 403):
            result["error"] = (
                f"Kalshi rejected the credentials (HTTP {response.status_code}) "
                f"for POST {path}. Check that the keys belong to the "
                f"{self._config.environment} environment. Response: {response.text[:300]}"
            )
            return result

        if isinstance(parsed, dict):
            message = parsed.get("message") or parsed.get("error") or response.text[:300]
            code = parsed.get("code")
            prefix = f"{code}: " if code else ""
            result["error"] = f"HTTP {response.status_code}: {prefix}{message}"
        else:
            result["error"] = f"HTTP {response.status_code}: {response.text[:300]}"
        return result
