"""Refresh prices and sizes for the pinned Greater Cycle universe.

    python refresh_quotes.py
    python refresh_quotes.py --universe PATH

Starts from the newest Greater Cycle candidates file. Markets already on those
events are refreshed in groups of 100: GET /markets?tickers=A,B,C (prices, YES
sizes, status, volume, open interest, close time). When the client is signed,
NO sizes come from GET /markets/orderbooks for the same group. When it is
unsigned, each ticker still gets GET /markets/{ticker}/orderbook?depth=1.
Prices are never inferred from the book. Markets that are not in the Greater
file are not requested.

The same filter gates then run again. Failures are omitted from this file
only; the Greater file is left in place so the next Lesser can see them.
Writes a new candidates file. Does not score and does not publish a watchlist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from enrich_sizes import sizes_from_orderbook
from filter_candidates import apply_gates, print_survivors, summarise
from kalshi.client import KalshiAuthError, KalshiError, KalshiReadOnlyClient
from kalshi.config import (
    CANDIDATES_DIR,
    ApiConfig,
    ConfigError,
    display_path,
    enable_utf8_stdout,
    load_api_config,
    load_filter_config,
)
from kalshi.fixedpoint import to_json
from kalshi.retention import cycle_kind_of, list_pipeline_files

QUOTE_BATCH_SIZE = 100

QUOTE_SOURCE = (
    "GET /markets?tickers= for prices, YES sizes, status, volume, "
    "open interest and close time, in groups of 100; "
    "GET /markets/orderbooks for NO sizes when signed, otherwise "
    "GET /markets/{ticker}/orderbook?depth=1"
)


def load_candidates(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise SystemExit(f"Could not read {display_path(path)}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{display_path(path)} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "events" not in payload:
        raise SystemExit(
            f"{display_path(path)} does not look like a candidates file: expected a "
            f"JSON object with an 'events' key."
        )
    return payload


def newest_greater() -> Path:
    """Newest same-directory candidates file tagged ``cycle_kind: greater``."""
    for row in list_pipeline_files(CANDIDATES_DIR, "candidates"):
        if cycle_kind_of(row.path) == "greater":
            return row.path
    raise SystemExit(
        f"No Greater Cycle universe file in {display_path(CANDIDATES_DIR)}.\n"
        f"Run 'python collect.py' and 'python filter_candidates.py' first."
    )


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(path.name + ".partial")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise SystemExit(f"Could not write {display_path(path)}: {exc}") from exc


def print_banner(config: ApiConfig) -> None:
    label = "PRODUCTION" if config.environment == "production" else "DEMO"
    print("=" * 72)
    print(f"  ENVIRONMENT: {label}  ->  {config.base_url}")
    if config.has_credentials:
        print("  Requests are SIGNED.")
    else:
        print(
            "  Requests are UNSIGNED. GET /markets?tickers= and per-market\n"
            "  order books are public, so this works without credentials.\n"
            "  GET /markets/orderbooks needs a signature."
        )
    if config.is_demo:
        print(
            "  WARNING: demo order books are synthetic and thin, so any liquidity\n"
            "  finding from this snapshot is meaningless. Use production for research."
        )
    print("  Read-only: this script cannot place or cancel orders.")
    print("=" * 72)
    print()


def _missing_market(ticker: str) -> dict[str, Any]:
    """A leg that could not be fetched. It fails the active-status gate."""
    return {"ticker": ticker, "status": "missing"}


def _with_no_sizes(market: dict[str, Any], book: dict[str, Any] | None) -> dict[str, Any]:
    """Copy the market payload and store the orderbook's NO sizes on it.

    A missing book level is written as zero so the executable-route gate does
    not fill it from the YES complement. Prices on ``market`` are left as the
    market endpoint sent them.
    """
    live = dict(market)
    no_bid, no_ask = sizes_from_orderbook(book)
    live["no_bid_size_fp"] = to_json(no_bid) if no_bid is not None else "0"
    live["no_ask_size_fp"] = to_json(no_ask) if no_ask is not None else "0"
    return live


def _event_tickers(events: list[dict[str, Any]]) -> list[str]:
    """Every market ticker on the Greater universe, in file order."""
    tickers: list[str] = []
    for event in events:
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker")
            if isinstance(ticker, str) and ticker:
                tickers.append(ticker)
    return tickers


def _live_by_ticker(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    live: dict[str, dict[str, Any]] = {}
    for market in markets:
        ticker = market.get("ticker")
        if isinstance(ticker, str) and ticker and ticker not in live:
            live[ticker] = market
    return live


def _books_by_ticker(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Map each batch orderbook item to the shape ``sizes_from_orderbook`` expects."""
    books: dict[str, dict[str, Any] | None] = {}
    for item in rows:
        ticker = item.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            continue
        fp = item.get("orderbook_fp")
        books[ticker] = {"orderbook_fp": fp} if isinstance(fp, dict) else None
    return books


