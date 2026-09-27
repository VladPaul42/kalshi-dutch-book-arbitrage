"""Runs Greater and Lesser cycles, idling only after each cycle's work finishes.

This is one of two places the dashboard starts a Core process (the other is
``trader_job.py``). It does so by SUBPROCESS, not by import. The dashboard
therefore still has no Kalshi client, no credential handling and no pipeline
code of its own.

One Greater Cycle (collect, then filter) is followed by ten Lesser Cycles
(refresh quotes, then score). A failed cycle is retried after the idle; the
phase does not advance, and a Greater Cycle never runs score.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

GREATER_STEPS: tuple[tuple[str, str], ...] = (
    ("collect", "collect.py"),
    ("filter", "filter_candidates.py"),
)
LESSER_STEPS: tuple[tuple[str, str], ...] = (
    ("refresh", "refresh_quotes.py"),
    ("score", "score.py"),
)
LESSER_CYCLES = 10

# Each step gets its own clock, started when that process is spawned.
STEP_TIMEOUT_SECONDS = 3600

MAX_LOG_LINES = 1000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_core_python(core_dir: Path) -> Path:
    """Pick the interpreter that can actually import the pipeline's dependencies.

    Core and Dashboard have SEPARATE virtualenvs, and this matters: the dashboard's
    venv has FastAPI and uvicorn but none of ``httpx``, ``cryptography``,
    ``truststore`` or ``python-dotenv``. Running the collector with
    ``sys.executable`` would die on ``import httpx`` before making a single
    request, so Core's own venv is preferred and the current interpreter is only a
    last resort (which is the right answer when the two share one venv).
    """
    candidates = (
        core_dir / ".venv" / "Scripts" / "python.exe",  # Windows
        core_dir / ".venv" / "bin" / "python",  # posix
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


class RefreshJob:
    """Runs the Greater / Lesser sequence on a background thread.

    ``run_forever`` blocks that thread: it waits until the cycle's work
    finishes, records ``idle_until_utc``, then sleeps. A restart reads
    ``data/cycle_state.json`` and resumes; a missing universe file starts at
    a Greater Cycle.
    """

    def __init__(self, core_dir: Path, enabled: bool = True, idle_minutes: int = 5) -> None:
        self.core_dir = core_dir.resolve()
        self.enabled = enabled
        self.idle_minutes = idle_minutes
        self.python_executable = resolve_core_python(self.core_dir)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = "idle"
        self._step: str | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._idle_until: str | None = None
        self._error: str | None = None
        self._log: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._phase = "greater"
        self._lessers_completed = 0
        self._universe_filename: str | None = None
        self._load_state()

    # -- availability -----------------------------------------------------

    def script_path(self, script: str) -> Path:
        return self.core_dir / script

    def missing_pieces(self) -> list[str]:
        """Everything needed but absent, as display strings. Empty means ready."""
        missing: list[str] = []
        if not self.core_dir.is_dir():
            return [f"pipeline directory not found: {self.core_dir}"]
        scripts = [script for _, script in GREATER_STEPS + LESSER_STEPS]
        for script in scripts:
            if not self.script_path(script).is_file():
                missing.append(f"script not found: {self.script_path(script)}")
        if not self.python_executable.is_file():
            missing.append(f"interpreter not found: {self.python_executable}")
        return missing

    @property
    def available(self) -> bool:
        """Whether a cycle could run right now if asked."""
        return self.enabled and not self.missing_pieces()

    # -- control ----------------------------------------------------------

    def run_forever(self) -> None:
        """Greater, idle, ten Lessers, idle after each, then Greater again."""
        while True:
            if not self.available:
                self._sleep_idle()
                continue
            self._ensure_universe()
            self._begin_run()
            error: str | None
            try:
                error = self._run_steps(self._steps_for_phase())
            except OSError as exc:
                error = f"could not run the pipeline: {exc}"
            self._finish(error)
            if error is None:
                self._advance_phase()
            self._save_state()
            self._sleep_idle()

    def status(self) -> dict[str, Any]:
        """A JSON-ready snapshot of where the job is."""
        available = self.available
        with self._lock:
            kind = self._phase
            index = self._lessers_completed + 1 if kind == "lesser" else None
            return {
                "available": available,
                "enabled": self.enabled,
                "status": self._status,
                "step": self._step,
                "started_at_utc": self._started_at,
                "finished_at_utc": self._finished_at,
                "idle_until_utc": self._idle_until,
                "error": self._error,
                "log": list(self._log),
                "core_dir": str(self.core_dir),
                "python_executable": str(self.python_executable),
                "cycle_kind": kind,
                "lesser_index": index,
                "lesser_count": LESSER_CYCLES,
                "next_label": self._phase_label_locked(),
                "cycle_idle_minutes": self.idle_minutes,
            }

    # -- phase ------------------------------------------------------------

    def _phase_label_locked(self) -> str:
        if self._phase == "greater":
            return "greater cycle"
        return f"lesser {self._lessers_completed + 1}/{LESSER_CYCLES}"

    def _steps_for_phase(self) -> tuple[tuple[str, str], ...]:
        return GREATER_STEPS if self._phase == "greater" else LESSER_STEPS

    def _state_path(self) -> Path:
        return self.core_dir / "data" / "cycle_state.json"

    def _candidates_dir(self) -> Path:
        return self.core_dir / "data" / "candidates"

    def _load_state(self) -> None:
        path = self._state_path()
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        phase = payload.get("phase")
        completed = payload.get("lessers_completed")
        universe = payload.get("universe_filename")
        if phase in ("greater", "lesser"):
            self._phase = phase
        if isinstance(completed, int) and 0 <= completed <= LESSER_CYCLES:
            self._lessers_completed = completed
        if isinstance(universe, str) and universe == Path(universe).name:
            self._universe_filename = universe
        self._ensure_universe()

    def _save_state(self) -> None:
        path = self._state_path()
        payload = {
            "phase": self._phase,
            "lessers_completed": self._lessers_completed,
            "universe_filename": self._universe_filename,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".partial")
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temp, path)
        except OSError as exc:
            self._append(f"[could not save cycle state: {exc}]")

    def _universe_path(self) -> Path | None:
        if not self._universe_filename:
            return None
        return self._candidates_dir() / self._universe_filename

    def _ensure_universe(self) -> None:
        """A Lesser with no universe file on disk starts over at Greater."""
        if self._phase != "lesser":
            return
        path = self._universe_path()
        if path is not None and path.is_file():
            return
        self._phase = "greater"
        self._lessers_completed = 0
        self._universe_filename = None

    def _advance_phase(self) -> None:
        if self._phase == "greater":
            self._universe_filename = self._newest_greater_name()
            self._phase = "lesser"
            self._lessers_completed = 0
            return
        self._lessers_completed += 1
        if self._lessers_completed >= LESSER_CYCLES:
            self._phase = "greater"
            self._lessers_completed = 0

    def _newest_greater_name(self) -> str | None:
        directory = self._candidates_dir()
        if not directory.is_dir():
            return None
        newest_name: str | None = None
        newest_stamp = ""
        for path in directory.glob("candidates_*.json"):
            if path.name.endswith(".partial"):
                continue
            parts = path.name.removesuffix(".json").split("_", 2)
            # candidates_{env}_{stamp}
            if len(parts) != 3:
                continue
            stamp = parts[2]
            if stamp < newest_stamp:
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("cycle_kind") == "greater":
                newest_stamp = stamp
                newest_name = path.name
        return newest_name

    # -- the work ---------------------------------------------------------

    def _append(self, line: str) -> None:
        with self._lock:
            self._log.append(line)

    def _begin_run(self) -> None:
        with self._lock:
            self._status = "running"
            self._step = self._steps_for_phase()[0][0]
            self._started_at = _utc_now()
            self._finished_at = None
            self._idle_until = None
            self._error = None
            self._log.clear()

    def _finish(self, error: str | None) -> None:
        with self._lock:
            self._status = "failed" if error else "succeeded"
            self._step = None
            self._error = error
            self._finished_at = _utc_now()

    def _sleep_idle(self) -> None:
        minutes = max(self.idle_minutes, 1)
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        with self._lock:
            self._idle_until = until.isoformat(timespec="seconds")
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            time.sleep(remaining)
        with self._lock:
            self._idle_until = None

    def _run_steps(self, steps: tuple[tuple[str, str], ...]) -> str | None:
        """Run each step in order. Returns an error string, or None on success."""
        for step, script in steps:
            with self._lock:
                self._step = step
            extra: list[str] = []
            if script == "refresh_quotes.py" and self._universe_filename:
                universe = self._candidates_dir() / self._universe_filename
                extra = ["--universe", str(universe)]
                self._append(
                    f"$ {self.python_executable.name} {script} --universe {universe.name}"
                )
            else:
                self._append(f"$ {self.python_executable.name} {script}")
            code = self._run_script(script, STEP_TIMEOUT_SECONDS, extra)
            if code != 0:
                return f"{script} exited with code {code}"
            self._append("")
        return None

    def _run_script(self, script: str, timeout: int, extra: list[str] | None = None) -> int:
        """Run one script to completion, streaming its output into the log."""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        process = subprocess.Popen(
            [str(self.python_executable), script, *(extra or [])],
            cwd=str(self.core_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        pump = threading.Thread(target=self._pump, args=(process,), daemon=True)
        pump.start()

        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
            timed_out = True

        pump.join(timeout=5)
        if timed_out:
            self._append(f"[timed out after {timeout}s; process killed]")
        return code

    def _pump(self, process: subprocess.Popen[str]) -> None:
        """Copy one process's output into the log until the pipe closes."""
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                self._append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            stream.close()
