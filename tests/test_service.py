"""Integration tests: the whole service, end to end.

These exercise the paths a judge will actually see at Demo Day, so a
failure here means the demo is broken.
"""

import pytest

import scenario
from chain.mock import MockChain
from engine.fixedpoint import to_fixed
from engine.models import OracleReading, ReasonCode
from service import ParcelService

DAY = 24 * 3600


@pytest.fixture
def service():
    chain = MockChain(start_time=scenario.INCEPTION)
    scenario.seed_chain(chain)
    return ParcelService(
        chain=chain,
        asset=scenario.asset_config(),
        holders=scenario.holder_registry(),
        total_shares=scenario.TOTAL_SHARES,
        inception=scenario.INCEPTION,
    )


class TestScenarioSetup:
    def test_all_investors_hold_shares(self, service):
        views = {v.label: v for v in service.holder_views(scenario.INCEPTION)}
        assert views["Alice"].balance == to_fixed("400")
        assert views["Eve"].balance == to_fixed("50")

    def test_supply_matches_configured_shares(self, service):
        assert service.engine.total_supply() == scenario.TOTAL_SHARES


class TestComplianceEnforcement:
    def test_dave_and_eve_are_frozen(self, service):
        result = service.enforce(scenario.INCEPTION)
        frozen = {scenario.label_for(a) for a in result.frozen}
        assert frozen == {"Dave", "Eve"}

    def test_dave_fails_on_jurisdiction(self, service):
        plan = service.enforcer.plan(scenario.INCEPTION)
        dave = scenario.INVESTORS["Dave"]
        assert plan.to_freeze[dave] is ReasonCode.JURISDICTION_NOT_PERMITTED

    def test_eve_fails_on_lockup(self, service):
        plan = service.enforcer.plan(scenario.INCEPTION)
        eve = scenario.INVESTORS["Eve"]
        assert plan.to_freeze[eve] is ReasonCode.LOCKUP_ACTIVE

    def test_enforcement_converges(self, service):
        service.enforce(scenario.INCEPTION)
        assert service.enforce(scenario.INCEPTION).plan.is_empty

    def test_no_drift_after_enforcement(self, service):
        service.enforce(scenario.INCEPTION)
        assert service.summary(scenario.INCEPTION)["drift"] == {}


class TestTheDemoMoment:
    """The transfer that gets blocked -- this is what judges watch."""

    def test_frozen_holder_cannot_transfer(self, service):
        service.enforce(scenario.INCEPTION)
        dave = scenario.INVESTORS["Dave"]
        alice = scenario.INVESTORS["Alice"]

        verdict, _ = service.attempt_transfer(
            dave, alice, to_fixed("10"), scenario.INCEPTION
        )
        assert not verdict.allowed
        assert verdict.reason is ReasonCode.JURISDICTION_NOT_PERMITTED

    def test_chain_itself_blocks_it(self, service):
        """Not just our check -- pallet-assets refuses the extrinsic."""
        service.enforce(scenario.INCEPTION)
        dave = scenario.INVESTORS["Dave"]
        alice = scenario.INVESTORS["Alice"]

        receipt = service.chain.transfer(
            scenario.ASSET_ID, dave, alice, to_fixed("10")
        )
        assert not receipt.ok
        assert "frozen" in receipt.error

    def test_compliant_transfer_settles(self, service):
        service.enforce(scenario.INCEPTION)
        alice = scenario.INVESTORS["Alice"]
        bob = scenario.INVESTORS["Bob"]

        verdict, error = service.attempt_transfer(
            alice, bob, to_fixed("10"), scenario.INCEPTION
        )
        assert verdict.allowed
        assert error is None
        assert service.chain.account_state(
            scenario.ASSET_ID, bob
        ).balance == to_fixed("260")

    def test_eve_unfreezes_when_lockup_expires(self, service):
        service.enforce(scenario.INCEPTION)
        eve = scenario.INVESTORS["Eve"]
        assert service.chain.account_state(scenario.ASSET_ID, eve).frozen

        after_lockup = scenario.INCEPTION + 366 * DAY
        service.enforce(after_lockup)
        assert not service.chain.account_state(scenario.ASSET_ID, eve).frozen


