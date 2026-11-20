"""Keep the pinned Greater universe, the two newest quote snapshots, and one raw file.

Every Lesser reloads the Greater file for membership. Demo and production are
pruned independently. The newest watchlist is always kept, so a Greater Cycle
does not take the in-force list off disk.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from kalshi.config import CANDIDATES_DIR, RAW_DIR, WATCHLIST_DIR, display_path

# The two newest quote files. The pinned Greater universe is kept separately.
KEEP_QUOTE_SNAPSHOTS_PER_ENV = 2
KEEP_RAW_PER_ENV = 1

_FILE_RE = re.compile(
    r"^(?P<kind>events_open|candidates|watchlist)_"
    r"(?P<env>demo|production)_"
    r"(?P<stamp>\d{8}T\d{6}Z)"
    r"(?P<suffix>\.json(?:\.gz)?)$"
)


@dataclass(frozen=True)
class PipelineFile:
    path: Path
    kind: str
    environment: str
    stamp: str


@dataclass
class PruneResult:
    deleted: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


def parse_pipeline_filename(name: str) -> PipelineFile | None:
    """Read kind, environment and UTC stamp from a pipeline filename."""
    if name.endswith(".partial"):
        return None
    matched = _FILE_RE.match(name)
    if matched is None:
        return None
    return PipelineFile(
        path=Path(name),
        kind=matched.group("kind"),
        environment=matched.group("env"),
        stamp=matched.group("stamp"),
    )


def list_pipeline_files(directory: Path, kind: str) -> list[PipelineFile]:
    """Named pipeline files of one kind in ``directory``, unmatched names skipped."""
    if not directory.is_dir():
        return []
    found: list[PipelineFile] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        parsed = parse_pipeline_filename(path.name)
        if parsed is None or parsed.kind != kind:
            continue
        found.append(
            PipelineFile(
                path=path,
                kind=parsed.kind,
                environment=parsed.environment,
                stamp=parsed.stamp,
            )
        )
    found.sort(key=lambda row: (row.stamp, row.path.name), reverse=True)
    return found


def newest_candidates_for_env(environment: str, directory: Path | None = None) -> Path | None:
    """Newest ``candidates_*.json`` for ``environment`` by filename UTC stamp."""
    files = [
        row
        for row in list_pipeline_files(directory or CANDIDATES_DIR, "candidates")
        if row.environment == environment
    ]
    if not files:
        return None
    return files[0].path


def is_live_candidates_file(path: Path, environment: str) -> bool:
    """True when ``path`` is the newest same-environment candidates file."""
    newest = newest_candidates_for_env(environment)
    if newest is None:
        return False
    try:
        return path.resolve() == newest.resolve()
    except OSError:
        return False


def cycle_kind_of(path: Path) -> str | None:
    """``greater`` or ``lesser`` from a candidates file, or None if it has neither."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("cycle_kind")
    if kind in ("greater", "lesser"):
        return kind
    return None


def _prune_kind(directory: Path, kind: str, keep: int) -> PruneResult:
    grouped: dict[str, list[PipelineFile]] = defaultdict(list)
    for row in list_pipeline_files(directory, kind):
        grouped[row.environment].append(row)

    result = PruneResult()
    for rows in grouped.values():
        for row in rows[keep:]:
            try:
                row.path.unlink()
            except OSError as exc:
                result.failed.append((row.path, str(exc)))
            else:
                result.deleted.append(row.path)
    return result


def _delete(path: Path, result: PruneResult) -> None:
    try:
        path.unlink()
    except OSError as exc:
        result.failed.append((path, str(exc)))
    else:
        result.deleted.append(path)


def prune_rolling_window() -> PruneResult:
    """Drop files outside the Greater-universe plus two-snapshot window.

    Per environment the keep set is the newest ``cycle_kind: greater`` file,
    union the two newest candidate files. Watchlists stay when their candidates
    file is still retained, and the newest watchlist stays either way.
    """
    combined = PruneResult()
    raw = _prune_kind(RAW_DIR, "events_open", KEEP_RAW_PER_ENV)
    combined.deleted.extend(raw.deleted)
    combined.failed.extend(raw.failed)

    retained: set[tuple[str, str]] = set()
    grouped: dict[str, list[PipelineFile]] = defaultdict(list)
    for row in list_pipeline_files(CANDIDATES_DIR, "candidates"):
        grouped[row.environment].append(row)

    for rows in grouped.values():
        keep = set(rows[:KEEP_QUOTE_SNAPSHOTS_PER_ENV])
        for row in rows:
            if cycle_kind_of(row.path) == "greater":
                keep.add(row)
                break
        for row in rows:
            if row in keep:
                retained.add((row.environment, row.stamp))
            else:
                _delete(row.path, combined)

    watch_groups: dict[str, list[PipelineFile]] = defaultdict(list)
    for row in list_pipeline_files(WATCHLIST_DIR, "watchlist"):
        watch_groups[row.environment].append(row)
    for rows in watch_groups.values():
        newest = rows[0]
        for row in rows:
            matched = (row.environment, row.stamp) in retained
            if matched or row is newest:
                continue
            _delete(row.path, combined)
    return combined


def print_prune_result(result: PruneResult) -> None:
    print()
    if not result.deleted and not result.failed:
        print("Retention: rolling window already within keep limits.")
        return
    if result.deleted:
        print(f"Retention: removed {len(result.deleted)} older file(s):")
        for path in result.deleted:
            print(f"  {display_path(path)}")
    if result.failed:
        print(f"Retention: could not remove {len(result.failed)} file(s):")
        for path, message in result.failed:
            print(f"  {display_path(path)}: {message}")
