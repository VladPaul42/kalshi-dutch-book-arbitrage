"""Authenticated Kalshi ticker WebSocket with dynamic watchlist subscription."""

from __future__ import annotations

import json
import ssl
from typing import Any

import truststore
from websockets.asyncio.client import ClientConnection, connect

from .client import KalshiSignedSession
from .config import ApiConfig, TraderConfig

WS_SIGNING_PATH = "/trade-api/ws/v2"


class ReconnectRequired(RuntimeError):
    """The socket cannot safely update its current subscription in place."""


class KalshiTickerWebSocket:
    """One signed ticker connection.

    Reconnection/backoff belongs to the long-running trader so connection state
    can be written to its status file.
    """

    def __init__(self, api: ApiConfig, trader: TraderConfig) -> None:
        self._api = api
        self._trader = trader
        self._connection: ClientConnection | None = None
        self._tickers: set[str] = set()
        self._ticker_sid: int | None = None
        self._next_id = 1

    async def connect(self, tickers: set[str]) -> None:
        session = KalshiSignedSession(self._api, require_credentials=True)
        try:
            headers = session.headers("GET", WS_SIGNING_PATH)
        finally:
            session.close()
        ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._connection = await connect(
            self._trader.ws_url,
            additional_headers=headers,
            ssl=ssl_context,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
            max_size=4 * 1024 * 1024,
        )
        self._ticker_sid = None
        self._tickers = set(tickers)
        if tickers:
            await self._send(
                "subscribe",
                {
                    "channels": ["ticker"],
                    "market_tickers": sorted(tickers),
                    "send_initial_snapshot": True,
                },
            )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = None
        self._ticker_sid = None

    async def _send(self, command: str, params: dict[str, Any]) -> None:
        if self._connection is None:
            raise ReconnectRequired("WebSocket is not connected")
        message = {"id": self._next_id, "cmd": command, "params": params}
        self._next_id += 1
        await self._connection.send(json.dumps(message))

    async def recv(self) -> dict[str, Any]:
        if self._connection is None:
            raise ReconnectRequired("WebSocket is not connected")
        raw = await self._connection.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return {}
        if payload.get("type") == "subscribed":
            msg = payload.get("msg")
            if isinstance(msg, dict) and msg.get("channel") == "ticker":
                sid = msg.get("sid")
                if isinstance(sid, int):
                    self._ticker_sid = sid
        return payload

    async def update_markets(self, tickers: set[str]) -> None:
        """Add/delete ticker filters on the existing ticker subscription."""
        desired = set(tickers)
        if desired == self._tickers:
            return
        if not self._tickers and desired:
            self._tickers = desired
            await self._send(
                "subscribe",
                {
                    "channels": ["ticker"],
                    "market_tickers": sorted(desired),
                    "send_initial_snapshot": True,
                },
            )
            return
        if self._ticker_sid is None:
            raise ReconnectRequired("ticker subscription has not been acknowledged")

        removed = self._tickers - desired
        added = desired - self._tickers
        if removed:
            await self._send(
                "update_subscription",
                {
                    "sids": [self._ticker_sid],
                    "market_tickers": sorted(removed),
                    "action": "delete_markets",
                },
            )
        if added:
            await self._send(
                "update_subscription",
                {
                    "sids": [self._ticker_sid],
                    "market_tickers": sorted(added),
                    "action": "add_markets",
                    # Same as subscribe: without this, added markets stay unquoted
                    # until they trade or the book moves.
                    "send_initial_snapshot": True,
                },
            )
        self._tickers = desired
