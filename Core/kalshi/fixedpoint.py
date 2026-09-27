"""Parsing for Kalshi's fixed-point string fields.

Kalshi no longer sends integer cents. The legacy integer fields were removed from
all REST and WebSocket payloads on 12 March 2026, so in practice you will only
ever see the string forms:

  * prices arrive in fields suffixed `_dollars`, as strings like "0.4200"
  * contract counts arrive in fields suffixed `_fp`, as strings like "13.00"

They are strings because tick sizes can be smaller than a cent and quantities can
be fractional down to 0.01 contracts. Responses emit up to 6 decimal places.

Everything goes through Decimal, never float. Getting this wrong does not crash;
it silently produces slightly wrong prices, which is much worse than a crash.
The legacy-integer fallbacks below should never fire against the current API and
exist only so that an old archived snapshot still parses.

Most sample code online still reads the old integer-cent fields and is out of date.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

CENTS_PER_DOLLAR = Decimal("100")

# Prices are reported in cents everywhere downstream of this module, quantized to
# four decimal places. That is finer than the finest tick Kalshi currently offers
# (centi-cent, $0.0001 = 0.01c) so it cannot lose real precision, and it keeps the
# output file tidy instead of carrying six trailing digits of noise.
CENT_QUANTUM = Decimal("0.0001")
COUNT_QUANTUM = Decimal("0.01")
# Order prices are sent as fixed-point dollars with four decimal places, e.g. "0.6300".
DOLLAR_QUANTUM = Decimal("0.0001")


def to_decimal(value: Any) -> Decimal | None:
    """Parse a fixed-point string (or number) into a Decimal, or None if unusable."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def price_cents(payload: dict[str, Any], dollars_key: str) -> Decimal | None:
    """Read a price field as cents.

    Prefers the `_dollars` string. Falls back to the legacy integer-cent field of
    the same name minus the suffix (e.g. `yes_bid` for `yes_bid_dollars`), which
    only matters for snapshots archived before March 2026; legacy fields cannot
    represent sub-penny prices.
    """
    from_dollars = to_decimal(payload.get(dollars_key))
    if from_dollars is not None:
        return (from_dollars * CENTS_PER_DOLLAR).quantize(CENT_QUANTUM)

    legacy_key = dollars_key.removesuffix("_dollars")
    legacy = to_decimal(payload.get(legacy_key))
    if legacy is not None:
        return legacy.quantize(CENT_QUANTUM)
    return None


def contract_count(payload: dict[str, Any], fp_key: str) -> Decimal | None:
    """Read a contract-count field as a number of contracts.

    Prefers the `_fp` string, falling back to the legacy integer field of the same
    name without the suffix (e.g. `volume_24h` for `volume_24h_fp`).
    """
    from_fp = to_decimal(payload.get(fp_key))
    if from_fp is not None:
        return from_fp.quantize(COUNT_QUANTUM)

    legacy = to_decimal(payload.get(fp_key.removesuffix("_fp")))
    if legacy is not None:
        return legacy.quantize(COUNT_QUANTUM)
    return None


def dollars_from_cents(cents: Decimal) -> Decimal:
    """Convert a cent price to fixed-point dollars, quantized to four places.

    63 cents becomes ``Decimal("0.6300")``, which ``format(..., "f")`` renders as
    the string the Trade API expects. Never goes through float.
    """
    return (cents / CENTS_PER_DOLLAR).quantize(DOLLAR_QUANTUM)


def to_json(value: Decimal | None) -> str | None:
    """Render a Decimal for JSON output.

    Emitted as a STRING, not a number, deliberately. A JSON float would reintroduce
    exactly the silent binary-floating-point drift this module exists to avoid, and
    anything reading these files back can hand the string straight to Decimal().
    """
    if value is None:
        return None
    return format(value.normalize(), "f")
