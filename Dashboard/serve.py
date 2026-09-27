"""Entry point for the Kalshi candidates dashboard.

Run it with ``python serve.py`` and open the URL it prints. The dashboard reads
``candidates_*.json`` and ``watchlist_*.json`` files written by the research
pipeline. It supervises Core's long-running trader as a separate process and,
unless ``--no-cycle`` is set, runs one Greater Cycle then ten Lesser Cycles,
idling only after each cycle's work finishes.

    python serve.py
    python serve.py --candidates-dir D:\\runs --port 9000
    python serve.py --no-cycle
    python serve.py --no-trader
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

import uvicorn

from api import create_app
from data_store import RunStore, WatchlistStore
from refresh_job import RefreshJob
from trader_job import TraderSupervisor

DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CYCLE_IDLE_MINUTES = 5

# The pipeline lives in the sibling Core folder: its output directory is read for
# candidate runs and watchlists, and its scripts are what the cycle starts.
DEFAULT_CORE_DIR = Path(__file__).resolve().parent.parent / "Core"
DEFAULT_CANDIDATES_DIR = DEFAULT_CORE_DIR / "data" / "candidates"
DEFAULT_WATCHLIST_DIR = DEFAULT_CORE_DIR / "data" / "watchlist"


def resolve_candidates_dir(cli_value: str | None) -> Path:
    """Pick the candidates directory: CLI flag, then CANDIDATES_DIR, then default."""
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    from_env = os.environ.get("CANDIDATES_DIR")
    if from_env:
        return Path(from_env).expanduser().resolve()
    return DEFAULT_CANDIDATES_DIR


def resolve_watchlist_dir(cli_value: str | None, candidates_dir: Path) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    from_env = os.environ.get("WATCHLIST_DIR")
    if from_env:
        return Path(from_env).expanduser().resolve()
    if candidates_dir == DEFAULT_CANDIDATES_DIR:
        return DEFAULT_WATCHLIST_DIR
    return candidates_dir.parent / "watchlist"


def resolve_core_dir(cli_value: str | None) -> Path:
    """Pick the pipeline directory: CLI flag, then CORE_DIR, then the sibling folder."""
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    from_env = os.environ.get("CORE_DIR")
    if from_env:
        return Path(from_env).expanduser().resolve()
    return DEFAULT_CORE_DIR


def load_cycle_idle_minutes(core_dir: Path) -> int:
    """Read CYCLE_IDLE_MINUTES from Core/.env, falling back to 5."""
    resolved = str(core_dir.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    try:
        from kalshi.config import ConfigError, load_score_config
    except ImportError:
        return DEFAULT_CYCLE_IDLE_MINUTES
    try:
        return load_score_config().cycle_idle_minutes
    except ConfigError:
        return DEFAULT_CYCLE_IDLE_MINUTES


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="serve.py",
        description="Local web dashboard for the Kalshi candidates funnel.",
    )
    parser.add_argument(
        "--candidates-dir",
        default=None,
        help="Directory holding candidates_*.json (default: ../Core/data/candidates).",
    )
    parser.add_argument(
        "--watchlist-dir",
        default=None,
        help="Directory holding watchlist_*.json (default: ../Core/data/watchlist).",
    )
    parser.add_argument(
        "--core-dir",
        default=None,
        help="Pipeline directory for cycle scripts (default: ../Core).",
    )
    parser.add_argument(
        "--no-cycle",
        action="store_true",
        help="Do not start the Greater/Lesser cycle loop.",
    )
    parser.add_argument(
        "--no-trader",
        action="store_true",
        help="Do not start or supervise Core/trade.py.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"default {DEFAULT_HOST}; this is a local research tool, not a server",
    )
    return parser.parse_args(argv)


def _force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 before anything prints a path.

    This project's absolute path contains Cyrillic characters, and a Windows
    console defaults to cp1252, which raises UnicodeEncodeError the moment such a
    path is printed.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def start_cycle_scheduler(job: RefreshJob) -> None:
    """Run the Greater/Lesser sequence. The job itself idles after each cycle."""
    threading.Thread(target=job.run_forever, name="cycle-scheduler", daemon=True).start()


def print_banner(
    store: RunStore,
    watchlists: WatchlistStore,
    job: RefreshJob,
    host: str,
    port: int,
    idle_minutes: int,
    trader: TraderSupervisor,
) -> None:
    """Report where data is read from, how much was found, and where to browse."""
    print("Kalshi candidates dashboard")
    print(f"  candidates dir : {store.candidates_dir}")
    print(f"  watchlist dir  : {watchlists.watchlist_dir}")

    if not job.enabled:
        print("  cycle          : disabled (--no-cycle)")
    elif job.available:
        print(
            f"  cycle          : greater, then 10 lesser, idle {idle_minutes} min after each, "
            f"runs {job.python_executable}"
        )
    else:
        print("  cycle          : unavailable")
        for problem in job.missing_pieces():
            print(f"                   {problem}")

    if not trader.enabled:
        print("  trader         : disabled (--no-trader)")
    elif trader.available:
        process = "up" if trader.status()["process_up"] else "starting"
        mode = "DRY" if trader.dry_mode else "LIVE"
        print(f"  trader         : {mode}, process {process}, supervised with restart")
    else:
        print("  trader         : unavailable")
        for problem in trader.missing_pieces():
            print(f"                   {problem}")

    no_data = not store.candidates_dir.is_dir() or not store.run_files()
    if not store.candidates_dir.is_dir():
        print("  run files      : directory does not exist")
    else:
        print(f"  run files      : {len(store.run_files())}")

    if no_data:
        print()
        print("  No data yet. Leave this running for the first cycle, or in Core run:")
        print("      python collect.py")
        print("      python filter_candidates.py")
        print("      python refresh_quotes.py")
        print("      python score.py")
        print("  then refresh the browser; no restart needed.")

    print()
    print(f"  open: http://{host}:{port}/")
    print()


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = parse_args(argv)
    store = RunStore(resolve_candidates_dir(args.candidates_dir))
    watchlists = WatchlistStore(resolve_watchlist_dir(args.watchlist_dir, store.candidates_dir))
    core_dir = resolve_core_dir(args.core_dir)
    idle_minutes = load_cycle_idle_minutes(core_dir)
    job = RefreshJob(core_dir, enabled=not args.no_cycle, idle_minutes=idle_minutes)
    trader = TraderSupervisor(core_dir, enabled=not args.no_trader)
    trader.start()
    print_banner(
        store, watchlists, job, args.host, args.port, idle_minutes, trader
    )
    if job.available:
        start_cycle_scheduler(job)
    app = create_app(
        store,
        watchlists,
        job,
        core_dir,
        cycle_idle_minutes=idle_minutes,
        trader_supervisor=trader,
    )
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
        )
    finally:
        trader.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
