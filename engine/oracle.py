"""Oracle reconciliation.

Real-world asset valuations arrive from multiple independent sources --
appraisers, market indices, broker quotes -- and they disagree. Some
readings are merely different; others are wrong, stale, or malicious.

This module turns a set of raw submissions into one defensible number,
or refuses to produce a number at all. Refusing is a feature: a stuck
NAV is recoverable, a NAV poisoned by a bad feed is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .fixedpoint import bps_of, median, median_absolute_deviation
from .models import OracleReading


class RejectReason(str, Enum):
    STALE = "stale"
    OUTLIER = "outlier"


class BreakerReason(str, Enum):
    NO_READINGS = "no_readings"
    INSUFFICIENT_SOURCES = "insufficient_sources"
    DEVIATION_EXCEEDED = "deviation_exceeded"


@dataclass(frozen=True)
class RejectedReading:
    reading: OracleReading
    reason: RejectReason


@dataclass(frozen=True)
class Reconciliation:
    """Outcome of reconciling one round of oracle submissions."""

    value: int | None
    accepted: tuple[OracleReading, ...]
    rejected: tuple[RejectedReading, ...]
    breaker: BreakerReason | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


class OracleReconciler:
    """Aggregates multi-source valuations into a single trusted value.

    Parameters
    ----------
    min_sources:
        Fewer accepted readings than this and we decline to publish.
        Two sources cannot outvote each other, so three is the sensible
        floor for outlier detection to mean anything.
    max_age_seconds:
        Readings older than this are discarded as stale.
    outlier_mad_multiple:
        A reading is an outlier if it deviates from the median by more
        than this multiple of the median absolute deviation.
    max_deviation_bps:
        Circuit breaker. If the new value moves more than this from the
        previous accepted value, refuse to publish and let a human look.
    """

    def __init__(
        self,
        min_sources: int = 3,
        max_age_seconds: int = 24 * 60 * 60,
        outlier_mad_multiple: int = 3,
        max_deviation_bps: int = 2_000,  # 20%
    ):
        if min_sources < 1:
            raise ValueError("min_sources must be at least 1")
        self.min_sources = min_sources
        self.max_age_seconds = max_age_seconds
        self.outlier_mad_multiple = outlier_mad_multiple
        self.max_deviation_bps = max_deviation_bps

    def reconcile(
        self,
        readings: list[OracleReading],
        now: int,
        previous_value: int | None = None,
    ) -> Reconciliation:
        if not readings:
            return Reconciliation(
                value=None,
                accepted=(),
                rejected=(),
                breaker=BreakerReason.NO_READINGS,
            )

        rejected: list[RejectedReading] = []

        # One submission per source: the most recent wins. Prevents a
        # single source from flooding the round to swing the median.
        latest_by_source: dict[str, OracleReading] = {}
        for reading in readings:
            existing = latest_by_source.get(reading.source)
            if existing is None or reading.timestamp > existing.timestamp:
                latest_by_source[reading.source] = reading

        fresh: list[OracleReading] = []
        for reading in latest_by_source.values():
            if now - reading.timestamp > self.max_age_seconds:
                rejected.append(RejectedReading(reading, RejectReason.STALE))
            else:
                fresh.append(reading)

        if len(fresh) < self.min_sources:
            return Reconciliation(
                value=None,
                accepted=(),
                rejected=tuple(rejected),
                breaker=BreakerReason.INSUFFICIENT_SOURCES,
            )

        accepted = self._reject_outliers(fresh, rejected)

        if len(accepted) < self.min_sources:
            return Reconciliation(
                value=None,
                accepted=(),
                rejected=tuple(rejected),
                breaker=BreakerReason.INSUFFICIENT_SOURCES,
            )

        value = median([r.value for r in accepted])

        if previous_value is not None and previous_value > 0:
            move = bps_of(abs(value - previous_value), previous_value)
            if move > self.max_deviation_bps:
                return Reconciliation(
                    value=None,
                    accepted=tuple(accepted),
                    rejected=tuple(rejected),
                    breaker=BreakerReason.DEVIATION_EXCEEDED,
                )

        return Reconciliation(
            value=value,
            accepted=tuple(accepted),
            rejected=tuple(rejected),
        )

    def _reject_outliers(
        self,
        readings: list[OracleReading],
        rejected: list[RejectedReading],
    ) -> list[OracleReading]:
        """Discard readings too far from the median, measured in MADs.

        With fewer than three readings there is no meaningful centre to
        deviate from, so everything passes and min_sources does the work.
        """
        if len(readings) < 3:
            return list(readings)

        values = [r.value for r in readings]
        med, mad = median_absolute_deviation(values)

        # MAD of zero means the readings are tightly clustered (often
        # identical). Anything not equal to the median is then an outlier.
        threshold = mad * self.outlier_mad_multiple

        accepted: list[OracleReading] = []
        for reading in readings:
            if abs(reading.value - med) > threshold:
                rejected.append(RejectedReading(reading, RejectReason.OUTLIER))
            else:
                accepted.append(reading)
        return accepted
