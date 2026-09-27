"""Rank Lesser Cycle survivors and write a top-N watchlist.

    python score.py                         # newest file in data/candidates
    python score.py --candidates PATH       # a specific one
    python score.py --top 10

Never calls the API. Reads a Lesser Cycle candidates file and the snapshot
named in ``previous_candidates``, and writes
data/watchlist/watchlist_{env}_{stamp}.json.

An event cannot enter the watchlist unless all four features are computable.
A Greater Cycle universe file is refused, so the last watchlist stays in force.

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
from kalshi.mispricing import ROUTE_IDS, quote_best_edge, route_nets
from kalshi.retention import (
    is_live_candidates_file,
    print_prune_result,
    prune_rolling_window,
)

_RANK_QUANTUM = Decimal("0.00000001")
# Net cents per bundle at which the edge percentile is weighted more heavily.
_EDGE_WEIGHT_NET_CENTS = Decimal(1)
_EDGE_WEIGHT = Decimal(2)
_EDGE_WEIGHT_AT_NET_FLOOR = Decimal(4)


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


def events_by_ticker(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        ticker = event.get("event_ticker")
        if not isinstance(ticker, str) or not ticker:
            continue
        indexed[ticker] = event
    return indexed


def load_previous_events(current: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Events from the snapshot this file names as its comparison reference."""
    name = current.get("previous_candidates")
    if not isinstance(name, str) or not name or Path(name).name != name:
        return {}
    path = CANDIDATES_DIR / name
    if not path.is_file():
        return {}
    return events_by_ticker(load_candidates(path))


# --- features -------------------------------------------------------------


def _fmt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _fmt_rank(value: Decimal) -> str:
    return format(value.quantize(_RANK_QUANTUM), "f")


def best_edge_route_delta(
    current_event: dict[str, Any], previous_event: dict[str, Any] | None
) -> Decimal | None:
    """This cycle's best-edge route net, minus that same route's previous net.

    An increase is better: -20¢ to -18¢ is +2¢. None when this cycle has no
    best route, or that route was not priced on the previous snapshot.
    """
    if previous_event is None:
        return None
    quoted = quote_best_edge(current_event)
    if quoted.route_id is None or quoted.net_per_bundle is None:
        return None
    previous_net = route_nets(previous_event).get(quoted.route_id)
    if previous_net is None:
        return None
    return quoted.net_per_bundle - previous_net


def best_route_delta(
    current_event: dict[str, Any], previous_event: dict[str, Any] | None
) -> Decimal | None:
    """Largest route-net increase versus the previous snapshot, any route.

    The route does not have to be the best-edge route. An increase is better.
    None when no route is priced on both snapshots.
    """
    if previous_event is None:
        return None
    current_nets = route_nets(current_event)
    previous_nets = route_nets(previous_event)
    best: Decimal | None = None
    for route_id in ROUTE_IDS:
        now = current_nets.get(route_id)
        then = previous_nets.get(route_id)
        if now is None or then is None:
            continue
        delta = now - then
        if best is None or delta > best:
            best = delta
    return best


def turnover_velocity(event: dict[str, Any]) -> Decimal | None:
    """max(volume_24h) / max(open_interest) across legs. None if the ratio is unusable."""
    volumes: list[Decimal] = []
    interests: list[Decimal] = []
    markets = event.get("markets") or []
    if not markets:
        return None
    for market in markets:
        if not isinstance(market, dict):
            return None
        volume = to_decimal(market.get("volume_24h_contracts"))
        interest = to_decimal(market.get("open_interest_contracts"))
        if volume is None or interest is None or volume < 0 or interest < 0:
            return None
        volumes.append(volume)
        interests.append(interest)
    max_interest = max(interests)
    if max_interest <= 0:
        return None
    return max(volumes) / max_interest


# --- ranking --------------------------------------------------------------