def refresh_events(
    client: KalshiReadOnlyClient,
    universe: dict[str, Any],
    sleep_seconds: float,
) -> tuple[list[dict[str, Any]], int, int]:
    """Fetch every universe market. Returns events, fetched count, failed count.

    Markets are requested in groups of ``QUOTE_BATCH_SIZE``. The batched path
    does not sleep between markets. An unsigned client still fetches NO sizes
    one ticker at a time and pauses once between those calls.
    """
    events = [event for event in (universe.get("events") or []) if isinstance(event, dict)]
    tickers = _event_tickers(events)
    total = len(tickers)
    fetched = 0
    failed = 0
    resolved: list[dict[str, Any]] = []
    cursor = 0

    for start in range(0, total, QUOTE_BATCH_SIZE):
        group = tickers[start : start + QUOTE_BATCH_SIZE]
        try:
            payload_markets = client.get_markets(group)
        except KalshiAuthError:
            raise
        except KalshiError as exc:
            for ticker in group:
                cursor += 1
                failed += 1
                resolved.append(_missing_market(ticker))
                print(f"  {cursor:>4}/{total}  {ticker}  failed: {exc}")
            continue

        live_by_ticker = _live_by_ticker(payload_markets)
        books_by_ticker: dict[str, dict[str, Any] | None] = {}
        orderbook_error: KalshiError | None = None
        orderbook_errors: dict[str, KalshiError] = {}
        if client.signed:
            try:
                books_by_ticker = _books_by_ticker(client.get_markets_orderbooks(group))
            except KalshiAuthError:
                raise
            except KalshiError as exc:
                orderbook_error = exc
        else:
            pending = [ticker for ticker in group if ticker in live_by_ticker]
            for index, ticker in enumerate(pending):
                try:
                    books_by_ticker[ticker] = client.get_market_orderbook(ticker, depth=1)
                except KalshiAuthError:
                    raise
                except KalshiError as exc:
                    orderbook_errors[ticker] = exc
                if index < len(pending) - 1 and sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        for ticker in group:
            cursor += 1
            live = live_by_ticker.get(ticker)
            ticker_error = orderbook_errors.get(ticker)
            if live is None:
                failed += 1
                resolved.append(_missing_market(ticker))
                print(f"  {cursor:>4}/{total}  {ticker}  missing")
            elif orderbook_error is not None:
                failed += 1
                resolved.append(_missing_market(ticker))
                print(f"  {cursor:>4}/{total}  {ticker}  failed: {orderbook_error}")
            elif ticker_error is not None:
                failed += 1
                resolved.append(_missing_market(ticker))
                print(f"  {cursor:>4}/{total}  {ticker}  failed: {ticker_error}")
            else:
                fetched += 1
                resolved.append(_with_no_sizes(live, books_by_ticker.get(ticker)))
                print(f"  {cursor:>4}/{total}  {ticker}  refreshed")

    index = 0
    refreshed: list[dict[str, Any]] = []
    for event in events:
        markets: list[dict[str, Any]] = []
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker")
            if not isinstance(ticker, str) or not ticker:
                continue
            markets.append(resolved[index])
            index += 1
        refreshed.append(
            {
                "event_ticker": event.get("event_ticker"),
                "title": event.get("title"),
                "sub_title": event.get("sub_title"),
                "mutually_exclusive": event.get("mutually_exclusive"),
                "markets": markets,
            }
        )
    return refreshed, fetched, failed


