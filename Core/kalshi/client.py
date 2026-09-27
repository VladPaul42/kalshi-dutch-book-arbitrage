"""Read-only signed client for the Kalshi Trade API.

This module exposes nine public methods on :class:`KalshiReadOnlyClient`, all
GET: `get_events_page`, `get_event_markets`, `get_market`, `get_markets`,
`get_market_orderbook`, `get_markets_orderbooks`, `get_balance`,
`get_subaccount_balances` and `get_positions`. There is no POST
or DELETE on that class, so collect.py cannot place or cancel an order. Writes
live in the sibling `orders` module, which reuses the signing and HTTP session
helpers below.

Signing. Every request carries three headers: KALSHI-ACCESS-KEY (the key ID),
KALSHI-ACCESS-TIMESTAMP (milliseconds since epoch, as a string) and
KALSHI-ACCESS-SIGNATURE, an RSA-PSS/SHA-256 signature with salt length equal to
the digest length, computed over exactly `timestamp + METHOD + path`. Nearly all
signature failures come from two things:

  * the signed path must INCLUDE the /trade-api/v2 prefix, and
  * query parameters must be STRIPPED before signing.

The timestamp is part of the signed message, so headers are rebuilt on every
attempt rather than reused.

TLS. This machine has a TLS-intercepting root certificate (antivirus or a proxy)
that lives in the Windows certificate store but not in Python's certifi bundle, so
browsers and curl work while Python fails with CERTIFICATE_VERIFY_FAILED. The
truststore package makes Python verify against the OS store instead. Certificate
verification is never disabled.

Unsigned mode. GET /events, GET /events/{event_ticker}, GET /markets/{ticker},
GET /markets?tickers= and GET /markets/{ticker}/orderbook are public endpoints
that need no credentials.
GET /markets/orderbooks requires credentials. When no credentials are configured
for the selected environment the client can run unsigned, which is what lets you
collect real production market data before generating production keys. Signed
endpoints obviously still require keys.
"""

from __future__ import annotations

import base64
import random
import ssl
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import truststore
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import ApiConfig
from .fixedpoint import to_decimal

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_POSITIONS_LIMIT = 1000
_POSITIONS_MAX_PAGES = 20


class KalshiError(RuntimeError):
    """Any failed interaction with the Kalshi API."""


class KalshiAuthError(KalshiError):
    """Credentials were rejected, or are needed and absent."""


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    try:
        pem = path.read_bytes()
    except OSError as exc:
        raise KalshiError(f"Could not read the private key at {path}: {exc}") from exc

    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:
        raise KalshiError(
            f"Could not parse {path} as an unencrypted PEM private key. Expected the "
            f"file Kalshi generated, which starts with '-----BEGIN'. Error: {exc}"
        ) from exc

    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiError(
            f"The key at {path} is a {type(key).__name__}, not RSA. Kalshi signatures "
            f"are RSA-PSS, so an RSA key is required."
        )
    return key


def sign_headers(
    private_key: rsa.RSAPrivateKey,
    api_key_id: str,
    method: str,
    signing_path: str,
) -> dict[str, str]:
    """RSA-PSS headers for one attempt. Timestamp is signed, so never cache these."""
    timestamp_ms = str(int(time.time() * 1000))
    message = f"{timestamp_ms}{method.upper()}{signing_path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            # Salt length equals digest length. Kalshi rejects PSS.MAX_LENGTH.
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


def _tls_error(base_url: str, exc: Exception) -> KalshiError:
    return KalshiError(
        f"TLS certificate verification failed for {base_url} even with "
        f"truststore enabled. This machine intercepts TLS, so the intercepting "
        f"root certificate must be present in the Windows certificate store. "
        f"Do NOT disable verification. Error: {exc}"
    )


