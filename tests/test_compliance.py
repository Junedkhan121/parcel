import pytest

from engine.compliance import ComplianceEngine
from engine.fixedpoint import to_fixed
from engine.models import AssetConfig, ComplianceRules, Holder, ReasonCode

NOW = 1_760_000_000  # fixed timestamp so tests are deterministic
DAY = 24 * 3600

ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
CHARLIE = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"


def make_engine(rules: ComplianceRules, holders: list[Holder]) -> ComplianceEngine:
    asset = AssetConfig(
        asset_id=42,
        name="Warehouse Unit 7, Rotterdam",
        symbol="WH7",
        rules=rules,
    )
    return ComplianceEngine(asset, {h.address: h for h in holders})


class TestHolderChecks:
    def test_eligible_holder_passes(self):
        engine = make_engine(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        result = engine.check_holder(ALICE, NOW)
        assert result.allowed
        assert result.reason is ReasonCode.OK

    def test_unregistered_address_denied(self):
        engine = make_engine(ComplianceRules(), [])
        result = engine.check_holder(ALICE, NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.UNKNOWN_HOLDER

    def test_wrong_jurisdiction_denied(self):
        engine = make_engine(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL", "DE"})),
            [Holder(ALICE, "RU")],
        )
        result = engine.check_holder(ALICE, NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.JURISDICTION_NOT_PERMITTED

    def test_empty_jurisdiction_list_permits_all(self):
        engine = make_engine(ComplianceRules(), [Holder(ALICE, "RU")])
        assert engine.check_holder(ALICE, NOW).allowed

    def test_accreditation_required(self):
        engine = make_engine(
            ComplianceRules(require_accreditation=True),
            [Holder(ALICE, "NL", accredited=False)],
        )
        result = engine.check_holder(ALICE, NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.ACCREDITATION_REQUIRED

    def test_accredited_holder_passes(self):
        engine = make_engine(
            ComplianceRules(require_accreditation=True),
            [Holder(ALICE, "NL", accredited=True)],
        )
        assert engine.check_holder(ALICE, NOW).allowed

    def test_active_lockup_denied(self):
        engine = make_engine(
            ComplianceRules(),
            [Holder(ALICE, "NL", lockup_until=NOW + DAY)],
        )
        result = engine.check_holder(ALICE, NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.LOCKUP_ACTIVE

    def test_expired_lockup_passes(self):
        engine = make_engine(
            ComplianceRules(),
            [Holder(ALICE, "NL", lockup_until=NOW - DAY)],
        )
        assert engine.check_holder(ALICE, NOW).allowed

    def test_sanctioned_holder_denied(self):
        engine = make_engine(
            ComplianceRules(),
            [Holder(ALICE, "NL", sanctioned=True)],
        )
        result = engine.check_holder(ALICE, NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.SANCTIONED

    def test_paused_asset_denies_everyone(self):
        engine = make_engine(
            ComplianceRules(paused=True),
            [Holder(ALICE, "NL", accredited=True)],
        )
        result = engine.check_holder(ALICE, NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.ASSET_PAUSED


class TestReasonPrecedence:
    """A denial should report the most fundamental cause, not an incidental one."""

    def test_sanctions_outrank_jurisdiction(self):
        engine = make_engine(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(ALICE, "RU", sanctioned=True)],
        )
        assert engine.check_holder(ALICE, NOW).reason is ReasonCode.SANCTIONED

    def test_sanctions_outrank_lockup(self):
        engine = make_engine(
            ComplianceRules(),
            [Holder(ALICE, "NL", sanctioned=True, lockup_until=NOW + DAY)],
        )
        assert engine.check_holder(ALICE, NOW).reason is ReasonCode.SANCTIONED

    def test_pause_outranks_everything(self):
        engine = make_engine(
            ComplianceRules(paused=True),
            [Holder(ALICE, "RU", sanctioned=True)],
        )
        assert engine.check_holder(ALICE, NOW).reason is ReasonCode.ASSET_PAUSED


class TestEligibilitySets:
    def test_partitions_holders(self):
        engine = make_engine(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(ALICE, "NL"),
                Holder(BOB, "RU"),
                Holder(CHARLIE, "NL", sanctioned=True),
            ],
        )
        assert engine.eligible_addresses(NOW) == {ALICE}

        ineligible = engine.ineligible_addresses(NOW)
        assert ineligible == {
            BOB: ReasonCode.JURISDICTION_NOT_PERMITTED,
            CHARLIE: ReasonCode.SANCTIONED,
        }

    def test_lockup_expiry_changes_eligibility_over_time(self):
        engine = make_engine(
            ComplianceRules(),
            [Holder(ALICE, "NL", lockup_until=NOW + DAY)],
        )
        assert engine.eligible_addresses(NOW) == set()
        assert engine.eligible_addresses(NOW + 2 * DAY) == {ALICE}


class TestTransferChecks:
    def test_permitted_transfer(self):
        engine = make_engine(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "NL", balance=to_fixed("50")),
            ],
        )
        assert engine.check_transfer(ALICE, BOB, to_fixed("10"), NOW).allowed

    def test_ineligible_sender_blocks(self):
        engine = make_engine(
            ComplianceRules(),
            [
                Holder(ALICE, "NL", balance=to_fixed("100"), sanctioned=True),
                Holder(BOB, "NL"),
            ],
        )
        result = engine.check_transfer(ALICE, BOB, to_fixed("10"), NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.SANCTIONED

    def test_ineligible_recipient_blocks(self):
        engine = make_engine(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "RU"),
            ],
        )
        result = engine.check_transfer(ALICE, BOB, to_fixed("10"), NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.JURISDICTION_NOT_PERMITTED

    def test_holder_cap_blocks_new_joiner(self):
        engine = make_engine(
            ComplianceRules(max_holders=2),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "NL", balance=to_fixed("100")),
                Holder(CHARLIE, "NL", balance=0),
            ],
        )
        result = engine.check_transfer(ALICE, CHARLIE, to_fixed("10"), NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.HOLDER_CAP_REACHED

    def test_holder_cap_allows_existing_holder(self):
        # At the cap, topping up someone who already holds is fine --
        # the holder count does not change.
        engine = make_engine(
            ComplianceRules(max_holders=2),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "NL", balance=to_fixed("100")),
            ],
        )
        assert engine.check_transfer(ALICE, BOB, to_fixed("10"), NOW).allowed

    def test_concentration_cap_blocks(self):
        # 40% cap; Bob at 50 of 200 supply would reach 90/200 = 45%.
        engine = make_engine(
            ComplianceRules(max_holder_bps=4_000),
            [
                Holder(ALICE, "NL", balance=to_fixed("150")),
                Holder(BOB, "NL", balance=to_fixed("50")),
            ],
        )
        result = engine.check_transfer(ALICE, BOB, to_fixed("40"), NOW)
        assert not result.allowed
        assert result.reason is ReasonCode.PER_HOLDER_LIMIT_EXCEEDED

    def test_concentration_cap_allows_under_limit(self):
        engine = make_engine(
            ComplianceRules(max_holder_bps=4_000),
            [
                Holder(ALICE, "NL", balance=to_fixed("150")),
                Holder(BOB, "NL", balance=to_fixed("50")),
            ],
        )
        assert engine.check_transfer(ALICE, BOB, to_fixed("10"), NOW).allowed


class TestSupply:
    def test_total_supply_sums_balances(self):
        engine = make_engine(
            ComplianceRules(),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "NL", balance=to_fixed("50")),
            ],
        )
        assert engine.total_supply() == to_fixed("150")

    def test_empty_registry_has_zero_supply(self):
        assert make_engine(ComplianceRules(), []).total_supply() == 0