def main() -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=(
            "Refresh prices and sizes for the Greater Cycle universe, re-apply "
            "the filter gates, and write a Lesser Cycle candidates file."
        )
    )
    parser.add_argument(
        "--universe",
        type=Path,
        default=None,
        help="Greater Cycle candidates file. Defaults to the newest one.",
    )
    args = parser.parse_args()

    universe_path = args.universe or newest_greater()
    if not universe_path.is_file():
        print(f"No such universe file: {universe_path}", file=sys.stderr)
        return 1
    universe = load_candidates(universe_path)
    if universe.get("cycle_kind") != "greater":
        print(
            f"{display_path(universe_path)} is not a Greater Cycle universe "
            f"(cycle_kind={universe.get('cycle_kind')!r}).",
            file=sys.stderr,
        )
        return 1

    file_env = universe.get("environment")
    try:
        config = load_api_config(file_env if file_env in ("demo", "production") else None)
        filter_config = load_filter_config()
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 1

    if not config.has_credentials and not config.allow_unsigned_public_reads:
        print(
            f"No {config.environment} credentials are configured and "
            f"ALLOW_UNSIGNED_PUBLIC_READS is false.\n"
            f"Either set the credential paths in .env or allow unsigned public reads.",
            file=sys.stderr,
        )
        return 1

    print_banner(config)
    print(f"Universe    : {display_path(universe_path)}")
    print(f"Events      : {len(universe.get('events') or [])}")
    print(f"Fetching {QUOTE_SOURCE} ...")
    print()

    started = time.monotonic()
    try:
        with KalshiReadOnlyClient(config) as client:
            refreshed, fetched, failed = refresh_events(
                client, universe, config.page_sleep_seconds
            )
    except KalshiAuthError as exc:
        print(f"\nRefresh failed:\n{exc}", file=sys.stderr)
        return 1
    except KalshiError as exc:
        print(f"\nRefresh failed:\n{exc}", file=sys.stderr)
        return 1

    survivors, funnel = apply_gates(
        refreshed, filter_config, opening_label="candidate universe"
    )
    funnel.print()
    summaries = [summarise(event) for event in survivors]
    print_survivors(summaries)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    environment = file_env if file_env in ("demo", "production") else config.environment
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    destination = CANDIDATES_DIR / f"candidates_{environment}_{stamp}.json"
    captured = now.isoformat(timespec="seconds")
    output = {
        "source_snapshot": universe.get("source_snapshot"),
        "source_snapshot_captured_at_utc": captured,
        "environment": environment,
        "filtered_at_utc": captured,
        "price_unit": "cents",
        "price_encoding": "decimal string, e.g. '42.5' means 42.5 cents",
        "quantity_unit": "contracts, decimal string, fractional to 0.01",
        "cycle_kind": "lesser",
        "universe_candidates": universe_path.name,
        "sizes_source": QUOTE_SOURCE,
        "quotes_refreshed_at_utc": captured,
        "quotes_markets_fetched": fetched,
        "quotes_markets_failed": failed,
        "gates": filter_config.as_dict(),
        "funnel": funnel.rows,
        "candidate_count": len(summaries),
        "events": summaries,
    }
    write_atomic(destination, output)

    elapsed = time.monotonic() - started
    print()
    print("-" * 72)
    print("Quote refresh summary")
    print("-" * 72)
    print(f"  Environment    : {config.environment.upper()}  ({config.base_url})")
    print(f"  Requests signed: {'yes' if config.has_credentials else 'no (public read)'}")
    print(f"  Markets fetched: {fetched:,}")
    print(f"  Markets failed : {failed:,}")
    print(f"  Survivors      : {len(summaries):,}")
    print(f"  Time taken     : {elapsed:.1f}s")
    print(f"  Wrote          : {display_path(destination)}")
    print("-" * 72)
    print("Next: python score.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
