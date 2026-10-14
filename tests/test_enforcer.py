import pytest

from chain.enforcer import ComplianceEnforcer, holders_from_chain
from chain.interface import AssetMetadata
from chain.mock import MockChain
from engine.compliance import ComplianceEngine
from engine.fixedpoint import to_fixed
from engine.models import AssetConfig, ComplianceRules, Holder, ReasonCode

ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
CHARLIE = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"

ASSET = 42
META = AssetMetadata(name="Warehouse Unit 7, Rotterdam", symbol="WH7", decimals=14)
DAY = 24 * 3600


def build(rules: ComplianceRules, holders: list[Holder]):
    chain = MockChain()
    chain.create_asset(ASSET, META, min_balance=0)
    for h in holders:
        if h.balance > 0:
            chain.mint(ASSET, h.address, h.balance)

    asset = AssetConfig(asset_id=ASSET, name=META.name, symbol=META.symbol, rules=rules)
    engine = ComplianceEngine(asset, {h.address: h for h in holders})
    return chain, engine, ComplianceEnforcer(chain, engine, ASSET)


class TestPlanning:
    def test_compliant_holders_need_no_action(self):
        _, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        plan = enforcer.plan()
        assert plan.is_empty
        assert plan.unchanged == 1

    def test_ineligible_holder_is_planned_for_freeze(self):
        _, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(BOB, "RU", balance=to_fixed("50"))],
        )
        plan = enforcer.plan()
        assert plan.to_freeze == {BOB: ReasonCode.JURISDICTION_NOT_PERMITTED}
        assert not plan.to_thaw

    def test_newly_compliant_holder_is_planned_for_thaw(self):
        chain, _, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        chain.freeze(ASSET, ALICE)
        plan = enforcer.plan()
        assert plan.to_thaw == [ALICE]
        assert not plan.to_freeze

    def test_unregistered_holder_is_frozen(self):
        # Someone holding the asset without being in the registry has no
        # verified jurisdiction, so they must not be able to move it.
        chain, _, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        chain.mint(ASSET, CHARLIE, to_fixed("25"))
        plan = enforcer.plan()
        assert plan.to_freeze == {CHARLIE: ReasonCode.UNKNOWN_HOLDER}

    def test_unregistered_account_with_no_balance_ignored(self):
        chain, _, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        chain.thaw(ASSET, CHARLIE)  # touches the account without a balance
        plan = enforcer.plan()
        assert CHARLIE not in plan.to_freeze

    def test_plan_length(self):
        _, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "RU", balance=to_fixed("50")),
                Holder(CHARLIE, "RU", balance=to_fixed("25")),
            ],
        )
        plan = enforcer.plan()
        assert len(plan) == 2


class TestApplication:
    def test_freeze_is_applied_on_chain(self):
        chain, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(BOB, "RU", balance=to_fixed("50"))],
        )
        result = enforcer.apply()
        assert result.ok
        assert BOB in result.frozen
        assert chain.account_state(ASSET, BOB).frozen

    def test_thaw_is_applied_on_chain(self):
        chain, _, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        chain.freeze(ASSET, ALICE)
        result = enforcer.apply()
        assert ALICE in result.thawed
        assert not chain.account_state(ASSET, ALICE).frozen

    def test_sync_converges(self):
        chain, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "RU", balance=to_fixed("50")),
            ],
        )
        enforcer.sync()
        assert chain.account_state(ASSET, BOB).frozen
        # Second run has nothing left to do.
        assert enforcer.sync().plan.is_empty

    def test_sync_issues_no_redundant_extrinsics(self):
        chain, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(BOB, "RU", balance=to_fixed("50"))],
        )
        enforcer.sync()
        count = len(chain.extrinsics)
        enforcer.sync()
        assert len(chain.extrinsics) == count


class TestEnforcementBite:
    def test_frozen_holder_cannot_transfer(self):
        """The whole point: a compliance failure blocks a real transfer."""
        chain, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "RU", balance=to_fixed("50")),
            ],
        )
        # Before enforcement, the chain happily moves the asset.
        assert chain.transfer(ASSET, BOB, ALICE, to_fixed("10")).ok

        enforcer.sync()

        receipt = chain.transfer(ASSET, BOB, ALICE, to_fixed("10"))
        assert not receipt.ok
        assert "frozen" in receipt.error

    def test_ineligible_recipient_cannot_receive(self):
        chain, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "RU", balance=to_fixed("50")),
            ],
        )
        enforcer.sync()
        receipt = chain.transfer(ASSET, ALICE, BOB, to_fixed("10"))
        assert not receipt.ok


