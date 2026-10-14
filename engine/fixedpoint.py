"""Fixed-point arithmetic for monetary values.

Every amount in Parcel is an int scaled by 10**14 (tPOTv3's decimals).
Floats are never used in calculations -- only at display boundaries.

Rounding is truncation toward zero throughout, which is deliberate: it
keeps rounding deterministic and never credits value that was not earned.
"""

from __future__ import annotations

from decimal import Decimal

from .models import DECIMALS, SCALE

BPS_DENOMINATOR = 10_000
SECONDS_PER_YEAR = 365 * 24 * 60 * 60  # ACT/365 day-count


def to_fixed(value: str | int | Decimal) -> int:
    """Convert a human-readable amount to scaled integer form.

    Accepts strings to avoid float imprecision: to_fixed("2400000.50").
    """
    return int(Decimal(str(value)) * SCALE)


def from_fixed(value: int) -> Decimal:
    """Convert a scaled integer back to a Decimal for display."""
    return Decimal(value) / SCALE


def format_fixed(value: int, places: int = 2) -> str:
    """Render a scaled integer as a human-readable string."""
    quantum = Decimal(1).scaleb(-places)
    return str(from_fixed(value).quantize(quantum))


def mul(a: int, b: int) -> int:
    """Multiply two scaled values, returning a scaled value."""
    return (a * b) // SCALE


def div(a: int, b: int) -> int:
    """Divide two scaled values, returning a scaled value."""
    if b == 0:
        raise ZeroDivisionError("fixed-point division by zero")
    return (a * SCALE) // b


def apply_bps(value: int, bps: int) -> int:
    """Take `bps` basis points of a scaled value."""
    return (value * bps) // BPS_DENOMINATOR


def bps_of(part: int, whole: int) -> int:
    """Express `part` as basis points of `whole`."""
    if whole == 0:
        return 0
    return (part * BPS_DENOMINATOR) // whole


def accrue_pro_rata(annual_amount: int, elapsed_seconds: int) -> int:
    """Pro-rate an annual amount over elapsed time using ACT/365.

    Used for management fees, which are quoted annually but accrue
    continuously.
    """
    if elapsed_seconds <= 0:
        return 0
    return (annual_amount * elapsed_seconds) // SECONDS_PER_YEAR


def median(values: list[int]) -> int:
    """Median of scaled integers.

    For an even-length list the two middle values are averaged with
    truncation, keeping the result an exact integer.
    """
    if not values:
        raise ValueError("median of empty sequence")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) // 2


def median_absolute_deviation(values: list[int]) -> tuple[int, int]:
    """Return (median, MAD) for a list of scaled integers.

    MAD is the median of absolute deviations from the median. It is used
    instead of standard deviation because it is robust: a single wildly
    wrong oracle reading barely moves it, whereas it would inflate a
    standard deviation enough to let the bad value pass.
    """
    med = median(values)
    deviations = [abs(v - med) for v in values]
    return med, median(deviations)


__all__ = [
    "DECIMALS",
    "SCALE",
    "BPS_DENOMINATOR",
    "SECONDS_PER_YEAR",
    "to_fixed",
    "from_fixed",
    "format_fixed",
    "mul",
    "div",
    "apply_bps",
    "bps_of",
    "accrue_pro_rata",
    "median",
    "median_absolute_deviation",
]
