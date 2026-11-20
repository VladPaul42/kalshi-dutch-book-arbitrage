"""Settings loaded from .env.

Two things worth knowing:

  * Credentials are stored as PATHS, never values. The files live outside this
    project and are read at runtime. Nothing here logs or copies them.
  * The environment switch moves the base URL and the credential paths together.
    Kalshi does not share credentials between environments - demo keys work only
    on demo hosts and production keys only on production hosts - so binding them
    to one switch makes that mismatch impossible to create by accident.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# Current recommended dedicated Trade API hosts. The older shared hosts
# (api.elections.kalshi.com, demo-api.kalshi.co) also still work. These hostnames
# have changed more than once, so they are overridable from .env.
DEFAULT_BASE_URLS = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://external-api.kalshi.com/trade-api/v2",
}
DEFAULT_WS_URLS = {
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    "production": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
CANDIDATES_DIR = PROJECT_ROOT / "data" / "candidates"
WATCHLIST_DIR = PROJECT_ROOT / "data" / "watchlist"
TRADER_DIR = PROJECT_ROOT / "data" / "trader"


class ConfigError(RuntimeError):
    """A setting in .env is missing or unusable."""


def enable_utf8_stdout() -> None:
    """Stop Windows consoles from crashing on non-ASCII output.

    The default console encoding here is cp1252, so printing any path containing
    non-Latin-1 characters raises UnicodeEncodeError - which would otherwise kill a
    script at the final summary line, after all the real work had succeeded.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def display_path(path: Path) -> str:
    """Render a path relative to the project root when possible.

    Shorter to read, and it keeps awkward characters in the absolute path out of
    console output entirely.
    """
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} in .env must be a whole number, got {raw!r}") from exc


def _get_decimal(name: str, default: str) -> Decimal:
    raw = _get(name, default)
    try:
        return Decimal(raw)
    except Exception as exc:
        raise ConfigError(f"{name} in .env must be a number, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} in .env must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} in .env must be true or false, got {raw!r}")


@dataclass(frozen=True)
class ApiConfig:
    """Which environment to talk to, and how."""

    environment: str  # "demo" or "production"
    base_url: str
    api_key_id_path: Path | None
    private_key_path: Path | None
    page_limit: int
    page_sleep_seconds: float
    max_pages: int
    timeout_seconds: float
    max_retries: int
    allow_unsigned_public_reads: bool
    compress_snapshots: bool

    @property
    def has_credentials(self) -> bool:
        return self.api_key_id_path is not None and self.private_key_path is not None

    @property
    def is_demo(self) -> bool:
        return self.environment == "demo"


@dataclass(frozen=True)
class FilterConfig:
    """The gates the filter applies, in funnel order."""

    require_mutually_exclusive: bool
    min_markets: int
    max_markets: int
    require_all_active: bool
    require_executable_route: bool
    min_volume_24h: Decimal
    min_open_interest: Decimal
    liquidity_mode: str  # "any" or "all"
    min_minutes_to_close: int
    max_days_to_close: int

    def as_dict(self) -> dict[str, object]:
        """A plain-JSON record of the thresholds actually used, for the output file."""
        return {
            "require_mutually_exclusive": self.require_mutually_exclusive,
            "min_markets": self.min_markets,
            "max_markets": self.max_markets,
            "require_all_active": self.require_all_active,
            "require_executable_route": self.require_executable_route,
            "min_volume_24h_contracts": str(self.min_volume_24h),
            "min_open_interest_contracts": str(self.min_open_interest),
            "liquidity_mode": self.liquidity_mode,
            "min_minutes_to_close": self.min_minutes_to_close,
            "max_days_to_close": self.max_days_to_close,
        }


@dataclass(frozen=True)
class ScoreConfig:
    """Watchlist size and how long the dashboard idles after a cycle finishes."""

    watchlist_size: int
    cycle_idle_minutes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "watchlist_size": self.watchlist_size,
            "cycle_idle_minutes": self.cycle_idle_minutes,
            "score": "max volume_24h_contracts across the event's legs",
        }


@dataclass(frozen=True)
class TraderConfig:
    """Live watchlist trader gates and environment-specific WebSocket URL."""

    dry_mode: bool
    min_annualized_pct: Decimal
    max_capital_per_event_dollars: Decimal
    max_capital_per_shot_dollars: Decimal
    ws_url: str

    @property
    def max_capital_per_event_cents(self) -> Decimal:
        return self.max_capital_per_event_dollars * Decimal(100)

    @property
    def max_capital_per_shot_cents(self) -> Decimal:
        return self.max_capital_per_shot_dollars * Decimal(100)