def percentile_ranks(values: list[Decimal]) -> list[Decimal]:
    """0-1 percentile ranks, higher raw value → higher rank. Average ranks for ties."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [Decimal(1)]

    order = sorted(range(n), key=lambda index: values[index])
    ranks = [Decimal(0)] * n
    index = 0
    while index < n:
        end = index
        while end + 1 < n and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = Decimal(index + end) / Decimal(2)
        for cursor in range(index, end + 1):
            ranks[order[cursor]] = average
        index = end + 1
    denom = Decimal(n - 1)
    return [rank / denom for rank in ranks]


def score_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach within-pool percentiles and a weighted-sum score, then rank.

    Edge percentile has weight 4 when net best edge is at least 1 cent,
    otherwise weight 2. The two route-net deltas and turnover have weight 1.
    A qualifying row can reach 7; the others still top out at 5.
    """
    if not rows:
        return []

    features = (
        "best_edge",
        "best_edge_route_delta",
        "best_route_delta",
        "turnover_velocity",
    )
    percentiles = {
        name: percentile_ranks([row[name] for row in rows]) for name in features
    }
    scored: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        parts = [percentiles[name][index] for name in features]
        edge_weight = (
            _EDGE_WEIGHT_AT_NET_FLOOR
            if row["best_edge"] >= _EDGE_WEIGHT_NET_CENTS
            else _EDGE_WEIGHT
        )
        score = edge_weight * parts[0] + parts[1] + parts[2] + parts[3]
        scored.append(
            {
                **row,
                "percentile_best_edge": parts[0],
                "percentile_best_edge_route_delta": parts[1],
                "percentile_best_route_delta": parts[2],
                "percentile_turnover_velocity": parts[3],
                "score": score,
            }
        )
    scored.sort(
        key=lambda row: (
            -row["score"],
            -row["best_edge"],
            str(row["event_ticker"] or ""),
        )
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
        "best_edge_route_delta": _fmt(row["best_edge_route_delta"]),
        "best_route_delta": _fmt(row["best_route_delta"]),
        "turnover_velocity": _fmt(row["turnover_velocity"]),
        "percentile_best_edge": _fmt_rank(row["percentile_best_edge"]),
        "percentile_best_edge_route_delta": _fmt_rank(row["percentile_best_edge_route_delta"]),
        "percentile_best_route_delta": _fmt_rank(row["percentile_best_route_delta"]),
        "percentile_turnover_velocity": _fmt_rank(row["percentile_turnover_velocity"]),
        "score": _fmt_rank(row["score"]),
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
    compared = load_previous_events(current)

    events = [event for event in (current.get("events") or []) if isinstance(event, dict)]
    funnel = snapshot_funnel(current)
    previous_count = (
        funnel[-1]["surviving"]
        if funnel and isinstance(funnel[-1].get("surviving"), int)
        else len(events)
    )

    with_edge: list[dict[str, Any]] = []
    for event in events:
        quoted = quote_best_edge(event)
        if quoted.net_per_bundle is None:
            continue
        with_edge.append(
            {
                "event": event,
                "event_ticker": event.get("event_ticker"),
                "title": event.get("title"),
                "best_route": quoted.route_id,
                "best_edge": quoted.net_per_bundle,
            }
        )
    previous_count = append_gate(
        funnel, "computable best_edge", len(with_edge), previous_count
    )

    with_edge_delta: list[dict[str, Any]] = []
    for row in with_edge:
        edge_delta = best_edge_route_delta(row["event"], compared.get(row["event_ticker"]))
        if edge_delta is None:
            continue
        with_edge_delta.append({**row, "best_edge_route_delta": edge_delta})
    previous_count = append_gate(
        funnel,
        "computable best_edge_route_delta",
        len(with_edge_delta),
        previous_count,
    )

    with_route_delta: list[dict[str, Any]] = []
    for row in with_edge_delta:
        route_delta = best_route_delta(row["event"], compared.get(row["event_ticker"]))
        if route_delta is None:
            continue
        with_route_delta.append({**row, "best_route_delta": route_delta})
    previous_count = append_gate(
        funnel, "computable best_route_delta", len(with_route_delta), previous_count
    )

    scoreable_raw: list[dict[str, Any]] = []
    for row in with_route_delta:
        velocity = turnover_velocity(row["event"])
        if velocity is None:
            continue
        scoreable_raw.append({**row, "turnover_velocity": velocity})
    append_gate(
        funnel, "computable turnover_velocity", len(scoreable_raw), previous_count
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
        "previous_candidates": current.get("previous_candidates"),
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
        print(
            "Watchlist is empty. Route-net deltas need this snapshot and the "
            "previous one, plus a computable best edge and turnover."
        )
        return
    print()
    print(f"Watchlist (top {len(rows)}):")
    for row in rows:
        print(
            f"  {row['rank']:>2}  score {row['score']:<12}  "
            f"edge {row['best_edge']:<12}  {row['event_ticker']}  "
            f"{(row.get('title') or '')[:48]}"
        )


def main() -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=(
            "Score snapshot-filtered candidates and write a top-N watchlist. "
            "Reads files only; never calls the API."
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