class TestValuationHistory:
    def test_first_valuation_accepted(self, service):
        ts, readings = scenario.valuation_rounds()[0]
        event = service.submit_valuation(readings, ts)
        assert event.applied
        assert event.reconciliation.value == to_fixed("2400000")

    def test_corrupt_broker_feed_rejected(self, service):
        """The month-6 round contains a misplaced decimal point."""
        for ts, readings in scenario.valuation_rounds():
            service.submit_valuation(readings, ts)

        rejected = service.rejected_readings()
        assert len(rejected) == 1
        _, source, reason = rejected[0]
        assert source == "broker_rotterdam"
        assert reason == "outlier"

    def test_nav_unaffected_by_corrupt_feed(self, service):
        for ts, readings in scenario.valuation_rounds():
            service.submit_valuation(readings, ts)
        # Median of the three sane readings, not the 24.98M outlier.
        assert service.nav.asset_value == to_fixed("2505000")

    def test_nav_rises_with_rent(self, service):
        ts, readings = scenario.valuation_rounds()[0]
        service.submit_valuation(readings, ts)
        before = service.nav.nav_per_share

        for payment_ts, amount, note in scenario.rent_payments():
            service.record_income(amount, payment_ts, note)

        assert service.nav.nav_per_share > before


class TestDistribution:
    def _run_six_months(self, service):
        rounds = dict(scenario.valuation_rounds())
        payments = {ts: (a, n) for ts, a, n in scenario.rent_payments()}
        for ts in sorted(set(rounds) | set(payments)):
            if ts in rounds:
                service.submit_valuation(rounds[ts], ts)
            if ts in payments:
                amount, note = payments[ts]
                service.record_income(amount, ts, note)
        return max(set(rounds) | set(payments))

    def test_distribution_lowers_nav(self, service):
        final = self._run_six_months(service)
        service.enforce(final)
        before = service.nav.nav_per_share

        service.distribute_income(service.nav.distributable // 2, final)
        assert service.nav.nav_per_share < before

    def test_share_count_unchanged(self, service):
        final = self._run_six_months(service)
        service.enforce(final)
        service.distribute_income(service.nav.distributable, final)
        assert service.nav.total_shares == scenario.TOTAL_SHARES

    def test_ineligible_holders_excluded(self, service):
        final = self._run_six_months(service)
        service.enforce(final)
        paid = service.distribute_income(service.nav.distributable // 2, final)

        assert scenario.INVESTORS["Dave"] not in paid
        assert scenario.INVESTORS["Eve"] not in paid
        assert scenario.INVESTORS["Alice"] in paid

    def test_withheld_amount_stays_in_vehicle(self, service):
        final = self._run_six_months(service)
        service.enforce(final)
        service.distribute_income(service.nav.distributable // 2, final)

        event = service.distributions[-1]
        assert event.withheld > 0

    def test_payment_is_pro_rata(self, service):
        final = self._run_six_months(service)
        service.enforce(final)
        paid = service.distribute_income(service.nav.distributable // 2, final)

        alice = paid[scenario.INVESTORS["Alice"]]
        charlie = paid[scenario.INVESTORS["Charlie"]]
        # Alice holds 400 shares to Charlie's 200.
        assert alice == pytest.approx(charlie * 2, rel=1e-9)


class TestSummary:
    def test_reports_frozen_count(self, service):
        service.enforce(scenario.INCEPTION)
        summary = service.summary(scenario.INCEPTION)
        assert summary["frozen_count"] == 2
        assert summary["holder_count"] == 5

    def test_accounting_identity_holds(self, service):
        """net value = asset value + income - fees, at all times."""
        for ts, readings in scenario.valuation_rounds():
            service.submit_valuation(readings, ts)
        for ts, amount, note in scenario.rent_payments():
            service.record_income(amount, ts, note)

        s = service.summary()
        assert s["total_value"] == (
            s["asset_value"] + s["accrued_income"] - s["accrued_fees"]
        )