def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def parse_environment(value: str, *, source: str = "KALSHI_ENV") -> str:
    """Normalise a demo/production label, or raise :class:`ConfigError`."""
    env = value.strip().lower()
    if env in ("prod", "production"):
        return "production"
    if env == "demo":
        return "demo"
    raise ConfigError(
        f"{source} is {value!r}. It must be 'demo' or 'production'."
    )


def resolve_environment() -> str:
    return parse_environment(_get("KALSHI_ENV", "demo"), source="KALSHI_ENV in .env")


def _credential_path(name: str) -> Path | None:
    """Resolve a credential file path, or None if it is not configured.

    Only the path is touched here. The file itself is opened later, by the code
    that actually needs to sign a request.
    """
    raw = _get(name, "")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ConfigError(
            f"{name} in .env points at {path}, which is not a file that exists.\n"
            f"Fix the path, or clear the setting to read public data unsigned."
        )
    return path


def load_api_config(environment: str | None = None) -> ApiConfig:
    """Load URL and credential paths for one Kalshi environment.

    ``environment=None`` follows ``KALSHI_ENV`` (the collector). Pass ``demo`` or
    ``production`` to bind a caller to a specific host and key pair — the
    dashboard uses the run file's ``environment`` field so a demo snapshot can
    never spend production credentials.
    """
    load_env()
    if environment is None:
        environment = resolve_environment()
    else:
        environment = parse_environment(environment, source="environment")
    prefix = "KALSHI_DEMO" if environment == "demo" else "KALSHI_PROD"

    base_url = _get(f"{prefix}_BASE_URL", DEFAULT_BASE_URLS[environment]).rstrip("/")
    if "/trade-api/v2" not in base_url:
        raise ConfigError(
            f"{prefix}_BASE_URL is {base_url!r}, which does not contain '/trade-api/v2'.\n"
            f"Signatures are computed over the full path from the API root, so the\n"
            f"prefix must be part of the base URL or every request will be rejected."
        )

    key_id_path = _credential_path(f"{prefix}_API_KEY_ID_PATH")
    private_key_path = _credential_path(f"{prefix}_PRIVATE_KEY_PATH")

    # Half-configured credentials are almost always a typo, and failing here is
    # kinder than a confusing 401 later.
    if bool(key_id_path) != bool(private_key_path):
        raise ConfigError(
            f"Only one of {prefix}_API_KEY_ID_PATH and {prefix}_PRIVATE_KEY_PATH is set.\n"
            f"Set both to make signed requests, or neither to read public data unsigned."
        )

    page_limit = _get_int("COLLECTOR_PAGE_LIMIT", 200)
    if not 1 <= page_limit <= 200:
        raise ConfigError(
            f"COLLECTOR_PAGE_LIMIT is {page_limit}. GET /events accepts 1-200; the cap "
            f"is 200 even with with_nested_markets=true."
        )

    return ApiConfig(
        environment=environment,
        base_url=base_url,
        api_key_id_path=key_id_path,
        private_key_path=private_key_path,
        page_limit=page_limit,
        page_sleep_seconds=_get_float("COLLECTOR_PAGE_SLEEP_SECONDS", 0.3),
        max_pages=_get_int("COLLECTOR_MAX_PAGES", 200),
        timeout_seconds=_get_float("HTTP_TIMEOUT_SECONDS", 30.0),
        max_retries=_get_int("HTTP_MAX_RETRIES", 4),
        allow_unsigned_public_reads=_get_bool("ALLOW_UNSIGNED_PUBLIC_READS", True),
        compress_snapshots=_get_bool("COLLECTOR_COMPRESS", True),
    )


