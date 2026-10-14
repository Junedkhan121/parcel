from decimal import Decimal

import pytest

from engine.fixedpoint import from_fixed, to_fixed
from engine.models import AssetConfig
from engine.nav import NavEngine

NOW = 1_760_000_000
DAY = 24 * 3600
YEAR = 365 * DAY


def make_engine(shares: str = "1000", fee_bps: int = 0) -> NavEngine:
    asset = AssetConfig(
        asset_id=42,
        name="Warehouse Unit 7, Rotterdam",
        symbol="WH7",
        management_fee_bps=fee_bps,
    )
    return NavEngine(asset, total_shares=to_fixed(shares), inception=NOW)


class TestValuation:
    def test_nav_per_share_from_valuation(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        assert nav.nav_per_share == to_fixed("2400")

    def test_no_shares_means_zero_nav(self):
        nav = make_engine(shares="0")
        nav.set_valuation(to_fixed("2400000"), NOW)
        assert nav.nav_per_share == 0

    def test_revaluation_moves_nav(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.set_valuation(to_fixed("2500000"), NOW + DAY)
        assert nav.nav_per_share == to_fixed("2500")


class TestIncome:
    def test_income_raises_nav(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("10000"), NOW + DAY)
        assert nav.nav_per_share == to_fixed("2410")

    def test_income_accumulates(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("10000"), NOW + DAY)
        nav.add_income(to_fixed("10000"), NOW + 2 * DAY)
        assert nav.accrued_income == to_fixed("20000")
        assert nav.nav_per_share == to_fixed("2420")

    def test_negative_income_rejected(self):
        nav = make_engine()
        with pytest.raises(ValueError):
            nav.add_income(to_fixed("-1"), NOW)


class TestFeeAccrual:
    def test_full_year_of_fees(self):
        # 2% of 2,400,000 = 48,000 over a full year.
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.accrue_fees(NOW + YEAR)
        assert nav.accrued_fees == to_fixed("48000")
        assert nav.net_value == to_fixed("2352000")

    def test_fees_prorate_over_days(self):
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.accrue_fees(NOW + DAY)
        # 2% of 2,400,000 is 48,000 a year; one day is 48,000 * 1/365.
        # Asserted exactly rather than approximately, since the whole
        # point of integer fixed-point is that this is deterministic.
        expected = (to_fixed("48000") * DAY) // YEAR
        assert nav.accrued_fees == expected
        assert from_fixed(nav.accrued_fees).quantize(Decimal("0.01")) == Decimal("131.51")

    def test_accrual_is_idempotent_at_same_timestamp(self):
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        first = nav.accrue_fees(NOW + DAY)
        second = nav.accrue_fees(NOW + DAY)
        assert first > 0
        assert second == 0

    def test_zero_fee_asset_accrues_nothing(self):
        nav = make_engine(shares="1000", fee_bps=0)
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.accrue_fees(NOW + YEAR)
        assert nav.accrued_fees == 0

    def test_clock_skew_does_not_credit_fees(self):
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        assert nav.accrue_fees(NOW - DAY) == 0

    def test_fees_charged_before_revaluation(self):
        # A year at 2,400,000 then a jump to 4,800,000 must not charge
        # the whole year at the new, higher value.
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.set_valuation(to_fixed("4800000"), NOW + YEAR)
        assert nav.accrued_fees == to_fixed("48000")

    def test_income_included_in_fee_basis(self):
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("1000000"), NOW)
        nav.add_income(to_fixed("1000000"), NOW)
        nav.accrue_fees(NOW + YEAR)
        # Fee basis is 2,000,000 at 2% = 40,000.
        assert nav.accrued_fees == to_fixed("40000")


class TestShareLifecycle:
    def test_issuing_shares_dilutes_nav(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.issue_shares(to_fixed("1000"))
        assert nav.nav_per_share == to_fixed("1200")

    def test_redeeming_shares_concentrates_nav(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.redeem_shares(to_fixed("500"))
        assert nav.nav_per_share == to_fixed("4800")

    def test_cannot_redeem_more_than_exist(self):
        nav = make_engine(shares="1000")
        with pytest.raises(ValueError):
            nav.redeem_shares(to_fixed("1001"))

    def test_cannot_issue_negative(self):
        nav = make_engine()
        with pytest.raises(ValueError):
            nav.issue_shares(to_fixed("-1"))


class TestHolderValue:
    def test_value_of_balance(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        assert nav.value_of(to_fixed("100")) == to_fixed("240000")

    def test_balance_unchanged_by_income(self):
        # The token does not rebase: income shows up as value per share,
        # never as a changed balance.
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        balance = to_fixed("100")
        before = nav.value_of(balance)
        nav.add_income(to_fixed("10000"), NOW + DAY)
        after = nav.value_of(balance)
        assert after > before
        assert balance == to_fixed("100")


class TestDistribution:
    def test_paying_out_lowers_nav(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("10000"), NOW)
        before = nav.nav_per_share

        paid = nav.pay_distribution(to_fixed("10000"), NOW)
        assert paid == to_fixed("10000")
        assert nav.nav_per_share < before
        assert nav.nav_per_share == to_fixed("2400")

    def test_share_count_unchanged(self):
        # A distribution is not a stock split.
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("10000"), NOW)
        nav.pay_distribution(to_fixed("5000"), NOW)
        assert nav.total_shares == to_fixed("1000")

    def test_cannot_distribute_more_than_earned(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("1000"), NOW)
        paid = nav.pay_distribution(to_fixed("999999"), NOW)
        assert paid == to_fixed("1000")
        assert nav.accrued_income == 0

    def test_distributable_is_net_of_fees(self):
        # Fees are owed to the manager before holders are paid.
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("50000"), NOW)
        nav.accrue_fees(NOW + YEAR)
        assert nav.distributable == nav.accrued_income - nav.accrued_fees

    def test_distributable_never_negative(self):
        nav = make_engine(shares="1000", fee_bps=500)
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("100"), NOW)
        nav.accrue_fees(NOW + YEAR)
        assert nav.distributable == 0

    def test_distributed_total_is_tracked(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("10000"), NOW)
        nav.pay_distribution(to_fixed("3000"), NOW)
        nav.pay_distribution(to_fixed("2000"), NOW)
        assert nav.distributed == to_fixed("5000")

    def test_negative_distribution_rejected(self):
        nav = make_engine()
        with pytest.raises(ValueError):
            nav.pay_distribution(to_fixed("-1"), NOW)

    def test_nav_falls_by_exactly_what_was_paid(self):
        nav = make_engine(shares="1000")
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("20000"), NOW)
        before = nav.net_value
        paid = nav.pay_distribution(to_fixed("12000"), NOW)
        assert nav.net_value == before - paid


class TestSnapshot:
    def test_snapshot_records_state(self):
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        nav.add_income(to_fixed("10000"), NOW)
        snap = nav.snapshot(NOW + DAY)

        assert snap.total_shares == to_fixed("1000")
        assert snap.accrued_income == to_fixed("10000")
        assert snap.accrued_fees > 0
        assert snap.epoch == NOW + DAY
        assert snap.nav_per_share == nav.nav_per_share

    def test_snapshot_without_timestamp_does_not_accrue(self):
        nav = make_engine(shares="1000", fee_bps=200)
        nav.set_valuation(to_fixed("2400000"), NOW)
        snap = nav.snapshot()
        assert snap.accrued_fees == 0
        assert snap.epoch == NOW
