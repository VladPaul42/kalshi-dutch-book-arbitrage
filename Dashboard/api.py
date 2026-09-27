"""HTTP layer: a JSON API over the pipeline's candidates and watchlist files.

Data routes only ever read files. The Greater/Lesser cycle is started by the
dashboard process itself; the browser only
polls ``GET /api/cycle/status``. The trader log is the same: ``GET /api/trader``
reads Core's status file and never talks to Kalshi.
"""

from __future__ import annotations

import math
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from data_store import (
    FEE_MODEL,
    SORT_FIELDS,
    RunNotFoundError,
    RunStore,
    WatchlistStore,
    filter_rows,
    sort_rows,
)
from refresh_job import RefreshJob
from trader_job import TraderSupervisor

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
_ORDERS = ("asc", "desc")


def _parse_list_query(
    page: int,
    page_size: int,
    sort: str,
    order: str,
    min_net_edge_cents: str,
) -> tuple[int, int, str, str, Decimal | None]:
    if page < 1:
        raise HTTPException(status_code=400, detail="page must be >= 1")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise HTTPException(status_code=400, detail=f"page_size must be 1..{MAX_PAGE_SIZE}")
    if sort not in SORT_FIELDS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {list(SORT_FIELDS)}")
    if order not in _ORDERS:
        raise HTTPException(status_code=400, detail=f"order must be one of {list(_ORDERS)}")

    # Taken as text and parsed with Decimal rather than declared as a float
    # parameter: a float threshold would compare against exact cent values
    # and could include or exclude a row at the boundary for no visible reason.
    threshold: Decimal | None = None
    if min_net_edge_cents.strip():
        try:
            threshold = Decimal(min_net_edge_cents.strip())
        except (InvalidOperation, ArithmeticError):
            raise HTTPException(
                status_code=400,
                detail="min_net_edge_cents must be a decimal number of cents, e.g. '0.5'",
            ) from None
    return page, page_size, sort, order, threshold


def _public_watchlist(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    return {
        "id": payload.get("id"),
        "filename": payload.get("filename"),
        "source_candidates": payload.get("source_candidates"),
        "environment": payload.get("environment"),
        "scored_at_utc": payload.get("scored_at_utc"),
        "source_snapshot_captured_at_utc": payload.get("source_snapshot_captured_at_utc"),
        "watchlist_size": payload.get("watchlist_size"),
        "scoreable_count": payload.get("scoreable_count"),
        "history_files": payload.get("history_files") or [],
        "funnel": payload.get("funnel") or [],
        "watchlist": payload.get("watchlist") or [],
    }


def create_app(
    store: RunStore,
    watchlists: WatchlistStore,
    refresh_job: RefreshJob | None = None,
    core_dir: Path | None = None,
    cycle_idle_minutes: int = 5,
    trader_supervisor: TraderSupervisor | None = None,
) -> FastAPI:
    """Build the FastAPI app for one candidates directory and its watchlists."""
    resolved_core: Path | None = None
    if core_dir is not None:
        resolved_core = Path(core_dir)
    elif refresh_job is not None:
        resolved_core = Path(refresh_job.core_dir)

    app = FastAPI(title="Kalshi candidates dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _load(run_id: str):
        try:
            return store.load(run_id)
        except RunNotFoundError:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}") from None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # The file exists but is unusable (truncated, wrong shape). 404 keeps
            # the client's handling simple: the run is not available to view.
            raise HTTPException(status_code=404, detail=f"unreadable run: {exc}") from None

    def _cycle_status() -> dict[str, Any]:
        if refresh_job is None:
            return {
                "available": False,
                "enabled": False,
                "status": "idle",
                "step": None,
                "started_at_utc": None,
                "finished_at_utc": None,
                "error": None,
                "cycle_idle_minutes": cycle_idle_minutes,
                "core_dir": None if resolved_core is None else str(resolved_core),
                "python_executable": None,
            }
        status = refresh_job.status()
        status["cycle_idle_minutes"] = cycle_idle_minutes
        return status

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    def read_config() -> dict[str, Any]:
        """Where the data is being read from, so the UI can explain an empty state."""
        return {
            "candidates_dir": str(store.candidates_dir),
            "candidates_dir_exists": store.candidates_dir.is_dir(),
            "watchlist_dir": str(watchlists.watchlist_dir),
            "run_file_count": len(store.run_files()),
            "cycle_available": refresh_job is not None and refresh_job.available,
            "cycle_idle_minutes": cycle_idle_minutes,
            "core_dir": None if resolved_core is None else str(resolved_core),
            "trader_available": (
                trader_supervisor is not None and trader_supervisor.available
            ),
        }

    @app.get("/api/trader", response_model=None)
    def read_trader() -> Any:
        """Read Core's rolling trader status; never talks to Kalshi."""
        if resolved_core is None:
            return JSONResponse(content={}, status_code=404)
        path = resolved_core / "data" / "trader" / "status.json"
        missing = False
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
            missing = True
        if not isinstance(payload, dict):
            payload = {}
            missing = True
        else:
            payload = dict(payload)
        if trader_supervisor is not None:
            payload["supervisor"] = trader_supervisor.status()
        if missing:
            return JSONResponse(content=payload, status_code=404)
        return payload

    @app.get("/api/cycle/status")
    def read_cycle_status() -> dict[str, Any]:
        """Polled while a cycle runs, for the step label and last error."""
        return _cycle_status()

    @app.get("/api/watchlist")
    def read_watchlist(run_id: str | None = Query(None)) -> dict[str, Any]:
        """Newest watchlist, or the one matching a candidates run id."""
        payload = watchlists.for_run(run_id) if run_id else watchlists.newest()
        public = _public_watchlist(payload)
        if public is None:
            raise HTTPException(status_code=404, detail="no watchlist file found")
        return public

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        return store.list_summaries()

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str) -> dict[str, Any]:
        run = _load(run_id)
        watchlist = _public_watchlist(watchlists.for_run(run_id))
        funnel = run.funnel
        if watchlist and watchlist.get("funnel"):
            funnel = watchlist["funnel"]
        return {
            "id": run.run_id,
            "filename": run.filename,
            "meta": run.meta,
            "gates": run.gates,
            "funnel": funnel,
            "event_count": len(run.rows),
            "fee_model": FEE_MODEL,
            "watchlist": watchlist,
        }

    @app.get("/api/runs/{run_id}/events")
    def list_events(
        run_id: str,
        page: int = Query(1),
        page_size: int = Query(DEFAULT_PAGE_SIZE),
        sort: str = Query("sell_yes_net"),
        order: str = Query("desc"),
        q: str = Query(""),
        min_net_edge_cents: str = Query(""),
    ) -> dict[str, Any]:
        page, page_size, sort, order, threshold = _parse_list_query(
            page, page_size, sort, order, min_net_edge_cents
        )
        run = _load(run_id)
        selected = sort_rows(filter_rows(run.rows, q, threshold), sort, order)
        total = len(selected)
        total_pages = max(1, math.ceil(total / page_size))
        start = (page - 1) * page_size
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "items": [row.payload for row in selected[start : start + page_size]],
        }

    @app.get("/api/runs/{run_id}/events/{event_ticker}")
    def read_event(run_id: str, event_ticker: str) -> dict[str, Any]:
        run = _load(run_id)
        detail = run.details.get(event_ticker)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown event: {event_ticker}")
        return detail

    return app
