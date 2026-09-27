"""Fill NO top-of-book sizes on a candidates file from Kalshi orderbooks.

    python enrich_sizes.py                         # newest file in data/candidates
    python enrich_sizes.py --candidates PATH       # a specific one

Nested GET /events markets carry YES sizes but not NO sizes. Kalshi's orderbook
is bids only: a YES bid at P is the NO ask at 1-P with the same resting size.
This script fetches GET /markets/{ticker}/orderbook?depth=1 for each surviving
market and writes:

    no_bid_size_contracts  <- size at the best NO bid
    no_ask_size_contracts  <- size at the best YES bid (the size you lift buying NO)

YES sizes and all prices are left as the snapshot wrote them. Empty or missing
books stay null; one 404 does not abort the run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi.client import KalshiAuthError, KalshiError, KalshiReadOnlyClient
from kalshi.config import (
    CANDIDATES_DIR,
    ApiConfig,
    ConfigError,
    display_path,
    enable_utf8_stdout,
    load_api_config,
)
from kalshi.fixedpoint import contract_count, to_json

SIZES_SOURCE = "GET /markets/{ticker}/orderbook?depth=1"


def newest_candidates() -> Path:
    files = sorted(
        [
            path
            for path in CANDIDATES_DIR.glob("candidates_*.json")
            if not path.name.endswith(".partial")
        ],
        key=lambda path: path.stat().st_mtime,
    )
    if not files:
        raise SystemExit(
            f"No candidates files found in {display_path(CANDIDATES_DIR)}.\n"
            f"Run 'python filter_candidates.py' first."
        )
    return files[-1]


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


def market_tickers(payload: dict[str, Any]) -> list[str]:
    """Unique market tickers in file order, skipping blanks."""
    seen: set[str] = set()
    ordered: list[str] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker")
            if isinstance(ticker, str) and ticker and ticker not in seen:
                seen.add(ticker)
                ordered.append(ticker)
    return ordered


def best_bid_size(levels: Any) -> Decimal | None:
    """Contract count at the best bid (last level; books are price-ascending)."""
    if not isinstance(levels, list) or not levels:
        return None
    last = levels[-1]
    if not isinstance(last, (list, tuple)) or len(last) < 2:
        return None
    return contract_count({"count_fp": last[1]}, "count_fp")


def sizes_from_orderbook(payload: dict[str, Any] | None) -> tuple[Decimal | None, Decimal | None]:
    """Return (no_bid_size, no_ask_size) from a GetMarketOrderbook body."""
    if not payload:
        return None, None
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        return None, None
    no_bid = best_bid_size(book.get("no_dollars"))
    no_ask = best_bid_size(book.get("yes_dollars"))
    return no_bid, no_ask


def print_banner(config: ApiConfig) -> None:
    label = "PRODUCTION" if config.environment == "production" else "DEMO"
    print("=" * 72)
    print(f"  ENVIRONMENT: {label}  ->  {config.base_url}")
    if config.has_credentials:
        print("  Requests are SIGNED.")
    else:
        print(
            "  Requests are UNSIGNED. GET /markets/{ticker}/orderbook is a public\n"
            "  endpoint, so this works without credentials. Set the key paths in .env "
            "to sign."
        )
    if config.is_demo:
        print(
            "  WARNING: demo order books are synthetic and thin, so any liquidity\n"
            "  finding from this snapshot is meaningless. Use production for research."
        )
    print("  Read-only: this script cannot place or cancel orders.")
    print("=" * 72)
    print()


def apply_sizes(
    payload: dict[str, Any], sizes: dict[str, tuple[Decimal | None, Decimal | None]]
) -> None:
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker")
            if ticker not in sizes:
                continue
            no_bid, no_ask = sizes[ticker]
            market["no_bid_size_contracts"] = to_json(no_bid)
            market["no_ask_size_contracts"] = to_json(no_ask)


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(path.name + ".partial")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise SystemExit(
            f"Could not write {display_path(path)}: {exc}"
        ) from exc


def main() -> int:
    enable_utf8_stdout()

    parser = argparse.ArgumentParser(
        description=(
            "Fill NO top-of-book sizes on a candidates file from Kalshi orderbooks. "
            "Reads the file, fetches one orderbook per market, writes the same file."
        )
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="Candidates file to enrich. Defaults to the newest file in data/candidates.",
    )
    args = parser.parse_args()

    candidates_path = args.candidates or newest_candidates()
    if not candidates_path.is_file():
        print(f"No such candidates file: {candidates_path}", file=sys.stderr)
        return 1

    payload = load_candidates(candidates_path)
    file_env = payload.get("environment")
    try:
        config = load_api_config(
            file_env if file_env in ("demo", "production") else None
        )
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

    tickers = market_tickers(payload)
    print(f"Candidates  : {display_path(candidates_path)}")
    print(f"Events      : {len(payload.get('events') or [])}")
    print(f"Markets     : {len(tickers)}")
    print(f"Fetching {SIZES_SOURCE} ...")
    print()

    sizes: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    fetched = 0
    failed = 0
    started_monotonic = time.monotonic()

    try:
        with KalshiReadOnlyClient(config) as client:
            for index, ticker in enumerate(tickers, start=1):
                try:
                    book = client.get_market_orderbook(ticker, depth=1)
                except KalshiAuthError as exc:
                    print(f"\nEnrichment failed:\n{exc}", file=sys.stderr)
                    return 1
                except KalshiError as exc:
                    failed += 1
                    sizes[ticker] = (None, None)
                    print(f"  {index:>4}/{len(tickers)}  {ticker}  failed: {exc}")
                else:
                    fetched += 1
                    sizes[ticker] = sizes_from_orderbook(book)
                    no_bid, no_ask = sizes[ticker]
                    print(
                        f"  {index:>4}/{len(tickers)}  {ticker}  "
                        f"no_bid={to_json(no_bid) or '-'}  "
                        f"no_ask={to_json(no_ask) or '-'}"
                    )
                if index < len(tickers) and config.page_sleep_seconds > 0:
                    time.sleep(config.page_sleep_seconds)
    except KalshiError as exc:
        print(f"\nEnrichment failed:\n{exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started_monotonic
    apply_sizes(payload, sizes)

    payload["sizes_source"] = SIZES_SOURCE
    payload["sizes_enriched_at_utc"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    payload["sizes_markets_fetched"] = fetched
    payload["sizes_markets_failed"] = failed

    write_atomic(candidates_path, payload)

    print()
    print("-" * 72)
    print("Size enrichment summary")
    print("-" * 72)
    print(f"  Environment    : {config.environment.upper()}  ({config.base_url})")
    print(f"  Requests signed: {'yes' if config.has_credentials else 'no (public read)'}")
    print(f"  Markets fetched: {fetched:,}")
    print(f"  Markets failed : {failed:,}")
    print(f"  Time taken     : {elapsed:.1f}s")
    print(f"  Wrote          : {display_path(candidates_path)}")
    print("-" * 72)
    print("Next: python score.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