class KalshiSignedSession:
    """Shared httpx session plus RSA-PSS headers. One attempt per ``send``."""

    def __init__(self, config: ApiConfig, *, require_credentials: bool = False) -> None:
        self._config = config
        self._api_key_id: str | None = None
        self._private_key: rsa.RSAPrivateKey | None = None

        if require_credentials and not config.has_credentials:
            raise KalshiAuthError(
                f"No credentials configured for the {config.environment} environment. "
                f"Set the API key ID and private key paths for that environment in "
                f"Core/.env before placing orders."
            )

        if config.has_credentials:
            assert config.api_key_id_path is not None
            assert config.private_key_path is not None
            try:
                self._api_key_id = config.api_key_id_path.read_text(
                    encoding="utf-8"
                ).strip()
            except OSError as exc:
                raise KalshiError(
                    f"Could not read the API key ID file at "
                    f"{config.api_key_id_path}: {exc}"
                ) from exc
            if not self._api_key_id:
                raise KalshiError(
                    f"The API key ID file at {config.api_key_id_path} is empty. It "
                    f"should contain the key ID on a single line."
                )
            self._private_key = _load_private_key(config.private_key_path)

        # Verify TLS against the OS trust store, not certifi. See module docstring.
        self._client = httpx.Client(
            timeout=config.timeout_seconds,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            headers={"Accept": "application/json"},
        )

    @property
    def config(self) -> ApiConfig:
        return self._config

    @property
    def signed(self) -> bool:
        return self._private_key is not None

    def close(self) -> None:
        self._client.close()

    def headers(self, method: str, signing_path: str) -> dict[str, str]:
        """Build fresh auth headers. Empty dict when running unsigned."""
        if self._private_key is None or self._api_key_id is None:
            return {}
        return sign_headers(self._private_key, self._api_key_id, method, signing_path)

    def send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """One HTTP call. Callers decide whether to retry."""
        url = f"{self._config.base_url}{path}"
        # urlsplit().path already excludes the query string; the explicit split is
        # belt-and-braces for a path with a query inlined into it.
        signing_path = urlsplit(url).path.split("?")[0]
        headers = self.headers(method, signing_path)
        if json is not None:
            headers["Content-Type"] = "application/json"

        try:
            return self._client.request(
                method, url, headers=headers, params=params, json=json
            )
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise _tls_error(self._config.base_url, exc) from exc
            raise


