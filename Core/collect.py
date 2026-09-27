"""Archive every open Kalshi event, with its markets, to a timestamped file.

Run it repeatedly (every 10 minutes or so) to build up history:

    python collect.py

One call per page, not per market: GET /events with with_nested_markets=true
returns each event with its markets already attached in `event.markets`. Pagination
follows `cursor` until it comes back empty.

The archive is RAW ON PURPOSE. Page payloads are stored byte-for-byte as Kalshi
sent them, wrapped in a metadata envelope that records where and when they came
from. Nothing is reshaped or pruned on the way in, because the point of a raw
archive is being able to re-run analysis on it later, including analysis nobody has
thought of yet. All the narrowing happens in filter_candidates.py, which reads
these files back.

This script only ever issues GET requests.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone

from kalshi.client import KalshiError, KalshiReadOnlyClient
from kalshi.config import (
    RAW_DIR,
    ApiConfig,
    ConfigError,
    display_path,
    enable_utf8_stdout,
    load_api_config,
)

COLLECTOR_VERSION = 1


def utc_stamp(moment: datetime) -> str:
    """Filename-safe UTC timestamp, e.g. 20260918T103000Z."""
    return moment.strftime("%Y%m%dT%H%M%SZ")


def print_banner(config: ApiConfig) -> None:
    """Say loudly which environment this run is pointed at.

    Worth being noisy about: demo and production data look identical in a file but
    mean completely different things.
    """
    label = "PRODUCTION" if config.environment == "production" else "DEMO"
    print("=" * 72)
    print(f"  ENVIRONMENT: {label}  ->  {config.base_url}")
    if config.has_credentials:
        print("  Requests are SIGNED.")
    else:
        print(
            "  Requests are UNSIGNED. GET /events is a public endpoint, so this works\n"
            "  without credentials. Set the key paths in .env to sign."
        )
    if config.is_demo:
        print(
            "  WARNING: demo order books are synthetic and thin, so any liquidity\n"
            "  finding from this snapshot is meaningless. Use production for research."
        )
    print("  Read-only: this script cannot place or cancel orders.")
    print("=" * 72)
    print()


def collect_pages(client: KalshiReadOnlyClient, config: ApiConfig) -> list[dict]:
    """Walk every page of open events, returning the verbatim page payloads."""
    pages: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for page_number in range(1, config.max_pages + 1):
        payload = client.get_events_page(limit=config.page_limit, cursor=cursor)

        if not isinstance(payload, dict) or "events" not in payload:
            raise KalshiError(
                f"Page {page_number} did not look like a GetEvents response: expected a "
                f"JSON object with an 'events' key, got keys "
                f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}. "
                f"Refusing to save a snapshot built from this."
            )

        events = payload.get("events")
        if not isinstance(events, list):
            raise KalshiError(
                f"Page {page_number} had an 'events' field of type "
                f"{type(events).__name__} rather than a list. Refusing to save."
            )

        # Store the payload exactly as received. No reshaping, no pruning.
        pages.append(payload)

        market_count = sum(len(event.get("markets") or []) for event in events)
        print(
            f"  page {page_number:>3}: {len(events):>4} events, "
            f"{market_count:>5} markets"
        )

        cursor = payload.get("cursor") or None
        if not cursor:
            break

        # A cursor that repeats means the server is looping us; stop rather than
        # spin until max_pages.
        if cursor in seen_cursors:
            print(
                f"  cursor repeated after page {page_number}; stopping to avoid a loop."
            )
            break
        seen_cursors.add(cursor)

        time.sleep(config.page_sleep_seconds)
    else:
        print(
            f"  hit COLLECTOR_MAX_PAGES ({config.max_pages}) with a cursor still "
            f"pending; the snapshot may be incomplete."
        )

    return pages


def main() -> int:
    enable_utf8_stdout()

    try:
        config = load_api_config()
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 1

    print_banner(config)

    if not config.has_credentials and not config.allow_unsigned_public_reads:
        print(
            f"No {config.environment} credentials are configured and "
            f"ALLOW_UNSIGNED_PUBLIC_READS is false.\n"
            f"Either set the credential paths in .env or allow unsigned public reads.",
            file=sys.stderr,
        )
        return 1

    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    print(f"Fetching open events at {started_at.isoformat(timespec='seconds')} ...")

    try:
        with KalshiReadOnlyClient(config) as client:
            pages = collect_pages(client, config)
    except KalshiError as exc:
        print(f"\nCollection failed:\n{exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started_monotonic

    all_events = [event for page in pages for event in page.get("events") or []]
    event_count = len(all_events)
    market_count = sum(len(event.get("markets") or []) for event in all_events)
    unique_event_count = len({
        event.get("event_ticker") for event in all_events if event.get("event_ticker")
    })

    # Fail loudly rather than leaving a plausible-looking empty archive behind.
    if event_count == 0:
        print(
            "\nThe API returned zero open events across all pages. That is almost\n"
            "certainly wrong, so no file was written. Check the environment and\n"
            "whether the exchange is open.",
            file=sys.stderr,
        )
        return 1

    if market_count == 0:
        print(
            f"\n{event_count} events came back but not one had any nested markets, so\n"
            f"with_nested_markets appears to have been ignored. No file was written.",
            file=sys.stderr,
        )
        return 1

    snapshot = {
        "snapshot_metadata": {
            "captured_at_utc": started_at.isoformat(timespec="seconds"),
            "environment": config.environment,
            "base_url": config.base_url,
            "endpoint": "/events",
            "query_params": {
                "status": "open",
                "with_nested_markets": "true",
                "limit": config.page_limit,
            },
            "signed": config.has_credentials,
            "pages_fetched": len(pages),
            "event_count": event_count,
            "unique_event_count": unique_event_count,
            "market_count": market_count,
            "elapsed_seconds": round(elapsed, 2),
            "collector_version": COLLECTOR_VERSION,
            "note": (
                "The 'pages' array holds GetEvents response payloads verbatim, exactly "
                "as returned. Nothing has been reshaped or pruned. Prices are "
                "fixed-point dollar strings in *_dollars fields and contract counts are "
                "fixed-point strings in *_fp fields; parse both with Decimal."
            ),
        },
        "pages": pages,
    }

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # Gzip by default. A full open-events snapshot is ~300 MB of JSON, and running
    # this every 10 minutes would be ~43 GB/day uncompressed. The gzipped bytes are
    # still the verbatim payload, and the filter reads either form transparently.
    suffix = ".json.gz" if config.compress_snapshots else ".json"
    filename = f"events_open_{config.environment}_{utc_stamp(started_at)}{suffix}"
    destination = RAW_DIR / filename

    # Write to a temp file and rename, so an interrupted run can never leave a
    # half-written file that looks like a good snapshot.
    temp_path = destination.with_name(destination.name + ".partial")
    try:
        if config.compress_snapshots:
            with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False)
        else:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False)
        os.replace(temp_path, destination)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        print(
            f"\nCould not write the snapshot to {display_path(destination)}: {exc}",
            file=sys.stderr,
        )
        return 1

    size_mb = destination.stat().st_size / (1024 * 1024)

    print()
    print("-" * 72)
    print("Collection summary")
    print("-" * 72)
    print(f"  Environment    : {config.environment.upper()}  ({config.base_url})")
    print(f"  Requests signed: {'yes' if config.has_credentials else 'no (public read)'}")
    print(f"  Pages fetched  : {len(pages)}")
    print(f"  Events found   : {event_count:,}  ({unique_event_count:,} unique tickers)")
    print(f"  Markets found  : {market_count:,}")
    print(f"  Time taken     : {elapsed:.1f}s")
    print(f"  Saved to       : {display_path(destination)}")
    print(
        f"  File size      : {size_mb:.1f} MB"
        f"{' (gzipped)' if config.compress_snapshots else ''}"
    )
    print("-" * 72)
    print()
    print("Next: python filter_candidates.py")
    print("Then: python refresh_quotes.py")

    return 0


if __name__ == "__main__":
    sys.exit(main())