def load_filter_config() -> FilterConfig:
    load_env()

    mode = _get("FILTER_LIQUIDITY_MODE", "any").lower()
    if mode not in ("any", "all"):
        raise ConfigError(
            f"FILTER_LIQUIDITY_MODE is {mode!r}. Use 'any' (volume OR open interest "
            f"clears its floor) or 'all' (both must clear)."
        )

    min_markets = _get_int("FILTER_MIN_MARKETS", 2)
    max_markets = _get_int("FILTER_MAX_MARKETS", 20)
    if min_markets < 1:
        raise ConfigError("FILTER_MIN_MARKETS must be at least 1.")
    if max_markets < min_markets:
        raise ConfigError(
            f"FILTER_MAX_MARKETS ({max_markets}) is below FILTER_MIN_MARKETS "
            f"({min_markets}), so no event can ever pass both gates."
        )

    min_minutes = _get_int("FILTER_MIN_MINUTES_TO_CLOSE", 15)
    max_days = _get_int("FILTER_MAX_DAYS_TO_CLOSE", 365)
    if min_minutes > max_days * 24 * 60:
        raise ConfigError(
            f"FILTER_MIN_MINUTES_TO_CLOSE ({min_minutes}) exceeds "
            f"FILTER_MAX_DAYS_TO_CLOSE ({max_days} days), so the close-time window "
            f"is empty."
        )

    return FilterConfig(
        require_mutually_exclusive=_get_bool("FILTER_REQUIRE_MUTUALLY_EXCLUSIVE", True),
        min_markets=min_markets,
        max_markets=max_markets,
        require_all_active=_get_bool("FILTER_REQUIRE_ALL_ACTIVE", True),
        require_executable_route=_get_bool("FILTER_REQUIRE_EXECUTABLE_ROUTE", True),
        min_volume_24h=_get_decimal("FILTER_MIN_VOLUME_24H", "0"),
        min_open_interest=_get_decimal("FILTER_MIN_OPEN_INTEREST", "0"),
        liquidity_mode=mode,
        min_minutes_to_close=min_minutes,
        max_days_to_close=max_days,
    )


def load_score_config() -> ScoreConfig:
    """Watchlist length and the idle that follows each finished cycle.

    ``CYCLE_INTERVAL_MINUTES`` is not read. The old 10-minute start-to-start
    interval must not override the post-work idle.
    """
    load_env()
    watchlist_size = _get_int("WATCHLIST_SIZE", 10)
    if watchlist_size < 1:
        raise ConfigError("WATCHLIST_SIZE must be at least 1.")
    idle_minutes = _get_int("CYCLE_IDLE_MINUTES", 5)
    if idle_minutes < 1:
        raise ConfigError("CYCLE_IDLE_MINUTES must be at least 1.")
    return ScoreConfig(
        watchlist_size=watchlist_size,
        cycle_idle_minutes=idle_minutes,
    )


def load_trader_config(environment: str | None = None) -> TraderConfig:
    """Load the trader's gates. Dry mode defaults on deliberately."""
    load_env()
    selected = resolve_environment() if environment is None else parse_environment(
        environment, source="environment"
    )
    prefix = "KALSHI_DEMO" if selected == "demo" else "KALSHI_PROD"
    minimum = _get_decimal("TRADER_MIN_ANNUALIZED_PCT", "5")
    capital = _get_decimal("TRADER_MAX_CAPITAL_PER_EVENT_DOLLARS", "10")
    shot = _get_decimal("TRADER_MAX_CAPITAL_PER_SHOT_DOLLARS", "2")
    if minimum < 0:
        raise ConfigError("TRADER_MIN_ANNUALIZED_PCT must be at least 0.")
    if capital <= 0:
        raise ConfigError("TRADER_MAX_CAPITAL_PER_EVENT_DOLLARS must be above 0.")
    if shot <= 0:
        raise ConfigError("TRADER_MAX_CAPITAL_PER_SHOT_DOLLARS must be above 0.")
    if shot > capital:
        raise ConfigError(
            "TRADER_MAX_CAPITAL_PER_SHOT_DOLLARS must not exceed "
            "TRADER_MAX_CAPITAL_PER_EVENT_DOLLARS."
        )
    ws_url = _get(f"{prefix}_WS_URL", DEFAULT_WS_URLS[selected])
    if not ws_url.startswith(("ws://", "wss://")) or "/trade-api/ws/v2" not in ws_url:
        raise ConfigError(
            f"{prefix}_WS_URL is {ws_url!r}; expected a ws(s) URL containing "
            "'/trade-api/ws/v2'."
        )
    return TraderConfig(
        dry_mode=_get_bool("TRADER_DRY_MODE", True),
        min_annualized_pct=minimum,
        max_capital_per_event_dollars=capital,
        max_capital_per_shot_dollars=shot,
        ws_url=ws_url,
    )
