"""The enforcer: turns compliance decisions into chain state.

The engine decides who may hold an asset. The chain enforces it by
freezing accounts. This module reconciles the two -- it compares what
*should* be frozen against what *is* frozen, and issues the minimum set
of extrinsics to close the gap.

Reconciling rather than blindly re-applying matters for three reasons:
extrinsics cost money, each one is a chance to fail, and a freeze that
is already in place does not need re-sending. It also means the enforcer
is safe to run repeatedly -- it converges rather than thrashing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chain.interface import ChainClient, TxReceipt
from engine.compliance import ComplianceEngine
from engine.models import Holder, ReasonCode


@dataclass
class EnforcementPlan:
    """What the enforcer intends to do, before it does it.

    Produced separately from execution so it can be shown to an operator
    for review -- freezing someone's asset is not a step to take blindly.
    """

    to_freeze: dict[str, ReasonCode] = field(default_factory=dict)
    to_thaw: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.to_freeze and not self.to_thaw

    def __len__(self) -> int:
        return len(self.to_freeze) + len(self.to_thaw)


@dataclass
class EnforcementResult:
    """What actually happened."""

    plan: EnforcementPlan
    frozen: dict[str, TxReceipt] = field(default_factory=dict)
    thawed: dict[str, TxReceipt] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def applied(self) -> int:
        return len(self.frozen) + len(self.thawed)


class ComplianceEnforcer:
    """Keeps on-chain freeze state in sync with compliance rules."""

    def __init__(
        self,
        chain: ChainClient,
        engine: ComplianceEngine,
        asset_id: int,
    ):
        self.chain = chain
        self.engine = engine
        self.asset_id = asset_id

    # -- planning ------------------------------------------------------------

    def plan(self, now: int | None = None) -> EnforcementPlan:
        """Diff intended compliance state against actual chain state.

        Time comes from the chain unless overridden, so lock-up expiry is
        judged by the same clock that will enforce it.
        """
        if now is None:
            now = self.chain.now()

        on_chain = self.chain.holders(self.asset_id)
        plan = EnforcementPlan()

        # Registered holders: freeze those who fail, thaw those who pass.
        for address in self.engine.holders:
            result = self.engine.check_holder(address, now)
            currently_frozen = on_chain.get(address)
            is_frozen = currently_frozen.frozen if currently_frozen else False

            if result.allowed and is_frozen:
                plan.to_thaw.append(address)
            elif not result.allowed and not is_frozen:
                plan.to_freeze[address] = result.reason
            else:
                plan.unchanged += 1

        # Accounts holding the asset but absent from the registry. These
        # should not be able to move units: an unregistered holder has
        # no verified jurisdiction or accreditation.
        for address, state in on_chain.items():
            if address in self.engine.holders:
                continue
            if state.balance > 0 and not state.frozen:
                plan.to_freeze[address] = ReasonCode.UNKNOWN_HOLDER

        return plan

    # -- execution -----------------------------------------------------------

    def apply(self, plan: EnforcementPlan | None = None) -> EnforcementResult:
        """Execute a plan. Computes a fresh one if not supplied.

        Failures are collected rather than raised: one account failing to
        freeze should not prevent the others from being enforced.
        """
        if plan is None:
            plan = self.plan()

        result = EnforcementResult(plan=plan)

        for address, _reason in plan.to_freeze.items():
            receipt = self.chain.freeze(self.asset_id, address)
            if receipt.ok:
                result.frozen[address] = receipt
            else:
                result.failures[address] = receipt.error or "freeze failed"

        for address in plan.to_thaw:
            receipt = self.chain.thaw(self.asset_id, address)
            if receipt.ok:
                result.thawed[address] = receipt
            else:
                result.failures[address] = receipt.error or "thaw failed"

        return result

    def sync(self, now: int | None = None) -> EnforcementResult:
        """Plan and apply in one step."""
        return self.apply(self.plan(now))

    # -- introspection -------------------------------------------------------

    def drift(self, now: int | None = None) -> dict[str, str]:
        """Where chain state disagrees with compliance rules.

        Intended for monitoring: a non-empty result means the chain is
        not currently enforcing what the rules require.
        """
        plan = self.plan(now)
        out: dict[str, str] = {}
        for address, reason in plan.to_freeze.items():
            out[address] = f"should be frozen: {reason.name}"
        for address in plan.to_thaw:
            out[address] = "should be thawed: now compliant"
        return out

    def refresh_balances(self) -> None:
        """Pull on-chain balances into the engine's holder registry.

        The engine's concentration and holder-count rules depend on
        balances, which live on-chain. Without this the engine would
        evaluate caps against stale numbers.
        """
        on_chain = self.chain.holders(self.asset_id)
        for address, holder in self.engine.holders.items():
            state = on_chain.get(address)
            holder.balance = state.balance if state else 0


def holders_from_chain(
    chain: ChainClient,
    asset_id: int,
    jurisdiction: str = "UNKNOWN",
) -> dict[str, Holder]:
    """Seed a holder registry from chain state.

    Useful for bootstrapping against an asset that already has holders.
    Everyone defaults to an unknown jurisdiction, so they will fail any
    allowlist until an operator fills in real details -- failing closed
    is the correct default for a compliance system.
    """
    return {
        address: Holder(
            address=address,
            jurisdiction=jurisdiction,
            balance=state.balance,
        )
        for address, state in chain.holders(asset_id).items()
    }
