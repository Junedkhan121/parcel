from decimal import Decimal

import pytest

from engine.fixedpoint import (
    SCALE,
    accrue_pro_rata,
    apply_bps,
    bps_of,
    div,
    format_fixed,
    from_fixed,
    median,
    median_absolute_deviation,
    mul,
    to_fixed,
)


class TestConversion:
    def test_to_fixed_from_string(self):
        assert to_fixed("1") == SCALE
        assert to_fixed("0.5") == SCALE // 2

    def test_to_fixed_avoids_float_error(self):
        # 0.1 is not representable in binary floating point. Going
        # through Decimal keeps it exact.
        assert to_fixed("0.1") == SCALE // 10

    def test_round_trip(self):
        assert from_fixed(to_fixed("2400000.50")) == Decimal("2400000.50")

    def test_format(self):
        assert format_fixed(to_fixed("1234.5678")) == "1234.57"
        assert format_fixed(to_fixed("1234.5678"), places=4) == "1234.5678"


class TestArithmetic:
    def test_mul_identity(self):
        assert mul(to_fixed("7"), SCALE) == to_fixed("7")

    def test_mul(self):
        assert mul(to_fixed("1.5"), to_fixed("4")) == to_fixed("6")

    def test_div_identity(self):
        assert div(to_fixed("7"), SCALE) == to_fixed("7")

    def test_div(self):
        assert div(to_fixed("6"), to_fixed("4")) == to_fixed("1.5")

    def test_div_by_zero_raises(self):
        with pytest.raises(ZeroDivisionError):
            div(SCALE, 0)

    def test_mul_truncates_toward_zero(self):
        # Truncation must never invent value.
        assert mul(1, 1) == 0


class TestBasisPoints:
    def test_apply_full(self):
        assert apply_bps(to_fixed("100"), 10_000) == to_fixed("100")

    def test_apply_two_percent(self):
        assert apply_bps(to_fixed("100"), 200) == to_fixed("2")

    def test_bps_of(self):
        assert bps_of(to_fixed("25"), to_fixed("100")) == 2_500

    def test_bps_of_zero_whole(self):
        assert bps_of(to_fixed("5"), 0) == 0


class TestAccrual:
    def test_full_year(self):
        annual = to_fixed("365")
        assert accrue_pro_rata(annual, 365 * 24 * 3600) == annual

    def test_one_day(self):
        annual = to_fixed("365")
        assert accrue_pro_rata(annual, 24 * 3600) == to_fixed("1")

    def test_zero_elapsed(self):
        assert accrue_pro_rata(to_fixed("365"), 0) == 0

    def test_negative_elapsed(self):
        # Clock skew must not credit negative fees.
        assert accrue_pro_rata(to_fixed("365"), -100) == 0


class TestMedian:
    def test_odd_length(self):
        assert median([3, 1, 2]) == 2

    def test_even_length_averages_middle(self):
        assert median([1, 2, 3, 4]) == 2  # (2+3)//2 truncates

    def test_single_value(self):
        assert median([42]) == 42

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            median([])

    def test_mad_identifies_spread(self):
        med, mad = median_absolute_deviation([10, 11, 12, 13, 14])
        assert med == 12
        assert mad == 1

    def test_mad_robust_to_single_outlier(self):
        # One absurd reading barely moves the MAD: deviations are
        # [2, 1, 0, 1, 9988] and their median is still 1. A standard
        # deviation here would be ~4,400, wide enough to let the bad
        # value through. That is why MAD is used instead.
        med, mad = median_absolute_deviation([10, 11, 12, 13, 10_000])
        assert med == 12
        assert mad == 1

    def test_mad_zero_when_identical(self):
        med, mad = median_absolute_deviation([5, 5, 5])
        assert med == 5
        assert mad == 0
