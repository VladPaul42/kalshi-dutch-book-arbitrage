"""Supervise Core/trade.py as a long-running sibling process."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from refresh_job import resolve_core_python


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TraderSupervisor:
    """Start the trader once and restart it on unexpected exit with backoff."""

    def __init__(self, core_dir: Path, enabled: bool = True) -> None:
        self.core_dir = core_dir.resolve()
        self.enabled = enabled
        self.python_executable = resolve_core_python(self.core_dir)
        self.script = self.core_dir / "trade.py"
        values = dotenv_values(self.core_dir / ".env")
        raw_dry = str(values.get("TRADER_DRY_MODE", "true")).strip().lower()
        self.dry_mode = raw_dry not in ("0", "false", "no", "off")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._last_exit_code: int | None = None
        self._last_started_at: str | None = None
        self._restart_delay_seconds = 1

    def missing_pieces(self) -> list[str]:
        missing: list[str] = []
        if not self.core_dir.is_dir():
            return [f"Core directory not found: {self.core_dir}"]
        if not self.script.is_file():
            missing.append(f"script not found: {self.script}")
        if not self.python_executable.is_file():
            missing.append(f"interpreter not found: {self.python_executable}")
        return missing

    @property
    def available(self) -> bool:
        return self.enabled and not self.missing_pieces()

    def start(self) -> None:
        if not self.available or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervise, name="trader-supervisor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            running = process is not None and process.poll() is None
            return {
                "available": self.available,
                "enabled": self.enabled,
                "process_up": running,
                "pid": process.pid if running else None,
                "dry_mode": self.dry_mode,
                "last_exit_code": self._last_exit_code,
                "last_started_at_utc": self._last_started_at,
                "restart_delay_seconds": self._restart_delay_seconds,
            }

    def _supervise(self) -> None:
        delay = 1
        while not self._stop.is_set():
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            started = time.monotonic()
            try:
                process = subprocess.Popen(
                    [str(self.python_executable), str(self.script.name)],
                    cwd=str(self.core_dir),
                    stdin=subprocess.DEVNULL,
                    env=env,
                )
            except OSError:
                with self._lock:
                    self._last_exit_code = -1
                    self._restart_delay_seconds = delay
                if self._stop.wait(delay):
                    return
                delay = min(delay * 2, 30)
                continue

            with self._lock:
                self._process = process
                self._last_started_at = _utc_now()
                self._restart_delay_seconds = delay
            while process.poll() is None and not self._stop.wait(1):
                pass
            if self._stop.is_set():
                return
            code = process.wait()
            runtime = time.monotonic() - started
            delay = 1 if runtime >= 30 else min(delay * 2, 30)
            with self._lock:
                self._last_exit_code = code
                self._process = None
                self._restart_delay_seconds = delay
            if self._stop.wait(delay):
                return
