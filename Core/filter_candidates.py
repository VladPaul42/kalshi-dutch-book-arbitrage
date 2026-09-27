"""Narrow one saved snapshot down to candidate mutually exclusive events.

    python filter_candidates.py                      # newest snapshot in data/raw
    python filter_candidates.py --snapshot PATH      # a specific one

This script NEVER touches the network. It imports only kalshi.config,
kalshi.fixedpoint and kalshi.mispricing plus the standard library - notably
not kalshi.client - so there is no code path from here to the API. It reads a
file, applies gates, prints a funnel, and writes a smaller file.

The funnel is the point. It says how many open events survive each gate in turn,
which is the actual research finding: whether mutually exclusive events with real
liquidity exist on Kalshi at all.

Two API details that will bite anyone editing the gates:

  * The tradeable market status is "active", NOT "open". The status enum is
    initialized / inactive / active / closed / determined / disputed / amended /
    finalized. There is no "open" value, so a `status == "open"` gate silently
    yields zero survivors and looks like a research finding rather than a bug.
  * Events have no close time of their own. Only markets carry close_time, so the
    event's close time is derived - here, the EARLIEST close_time among its
    markets, because one leg closing in three minutes makes the ladder
    untradeable regardless of the others.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi.config import (
    CANDIDATES_DIR,
    RAW_DIR,
    ConfigError,
    FilterConfig,
    display_path,
    enable_utf8_stdout,
    load_filter_config,
)
from kalshi.fixedpoint import contract_count, price_cents, to_json
from kalshi.mispricing import route_executable
from kalshi.retention import is_live_candidates_file, print_prune_result, prune_rolling_window

# The market lifecycle state in which a market can actually be traded.
TRADEABLE_STATUS = "active"


def parse_iso8601(value: Any) -> datetime | None:
    """Parse an RFC3339 timestamp from the API into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    # Python's fromisoformat only learned to accept a trailing "Z" in 3.11, and
    # some fields carry more sub-second digits than it likes.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def newest_snapshot() -> Path:
    candidates = sorted(
        [p for p in RAW_DIR.glob("events_open_*.json*") if not p.name.endswith(".partial")],
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        raise SystemExit(
            f"No snapshots found in {display_path(RAW_DIR)}.\n"
            f"Run 'python collect.py' first."
        )
    return candidates[-1]


def load_snapshot(path: Path) -> dict[str, Any]:
    """Read a snapshot, transparently handling gzipped and plain JSON."""
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
    except OSError as exc:
        raise SystemExit(f"Could not read {display_path(path)}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{display_path(path)} is not valid JSON: {exc}\n"
            f"If the collector was interrupted, delete it and collect again."
        ) from exc

    if not isinstance(payload, dict) or "pages" not in payload:
        raise SystemExit(
            f"{display_path(path)} does not look like a collector snapshot: expected a "
            f"JSON object with 'snapshot_metadata' and 'pages' keys."
        )
    return payload


def extract_events(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the verbatim pages into one event list, deduplicated by ticker.

    Pagination can repeat an event across page boundaries when the underlying data
    shifts mid-walk, so dedupe before counting anything. Later copies win, being
    marginally fresher.
    """
    by_ticker: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []

    for page in snapshot.get("pages") or []:
        for event in page.get("events") or []:
            if not isinstance(event, dict):
                continue
            ticker = event.get("event_ticker")
            if isinstance(ticker, str) and ticker:
                by_ticker[ticker] = event
            else:
                anonymous.append(event)

    return list(by_ticker.values()) + anonymous


# --- gate helpers ---------------------------------------------------------


def markets_of(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = event.get("markets")
    return [m for m in markets if isinstance(m, dict)] if isinstance(markets, list) else []


def _snapshot_sizes(
    markets: list[dict[str, Any]], size_key: str, fallback_key: str | None = None
) -> list[Decimal | None]:
    """Top-of-book sizes from a nested snapshot market.

    Nested GET /events payloads omit ``no_*_size_fp``. Kalshi's book is bids
    only, so the size you lift buying NO is the YES bid size, and the size you
    hit selling NO is the YES ask size. Price is never inferred this way.
    """
    sizes: list[Decimal | None] = []
    for market in markets:
        size = contract_count(market, size_key)
        if size is None and fallback_key is not None:
            size = contract_count(market, fallback_key)
        sizes.append(size)
    return sizes


def has_executable_route(markets: list[dict[str, Any]]) -> bool:
    """True if at least one of the four full-bundle trades can be hit.

    A route is executable only when every leg has a hittable price (strictly
    between 0 and 100 cents) and a positive resting size.
    """
    if not markets:
        return False

    yes_bids = [price_cents(m, "yes_bid_dollars") for m in markets]
    yes_asks = [price_cents(m, "yes_ask_dollars") for m in markets]
    no_bids = [price_cents(m, "no_bid_dollars") for m in markets]
    no_asks = [price_cents(m, "no_ask_dollars") for m in markets]
    yes_bid_sizes = _snapshot_sizes(markets, "yes_bid_size_fp")
    yes_ask_sizes = _snapshot_sizes(markets, "yes_ask_size_fp")
    no_ask_sizes = _snapshot_sizes(markets, "no_ask_size_fp", "yes_bid_size_fp")
    no_bid_sizes = _snapshot_sizes(markets, "no_bid_size_fp", "yes_ask_size_fp")

    return (
        route_executable(yes_bids, yes_bid_sizes)
        or route_executable(no_asks, no_ask_sizes)
        or route_executable(yes_asks, yes_ask_sizes)
        or route_executable(no_bids, no_bid_sizes)
    )


def clears_liquidity(market: dict[str, Any], config: FilterConfig) -> bool:
    """Skip dead markets, whose prices look wrong only because nobody is trading."""
    volume = contract_count(market, "volume_24h_fp") or Decimal(0)
    open_interest = contract_count(market, "open_interest_fp") or Decimal(0)

    volume_ok = volume >= config.min_volume_24h
    interest_ok = open_interest >= config.min_open_interest

    if config.liquidity_mode == "all":
        return volume_ok and interest_ok
    return volume_ok or interest_ok


def earliest_close(event: dict[str, Any]) -> datetime | None:
    """The event's effective close time: the first of its markets to close."""
    closes = [parse_iso8601(m.get("close_time")) for m in markets_of(event)]
    present = [c for c in closes if c is not None]
    return min(present) if present else None


def market_close_text(market: dict[str, Any]) -> str | None:
    """One market's own close. The event close stays the earliest of these."""
    close = parse_iso8601(market.get("close_time"))
    return close.isoformat(timespec="seconds") if close else None


def market_label(market: dict[str, Any]) -> str:
    """Human-readable name for one leg.

    yes_sub_title is the current field; title and subtitle are deprecated and used
    only as a fallback.
    """
    for key in ("yes_sub_title", "title", "subtitle"):
        value = market.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return market.get("ticker") or ""


# --- the funnel -----------------------------------------------------------


class Funnel:
    """Records how many events survive each gate, in order."""

    def __init__(self, label: str, count: int) -> None:
        self.rows: list[dict[str, Any]] = [
            {"gate": label, "surviving": count, "dropped": 0}
        ]

    def record(self, label: str, surviving: int) -> None:
        previous = self.rows[-1]["surviving"]
        self.rows.append(
            {"gate": label, "surviving": surviving, "dropped": previous - surviving}
        )

    def print(self) -> None:
        width = max(len(row["gate"]) for row in self.rows)
        print()
        print("=" * (width + 30))
        print("  SURVIVAL FUNNEL")
        print("=" * (width + 30))
        for index, row in enumerate(self.rows):
            line = f"  {row['gate']:<{width}}  {row['surviving']:>7,}"
            if index > 0:
                line += f"   (-{row['dropped']:,})"
            print(line)
        print("=" * (width + 30))


def apply_gates(
    events: list[dict[str, Any]],
    config: FilterConfig,
    *,
    opening_label: str = "open events in snapshot",
) -> tuple[list[dict[str, Any]], Funnel]:
    """Run every gate in order, building the funnel as we go."""
    funnel = Funnel(opening_label, len(events))
    survivors = events

    if config.require_mutually_exclusive:
        survivors = [e for e in survivors if e.get("mutually_exclusive") is True]
        funnel.record("flagged mutually exclusive", len(survivors))

    survivors = [e for e in survivors if len(markets_of(e)) >= config.min_markets]
    funnel.record(f"with {config.min_markets}+ markets", len(survivors))

    survivors = [e for e in survivors if len(markets_of(e)) <= config.max_markets]
    funnel.record(f"with at most {config.max_markets} markets", len(survivors))

    if config.require_all_active:
        survivors = [
            e
            for e in survivors
            if all(m.get("status") == TRADEABLE_STATUS for m in markets_of(e))
        ]
        funnel.record("every market tradeable (active)", len(survivors))

    if config.require_executable_route:
        survivors = [e for e in survivors if has_executable_route(markets_of(e))]
        funnel.record("at least one executable route", len(survivors))

    mode_word = "and" if config.liquidity_mode == "all" else "or"
    survivors = [
        e for e in survivors if all(clears_liquidity(m, config) for m in markets_of(e))
    ]
    funnel.record(
        f"above liquidity floor "
        f"(vol24h>={config.min_volume_24h:g} {mode_word} OI>={config.min_open_interest:g})",
        len(survivors),
    )

    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    for event in survivors:
        close = earliest_close(event)
        if close is None:
            continue
        minutes_away = (close - now).total_seconds() / 60
        if config.min_minutes_to_close <= minutes_away <= config.max_days_to_close * 1440:
            kept.append(event)
    survivors = kept
    funnel.record(
        f"closing in {config.min_minutes_to_close}min-{config.max_days_to_close}d",
        len(survivors),
    )

    return survivors, funnel


# --- output ---------------------------------------------------------------


def summarise(
    event: dict[str, Any], *, complement_missing_no_sizes: bool = False
) -> dict[str, Any]:
    """Keep only the fields needed later. All prices in CENTS, as strings.

    ``complement_missing_no_sizes`` fills a missing NO size from the YES book
    the executable-route gate already uses. Prices are never complemented.
    """
    markets = markets_of(event)
    close = earliest_close(event)

    def _no_sizes(market: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
        yes_bid_size = contract_count(market, "yes_bid_size_fp")
        yes_ask_size = contract_count(market, "yes_ask_size_fp")
        no_bid_size = contract_count(market, "no_bid_size_fp")
        no_ask_size = contract_count(market, "no_ask_size_fp")
        if complement_missing_no_sizes:
            if no_ask_size is None:
                no_ask_size = yes_bid_size
            if no_bid_size is None:
                no_bid_size = yes_ask_size
        return no_bid_size, no_ask_size

    return {
        "event_ticker": event.get("event_ticker"),
        "title": event.get("title"),
        "sub_title": event.get("sub_title"),
        "mutually_exclusive": event.get("mutually_exclusive"),
        "close_time": close.isoformat(timespec="seconds") if close else None,
        "close_time_basis": "earliest close_time among this event's markets",
        "market_count": len(markets),
        "markets": [
            {
                "ticker": market.get("ticker"),
                "title": market_label(market),
                "close_time": market_close_text(market),
                "yes_bid_cents": to_json(price_cents(market, "yes_bid_dollars")),
                "yes_ask_cents": to_json(price_cents(market, "yes_ask_dollars")),
                # Actual NO book, never inferred from YES. Kalshi can omit a
                # field; to_json(None) writes null rather than a complement.
                "no_bid_cents": to_json(price_cents(market, "no_bid_dollars")),
                "no_ask_cents": to_json(price_cents(market, "no_ask_dollars")),
                "volume_24h_contracts": to_json(
                    contract_count(market, "volume_24h_fp")
                ),
                "open_interest_contracts": to_json(
                    contract_count(market, "open_interest_fp")
                ),
                # Size resting at the best bid, i.e. how much could actually be
                # sold at yes_bid_cents before eating into the book. Top of book
                # only: nothing here walks depth. Confirmed present on every
                # nested market in a real snapshot, but files written before this
                # field existed simply lack it, so readers must tolerate absence.
                "yes_bid_size_contracts": to_json(
                    contract_count(market, "yes_bid_size_fp")
                ),
                "yes_ask_size_contracts": to_json(
                    contract_count(market, "yes_ask_size_fp")
                ),
                # Nested GET /events markets omit no_*_size_fp. A Greater Cycle
                # file stores the YES complement so the first Lesser can price
                # every route. Prices stay the snapshot's own NO prices.
                "no_bid_size_contracts": to_json(_no_sizes(market)[0]),
                "no_ask_size_contracts": to_json(_no_sizes(market)[1]),
            }
            for market in markets
        ],
    }


def print_survivors(events: list[dict[str, Any]], limit: int = 15) -> None:
    """Show a sample of survivors. The full set goes to the output file."""
    if not events:
        return

    # Widest ladders first: those are the most interesting to eyeball, and a
    # two-leg sports game is not news.
    ordered = sorted(events, key=lambda e: e["market_count"], reverse=True)

    print()
    print(f"Survivors (widest {min(limit, len(ordered))} of {len(ordered)}):")
    for event in ordered[:limit]:
        title = (event.get("title") or "")[:64]
        print(
            f"  {event['market_count']:>3} legs  "
            f"closes {event['close_time'][:16]}  "
            f"{event['event_ticker']:<30} {title}"
        )

    if len(ordered) > limit:
        print(f"  ... and {len(ordered) - limit} more, all in the output file.")

    spread = collections.Counter(e["market_count"] for e in ordered)
    print()
    print("  Legs per surviving event:")
    for legs in sorted(spread):
        print(f"    {legs:>3} legs : {spread[legs]:>4} events")


def main() -> int:
    enable_utf8_stdout()

    parser = argparse.ArgumentParser(
        description=(
            "Filter a saved Kalshi snapshot down to candidate mutually exclusive "
            "events. Reads a file only; never calls the API."
        )
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Snapshot to read. Defaults to the newest file in data/raw.",
    )
    args = parser.parse_args()

    try:
        config = load_filter_config()
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 1

    snapshot_path = args.snapshot or newest_snapshot()
    if not snapshot_path.is_file():
        print(f"No such snapshot: {snapshot_path}", file=sys.stderr)
        return 1

    snapshot = load_snapshot(snapshot_path)
    metadata = snapshot.get("snapshot_metadata") or {}
    environment = metadata.get("environment", "unknown")

    print(f"Snapshot    : {display_path(snapshot_path)}")
    print(f"Captured at : {metadata.get('captured_at_utc', 'unknown')}")
    print(f"Environment : {str(environment).upper()}")
    print(f"Pages       : {metadata.get('pages_fetched', 'unknown')}")
    if environment == "demo":
        print()
        print(
            "NOTE: this is DEMO data. Demo order books are synthetic and thin, so the\n"
            "      liquidity gates below say nothing about the real market."
        )

    events = extract_events(snapshot)
    if not events:
        print(
            "\nThe snapshot contained no events at all, which should not happen.",
            file=sys.stderr,
        )
        return 1

    survivors, funnel = apply_gates(events, config)
    funnel.print()

    summaries = [
        summarise(event, complement_missing_no_sizes=True) for event in survivors
    ]
    print_survivors(summaries)

    # Name the output after the SOURCE snapshot, not the current time, so it is
    # obvious which archive any candidate file came from.
    stem = snapshot_path.name
    for suffix in (".json.gz", ".json"):
        stem = stem.removesuffix(suffix)
    stem = stem.removeprefix("events_open_")

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    destination = CANDIDATES_DIR / f"candidates_{stem}.json"

    output = {
        "source_snapshot": snapshot_path.name,
        "source_snapshot_captured_at_utc": metadata.get("captured_at_utc"),
        "environment": environment,
        "filtered_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # One unit throughout: cents, 0-100, as strings so that Decimal can read
        # them back without floating-point drift.
        "price_unit": "cents",
        "price_encoding": "decimal string, e.g. '42.5' means 42.5 cents",
        "quantity_unit": "contracts, decimal string, fractional to 0.01",
        "cycle_kind": "greater",
        "sizes_source": (
            "nested snapshot; missing NO sizes filled from the YES complement "
            "(no_ask size from the YES bid, no_bid size from the YES ask). "
            "NO prices are the snapshot's own prices."
        ),
        "gates": config.as_dict(),
        "funnel": funnel.rows,
        "candidate_count": len(summaries),
        "events": summaries,
    }

    with destination.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)

    print()
    print(f"Wrote {len(summaries)} candidate events to {display_path(destination)}")
    print("Greater Cycle universe: not scored, and no watchlist was written.")
    print("Next: python refresh_quotes.py")

    if isinstance(environment, str) and is_live_candidates_file(destination, environment):
        print_prune_result(prune_rolling_window())
    return 0


if __name__ == "__main__":
    sys.exit(main())