def primary_shard_balances(payload: Any) -> dict[int, Decimal] | None:
    """Available dollars on subaccount 0, one entry per exchange shard.

    None when ``payload`` has no ``subaccount_balances`` list. Rows for other
    subaccounts are ignored. A shard with several rows keeps the newest
    ``updated_ts``. The dollar amount is not rounded.
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("subaccount_balances")
    if not isinstance(rows, list):
        return None
    latest: dict[int, tuple[int, Decimal]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("subaccount_number") != 0:
            continue
        shard = row.get("exchange_index")
        balance = to_decimal(row.get("balance"))
        if (
            not isinstance(shard, int)
            or isinstance(shard, bool)
            or balance is None
        ):
            continue
        updated = row.get("updated_ts")
        stamp = updated if isinstance(updated, int) and not isinstance(updated, bool) else 0
        previous = latest.get(shard)
        if previous is None or stamp >= previous[0]:
            latest[shard] = (stamp, balance)
    return {shard: amount for shard, (_, amount) in latest.items()}


class KalshiReadOnlyClient:
    """GET-only client. Signs requests when credentials are available."""

    def __init__(self, config: ApiConfig) -> None:
        self._config = config
        self._session = KalshiSignedSession(config)

    # --- context manager ---------------------------------------------------

    def __enter__(self) -> "KalshiReadOnlyClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    @property
    def signed(self) -> bool:
        return self._session.signed

    # --- signing / retries -------------------------------------------------

    def _headers(self, method: str, signing_path: str) -> dict[str, str]:
        """Build fresh auth headers. Empty dict when running unsigned.

        Called once per attempt, never cached, because the timestamp is signed.
        """
        return self._session.headers(method, signing_path)

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> float:
        """Exponential backoff with jitter, honouring Retry-After when sent."""
        suggested = 0.0
        if retry_after:
            try:
                suggested = float(retry_after)
            except ValueError:
                suggested = 0.0
        return max(suggested, 0.5 * (2**attempt)) + random.uniform(0, 0.25)

    # --- the single GET path -----------------------------------------------

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        """Perform one GET with retries. Private: only the helpers below call it."""
        url = f"{self._config.base_url}{path}"
        signing_path = urlsplit(url).path.split("?")[0]

        last_error: Exception | None = None
        max_retries = self._config.max_retries

        for attempt in range(max_retries + 1):
            try:
                response = self._session.send("GET", path, params=params)
            except KalshiError:
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                time.sleep(self._backoff(attempt))
                continue

            if response.status_code in (401, 403):
                if not self.signed:
                    raise KalshiAuthError(
                        f"GET {signing_path} returned {response.status_code} and the "
                        f"request was unsigned. This endpoint needs credentials: set "
                        f"the API key ID and private key paths for the "
                        f"{self._config.environment} environment in .env."
                    )
                raise KalshiAuthError(
                    f"Kalshi rejected the credentials ({response.status_code}) for "
                    f"GET {signing_path}. Check that the key ID matches the private "
                    f"key, that both belong to the {self._config.environment} "
                    f"environment matching {self._config.base_url}, and that the system "
                    f"clock is accurate (the timestamp is signed). "
                    f"Response: {response.text[:300]}"
                )

            if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
                delay = self._backoff(attempt, response.headers.get("Retry-After"))
                print(
                    f"  retryable HTTP {response.status_code}, waiting {delay:.2f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
                continue

            if response.status_code == 404 and allow_not_found:
                return None

            if response.status_code >= 400:
                raise KalshiError(
                    f"GET {signing_path} failed with HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise KalshiError(
                    f"GET {signing_path} returned HTTP {response.status_code} with a "
                    f"non-JSON body: {response.text[:200]}"
                ) from exc

        raise KalshiError(
            f"GET {signing_path} failed after {max_retries + 1} attempts. "
            f"Last error: {last_error}"
        )

    # --- the only GET endpoints, all read-only -----------------------------

    def get_balance(self) -> dict[str, Any]:
        """GET /portfolio/balance. Requires credentials. Used only to prove auth."""
        payload = self._get("/portfolio/balance")
        assert payload is not None
        return payload

    def get_subaccount_balances(self) -> dict[int, Decimal]:
        """Available dollars on subaccount 0, one entry per exchange shard.

        GET /portfolio/subaccounts/balances. Requires credentials. Raises when
        the response has no ``subaccount_balances`` list.
        """
        payload = self._get("/portfolio/subaccounts/balances")
        assert payload is not None
        balances = primary_shard_balances(payload)
        if balances is None:
            raise KalshiError(
                "GET /portfolio/subaccounts/balances returned no "
                "subaccount_balances list"
            )
        return balances

    def get_positions(self) -> list[dict[str, Any]]:
        """Open market positions on subaccount 0, every exchange shard.

        GET /portfolio/positions?count_filter=position&limit=1000. Follows
        ``cursor`` until it is empty. ``subaccount`` and ``exchange_index`` are
        omitted, so this is the primary account across all shards. Requires
        credentials. Raises if a cursor repeats or the page cap is hit with a
        cursor still outstanding, rather than returning a partial book.
        """
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(_POSITIONS_MAX_PAGES):
            params: dict[str, Any] = {
                "count_filter": "position",
                "limit": _POSITIONS_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._get("/portfolio/positions", params=params)
            assert payload is not None
            markets = payload.get("market_positions")
            if not isinstance(markets, list):
                raise KalshiError(
                    "GET /portfolio/positions returned no market_positions list"
                )
            rows.extend(row for row in markets if isinstance(row, dict))
            next_cursor = payload.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return rows
            if next_cursor in seen:
                raise KalshiError("GET /portfolio/positions repeated a cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise KalshiError(
            "GET /portfolio/positions still had a cursor after "
            f"{_POSITIONS_MAX_PAGES} pages"
        )

    def get_market(self, ticker: str) -> dict[str, Any] | None:
        """One market, including prices, YES sizes, status, volume and close time.

        Returns the inner ``market`` object, or None when the market is missing
        (HTTP 404). Prices on this payload are the real book fields. NO sizes
        are often absent; those come from :meth:`get_market_orderbook`.
        """
        encoded = quote(ticker, safe="")
        payload = self._get(f"/markets/{encoded}", allow_not_found=True)
        if not isinstance(payload, dict):
            return None
        market = payload.get("market")
        if isinstance(market, dict):
            return market
        return payload

    def get_markets(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Markets for a comma-separated ticker list.

        GET /markets?tickers=A,B,C. Each item is the same market object as
        :meth:`get_market`: prices, YES sizes, status, volume, open interest
        and close time. NO sizes are absent. Returns the inner ``markets``
        list, or an empty list when the body is not that shape.
        """
        if not tickers:
            return []
        payload = self._get("/markets", params={"tickers": ",".join(tickers)})
        if not isinstance(payload, dict):
            return []
        markets = payload.get("markets")
        if not isinstance(markets, list):
            return []
        return [market for market in markets if isinstance(market, dict)]

    def get_market_orderbook(
        self, ticker: str, depth: int = 1
    ) -> dict[str, Any] | None:
        """Top of the YES-bid and NO-bid books for one market.

        Kalshi's orderbook is bids only: a YES bid at P is the NO ask at 1-P
        with the same resting size. ``depth=1`` is the best level on each side.
        Returns None when the market is missing (HTTP 404).
        """
        encoded = quote(ticker, safe="")
        return self._get(
            f"/markets/{encoded}/orderbook",
            params={"depth": depth},
            allow_not_found=True,
        )

    def get_markets_orderbooks(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Order books for up to 100 tickers. Requires credentials.

        GET /markets/orderbooks with one ``tickers`` query parameter per ticker
        (``tickers=A&tickers=B``), not a comma-separated string. Each item is
        ``{"ticker": "...", "orderbook_fp": {...}}``. Returns the inner
        ``orderbooks`` list, or an empty list when the body is not that shape.
        """
        if not tickers:
            return []
        payload = self._get("/markets/orderbooks", params={"tickers": list(tickers)})
        if not isinstance(payload, dict):
            return []
        books = payload.get("orderbooks")
        if not isinstance(books, list):
            return []
        return [book for book in books if isinstance(book, dict)]

    def get_events_page(
        self, limit: int, cursor: str | None = None
    ) -> dict[str, Any]:
        """One page of open events with their markets attached.

        with_nested_markets=true puts the event's markets in `event.markets`, so
        this is one call per page rather than one call per market. This endpoint
        already excludes multivariate (combo) events.

        `limit` is capped at 200 by the API, and nested markets now honour that cap
        rather than being silently reduced to 100.
        """
        params: dict[str, Any] = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        payload = self._get("/events", params=params)
        assert payload is not None
        return payload

    def get_event_markets(self, event_ticker: str) -> list[dict[str, Any]]:
        """Every market on one event.

        GET /events/{event_ticker}?with_nested_markets=true. Returns the inner
        ``markets`` list, or an empty list when the event is missing or the
        body is not that shape.
        """
        encoded = quote(event_ticker, safe="")
        payload = self._get(
            f"/events/{encoded}",
            params={"with_nested_markets": "true"},
            allow_not_found=True,
        )
        if not isinstance(payload, dict):
            return []
        event = payload.get("event")
        if not isinstance(event, dict):
            return []
        markets = event.get("markets")
        if not isinstance(markets, list):
            return []
        return [market for market in markets if isinstance(market, dict)]
