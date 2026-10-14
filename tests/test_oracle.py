from engine.fixedpoint import to_fixed
from engine.models import OracleReading
from engine.oracle import (
    BreakerReason,
    OracleReconciler,
    RejectReason,
)

NOW = 1_760_000_000
HOUR = 3600
DAY = 24 * HOUR


def reading(source: str, value: str, age_seconds: int = 0) -> OracleReading:
    return OracleReading(
        source=source,
        value=to_fixed(value),
        timestamp=NOW - age_seconds,
    )


class TestHappyPath:
    def test_median_of_three(self):
        r = OracleReconciler().reconcile(
            [
                reading("appraiser", "2400000"),
                reading("index", "2410000"),
                reading("broker", "2395000"),
            ],
            NOW,
        )
        assert r.ok
        assert r.value == to_fixed("2400000")
        assert len(r.accepted) == 3
        assert r.rejected == ()

    def test_identical_readings_accepted(self):
        # MAD is zero here; identical values must not be read as outliers.
        r = OracleReconciler().reconcile(
            [
                reading("a", "100"),
                reading("b", "100"),
                reading("c", "100"),
            ],
            NOW,
        )
        assert r.ok
        assert r.value == to_fixed("100")


class TestStaleness:
    def test_stale_reading_rejected(self):
        r = OracleReconciler(max_age_seconds=DAY).reconcile(
            [
                reading("a", "100"),
                reading("b", "101"),
                reading("c", "102"),
                reading("old", "500", age_seconds=2 * DAY),
            ],
            NOW,
        )
        assert r.ok
        assert any(
            rej.reason is RejectReason.STALE and rej.reading.source == "old"
            for rej in r.rejected
        )
        assert all(acc.source != "old" for acc in r.accepted)

    def test_all_stale_trips_breaker(self):
        r = OracleReconciler(max_age_seconds=HOUR).reconcile(
            [
                reading("a", "100", age_seconds=DAY),
                reading("b", "101", age_seconds=DAY),
                reading("c", "102", age_seconds=DAY),
            ],
            NOW,
        )
        assert not r.ok
        assert r.breaker is BreakerReason.INSUFFICIENT_SOURCES


class TestOutlierRejection:
    def test_single_wild_reading_rejected(self):
        r = OracleReconciler().reconcile(
            [
                reading("a", "2400000"),
                reading("b", "2410000"),
                reading("c", "2395000"),
                reading("rogue", "99000000"),
            ],
            NOW,
        )
        assert r.ok
        assert any(
            rej.reason is RejectReason.OUTLIER and rej.reading.source == "rogue"
            for rej in r.rejected
        )
        # The median must be unmoved by the rogue value.
        assert r.value == to_fixed("2400000")

    def test_rejecting_outliers_can_trip_breaker(self):
        # Three readings, one rogue -> only two survive, below min_sources.
        r = OracleReconciler(min_sources=3).reconcile(
            [
                reading("a", "100"),
                reading("b", "100"),
                reading("rogue", "900000"),
            ],
            NOW,
        )
        assert not r.ok
        assert r.breaker is BreakerReason.INSUFFICIENT_SOURCES


class TestSourceDeduplication:
    def test_latest_submission_per_source_wins(self):
        r = OracleReconciler().reconcile(
            [
                OracleReading("a", to_fixed("100"), NOW - HOUR),
                OracleReading("a", to_fixed("200"), NOW),  # newer
                reading("b", "200"),
                reading("c", "200"),
            ],
            NOW,
        )
        assert r.ok
        assert r.value == to_fixed("200")
        assert len(r.accepted) == 3

    def test_source_cannot_flood_the_median(self):
        # Without dedup, five submissions from "spam" would dominate.
        spam = [OracleReading("spam", to_fixed("1"), NOW - i) for i in range(5)]
        r = OracleReconciler().reconcile(
            spam + [reading("a", "100"), reading("b", "101"), reading("c", "102")],
            NOW,
        )
        assert r.ok
        # "spam" collapses to one vote and is then an outlier.
        assert r.value == to_fixed("101")


class TestCircuitBreaker:
    def test_large_move_blocked(self):
        reconciler = OracleReconciler(max_deviation_bps=2_000)  # 20%
        r = reconciler.reconcile(
            [
                reading("a", "200"),
                reading("b", "200"),
                reading("c", "200"),
            ],
            NOW,
            previous_value=to_fixed("100"),
        )
        assert not r.ok
        assert r.breaker is BreakerReason.DEVIATION_EXCEEDED
        # Readings were valid; it is the move that is suspicious.
        assert len(r.accepted) == 3

    def test_small_move_allowed(self):
        reconciler = OracleReconciler(max_deviation_bps=2_000)
        r = reconciler.reconcile(
            [
                reading("a", "105"),
                reading("b", "105"),
                reading("c", "105"),
            ],
            NOW,
            previous_value=to_fixed("100"),
        )
        assert r.ok
        assert r.value == to_fixed("105")

    def test_downward_move_also_checked(self):
        reconciler = OracleReconciler(max_deviation_bps=2_000)
        r = reconciler.reconcile(
            [
                reading("a", "50"),
                reading("b", "50"),
                reading("c", "50"),
            ],
            NOW,
            previous_value=to_fixed("100"),
        )
        assert not r.ok
        assert r.breaker is BreakerReason.DEVIATION_EXCEEDED

    def test_first_valuation_has_no_previous(self):
        r = OracleReconciler().reconcile(
            [
                reading("a", "2400000"),
                reading("b", "2400000"),
                reading("c", "2400000"),
            ],
            NOW,
            previous_value=None,
        )
        assert r.ok


class TestDegenerateInput:
    def test_no_readings(self):
        r = OracleReconciler().reconcile([], NOW)
        assert not r.ok
        assert r.breaker is BreakerReason.NO_READINGS

    def test_too_few_sources(self):
        r = OracleReconciler(min_sources=3).reconcile(
            [reading("a", "100"), reading("b", "101")],
            NOW,
        )
        assert not r.ok
        assert r.breaker is BreakerReason.INSUFFICIENT_SOURCES

    def test_single_source_mode(self):
        # Permitted, but outlier detection is meaningless with one reading.
        r = OracleReconciler(min_sources=1).reconcile([reading("a", "100")], NOW)
        assert r.ok
        assert r.value == to_fixed("100")