class TestTimeDependentRules:
    def test_lockup_expiry_thaws_holder(self):
        chain, _, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=to_fixed("100"), lockup_until=0)],
        )
        # Put Alice in a lock-up that ends a day from chain time.
        enforcer.engine.holders[ALICE].lockup_until = chain.now() + DAY

        enforcer.sync()
        assert chain.account_state(ASSET, ALICE).frozen

        chain.advance(2 * DAY)
        enforcer.sync()
        assert not chain.account_state(ASSET, ALICE).frozen

    def test_uses_chain_time_not_wall_clock(self):
        chain, _, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        enforcer.engine.holders[ALICE].lockup_until = chain.now() + DAY
        assert enforcer.plan().to_freeze == {ALICE: ReasonCode.LOCKUP_ACTIVE}


class TestPauseSwitch:
    def test_pausing_freezes_everyone(self):
        chain, engine, enforcer = build(
            ComplianceRules(),
            [
                Holder(ALICE, "NL", balance=to_fixed("100")),
                Holder(BOB, "NL", balance=to_fixed("50")),
            ],
        )
        engine.asset.rules.paused = True
        enforcer.sync()
        assert chain.account_state(ASSET, ALICE).frozen
        assert chain.account_state(ASSET, BOB).frozen

    def test_unpausing_restores_everyone(self):
        chain, engine, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=to_fixed("100"))],
        )
        engine.asset.rules.paused = True
        enforcer.sync()
        engine.asset.rules.paused = False
        enforcer.sync()
        assert not chain.account_state(ASSET, ALICE).frozen


class TestFailureHandling:
    def test_one_failure_does_not_block_others(self):
        chain, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [
                Holder(BOB, "RU", balance=to_fixed("50")),
                Holder(CHARLIE, "RU", balance=to_fixed("25")),
            ],
        )

        original_freeze = chain.freeze

        def flaky_freeze(asset_id, address):
            if address == BOB:
                from chain.interface import TxReceipt

                return TxReceipt(ok=False, error="node rejected extrinsic")
            return original_freeze(asset_id, address)

        chain.freeze = flaky_freeze  # type: ignore[method-assign]

        result = enforcer.apply()
        assert not result.ok
        assert BOB in result.failures
        assert CHARLIE in result.frozen


class TestBalanceRefresh:
    def test_pulls_chain_balances_into_registry(self):
        chain, engine, enforcer = build(
            ComplianceRules(),
            [Holder(ALICE, "NL", balance=0)],
        )
        chain.mint(ASSET, ALICE, to_fixed("500"))
        assert engine.holders[ALICE].balance == 0

        enforcer.refresh_balances()
        assert engine.holders[ALICE].balance == to_fixed("500")

    def test_concentration_rule_uses_refreshed_balances(self):
        chain, engine, enforcer = build(
            ComplianceRules(max_holder_bps=5_000),
            [Holder(ALICE, "NL", balance=0), Holder(BOB, "NL", balance=0)],
        )
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.mint(ASSET, BOB, to_fixed("100"))
        enforcer.refresh_balances()
        # Bob at 100 of 200 is exactly 50%; another 50 would breach.
        result = engine.check_transfer(ALICE, BOB, to_fixed("50"), chain.now())
        assert result.reason is ReasonCode.PER_HOLDER_LIMIT_EXCEEDED


class TestBootstrap:
    def test_seeds_registry_from_chain(self):
        chain = MockChain()
        chain.create_asset(ASSET, META, min_balance=0)
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.mint(ASSET, BOB, to_fixed("50"))

        holders = holders_from_chain(chain, ASSET)
        assert set(holders) == {ALICE, BOB}
        assert holders[ALICE].balance == to_fixed("100")

    def test_bootstrapped_holders_fail_closed(self):
        # Unknown jurisdiction must not pass an allowlist.
        chain = MockChain()
        chain.create_asset(ASSET, META, min_balance=0)
        chain.mint(ASSET, ALICE, to_fixed("100"))

        holders = holders_from_chain(chain, ASSET)
        asset = AssetConfig(
            asset_id=ASSET,
            name=META.name,
            symbol=META.symbol,
            rules=ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
        )
        engine = ComplianceEngine(asset, holders)
        assert not engine.check_holder(ALICE, chain.now()).allowed


class TestDrift:
    def test_reports_disagreement(self):
        _, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(BOB, "RU", balance=to_fixed("50"))],
        )
        drift = enforcer.drift()
        assert BOB in drift
        assert "JURISDICTION" in drift[BOB]

    def test_empty_when_synced(self):
        _, _, enforcer = build(
            ComplianceRules(allowed_jurisdictions=frozenset({"NL"})),
            [Holder(BOB, "RU", balance=to_fixed("50"))],
        )
        enforcer.sync()
        assert enforcer.drift() == {}
