"""Rank Lesser Cycle survivors and write a top-N watchlist.

    python score.py                         # newest file in data/candidates
    python score.py --candidates PATH       # a specific one
    python score.py --top 10

Never calls the API. Reads a Lesser Cycle candidates file and writes
data/watchlist/watchlist_{env}_{stamp}.json.

The score is the busiest leg's 24-hour volume. An event whose volume is
missing or negative stays out of the ranking. A Greater Cycle universe file
is refused, so the last watchlist stays in force.

After a live score (the newest same-environment candidates file) retention
keeps the pinned Greater file, the two newest quote snapshots, and the
in-force watchlist.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi.config import (
    CANDIDATES_DIR,
    WATCHLIST_DIR,
    ConfigError,
    ScoreConfig,
    display_path,
    enable_utf8_stdout,
    load_score_config,
)
from kalshi.fixedpoint import to_decimal
from kalshi.mispricing import quote_best_edge
from kalshi.retention import (
    is_live_candidates_file,
    print_prune_result,
    prune_rolling_window,
)


# --- loading --------------------------------------------------------------


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


# --- features -------------------------------------------------------------


def _fmt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def volume_24h(event: dict[str, Any]) -> Decimal | None:
    """Largest ``volume_24h_contracts`` across legs.

    None when the event has no markets, a market is not an object, or any leg
    is missing a volume or has a negative one. Zero is a usable score.
    """
    markets = event.get("markets") or []
    if not markets:
        return None
    volumes: list[Decimal] = []
    for market in markets:
        if not isinstance(market, dict):
            return None
        volume = to_decimal(market.get("volume_24h_contracts"))
        if volume is None or volume < 0:
            return None
        volumes.append(volume)
    return max(volumes)


def score_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by 24-hour volume, highest first. Ties break on event ticker."""
    if not rows:
        return []
    scored = sorted(
        rows,
        key=lambda row: (-row["score"], str(row["event_ticker"] or "")),
    )
    for rank, row in enumerate(scored, start=1):
        row["rank"] = rank
    return scored


def serialise_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": row["rank"],
        "event_ticker": row["event_ticker"],
        "title": row["title"],
        "best_route": row["best_route"],
        "best_edge": _fmt(row["best_edge"]),
        "score": _fmt(row["score"]),
    }


# --- funnel ---------------------------------------------------------------


def snapshot_funnel(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("funnel")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def append_gate(
    funnel: list[dict[str, Any]], label: str, surviving: int, previous: int
) -> int:
    funnel.append(
        {"gate": label, "surviving": surviving, "dropped": previous - surviving}
    )
    return surviving


def print_funnel(funnel: list[dict[str, Any]]) -> None:
    if not funnel:
        return
    width = max(len(str(row.get("gate") or "")) for row in funnel)
    print()
    print("=" * (width + 30))
    print("  SURVIVAL FUNNEL")
    print("=" * (width + 30))
    for index, row in enumerate(funnel):
        surviving = row.get("surviving")
        line = f"  {str(row.get('gate') or ''):<{width}}  {surviving:>7,}"
        if index > 0:
            dropped = row.get("dropped") or 0
            line += f"   (-{dropped:,})"
        print(line)
    print("=" * (width + 30))


# --- main -----------------------------------------------------------------


def watchlist_destination(candidates_path: Path) -> Path:
    stem = candidates_path.name.removesuffix(".json").removeprefix("candidates_")
    return WATCHLIST_DIR / f"watchlist_{stem}.json"


def score_candidates(
    candidates_path: Path, config: ScoreConfig
) -> tuple[dict[str, Any], Path]:
    current = load_candidates(candidates_path)
    if current.get("cycle_kind") == "greater":
        raise SystemExit(
            "Refusing to score a Greater Cycle universe file. "
            "The last watchlist stays in force."
        )

    events = [event for event in (current.get("events") or []) if isinstance(event, dict)]
    funnel = snapshot_funnel(current)
    previous_count = (
        funnel[-1]["surviving"]
        if funnel and isinstance(funnel[-1].get("surviving"), int)
        else len(events)
    )

    scoreable_raw: list[dict[str, Any]] = []
    for event in events:
        volume = volume_24h(event)
        if volume is None:
            continue
        quoted = quote_best_edge(event)
        scoreable_raw.append(
            {
                "event_ticker": event.get("event_ticker"),
                "title": event.get("title"),
                "best_route": quoted.route_id,
                "best_edge": quoted.net_per_bundle,
                "score": volume,
            }
        )
    append_gate(
        funnel, "computable volume_24h", len(scoreable_raw), previous_count
    )

    ranked = score_rows(scoreable_raw)
    top_n = ranked[: config.watchlist_size]
    serialised_pool = [serialise_event(row) for row in ranked]
    serialised_watchlist = [serialise_event(row) for row in top_n]

    output = {
        "source_candidates": candidates_path.name,
        "source_snapshot": current.get("source_snapshot"),
        "source_snapshot_captured_at_utc": current.get("source_snapshot_captured_at_utc"),
        "environment": current.get("environment"),
        "scored_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cycle_kind": current.get("cycle_kind"),
        "watchlist_size": config.watchlist_size,
        "scoreable_count": len(ranked),
        "gates": {
            **(current.get("gates") if isinstance(current.get("gates"), dict) else {}),
            **config.as_dict(),
        },
        "funnel": funnel,
        "scoreable": serialised_pool,
        "watchlist": serialised_watchlist,
    }
    return output, watchlist_destination(candidates_path)


def print_watchlist(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print()
        print("Watchlist is empty. Every event was missing a usable 24h volume.")
        return
    print()
    print(f"Watchlist (top {len(rows)}):")
    for row in rows:
        print(
            f"  {row['rank']:>2}  24h vol {row['score']:<14}  "
            f"edge {row['best_edge'] or '-':<12}  {row['event_ticker']}  "
            f"{(row.get('title') or '')[:48]}"
        )


def main() -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=(
            "Score snapshot-filtered candidates by 24h volume and write a "
            "top-N watchlist. Reads files only; never calls the API."
        )
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="Candidates file to score. Defaults to the newest file in data/candidates.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Watchlist length. Defaults to WATCHLIST_SIZE in .env (10).",
    )
    args = parser.parse_args()

    try:
        config = load_score_config()
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 1

    if args.top is not None:
        if args.top < 1:
            print("--top must be at least 1.", file=sys.stderr)
            return 1
        config = ScoreConfig(
            watchlist_size=args.top,
            cycle_idle_minutes=config.cycle_idle_minutes,
        )

    candidates_path = args.candidates or newest_candidates()
    if not candidates_path.is_file():
        print(f"No such candidates file: {candidates_path}", file=sys.stderr)
        return 1

    output, destination = score_candidates(candidates_path, config)
    print_funnel(output["funnel"])
    print_watchlist(output["watchlist"])

    WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)

    print()
    print(
        f"Wrote {output['scoreable_count']} scoreable events "
        f"({len(output['watchlist'])} on the watchlist) to {display_path(destination)}"
    )

    environment = output.get("environment")
    if isinstance(environment, str) and is_live_candidates_file(
        candidates_path, environment
    ):
        print_prune_result(prune_rolling_window())
    else:
        print()
        print(
            "Retention: skipped (this is not the newest candidates file "
            "for this environment)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
