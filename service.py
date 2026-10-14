"""The Parcel service.

Ties the pieces together into one object the frontend and CLI both use:
reconcile oracle feeds, accrue NAV, enforce compliance on-chain, and
report what happened.

Kept deliberately thin. The logic lives in engine/ and chain/; this is
orchestration and an audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chain.enforcer import ComplianceEnforcer, EnforcementResult
from chain.interface import ChainClient
from engine.compliance import ComplianceEngine
from engine.models import (
    AssetConfig,
    ComplianceResult,
    Holder,
    NavSnapshot,
    OracleReading,
    ReasonCode,
)
from engine.nav import NavEngine
from engine.oracle import OracleReconciler, Reconciliation


@dataclass
class ValuationEvent:
    """An attempt to update the asset's valuation, successful or not."""

    timestamp: int
    reconciliation: Reconciliation
    applied: bool

    @property
    def rejected_sources(self) -> list[str]:
        return [r.reading.source for r in self.reconciliation.rejected]


@dataclass
class IncomeEvent:
    timestamp: int
    amount: int
    note: str


@dataclass
class DistributionEvent:
    """A payment of accrued income out to holders."""

    timestamp: int
    total_paid: int
    per_holder: dict[str, int] = field(default_factory=dict)
    # Entitlement belonging to ineligible holders, retained in the
    # vehicle rather than redistributed to everyone else.
    withheld: int = 0


@dataclass
class HolderView:
    """Everything the UI needs about one holder."""

    address: str
    label: str
    jurisdiction: str
    balance: int
    value: int
    frozen_on_chain: bool
    compliance: ComplianceResult

    @property
    def enforced_correctly(self) -> bool:
        """Does chain state match what compliance requires?"""
        return self.frozen_on_chain == (not self.compliance.allowed)


class ParcelService:
    """Operates one tokenized asset end to end."""

    def __init__(
        self,
        chain: ChainClient,
        asset: AssetConfig,
        holders: dict[str, Holder],
        total_shares: int,
        inception: int,
        reconciler: OracleReconciler | None = None,
    ):
        self.chain = chain
        self.asset = asset
        self.engine = ComplianceEngine(asset, holders)
        self.nav = NavEngine(asset, total_shares=total_shares, inception=inception)
        self.reconciler = reconciler or OracleReconciler()
        self.enforcer = ComplianceEnforcer(chain, self.engine, asset.asset_id)

        self.valuations: list[ValuationEvent] = []
        self.income: list[IncomeEvent] = []
        self.distributions: list[DistributionEvent] = []
        self.nav_history: list[NavSnapshot] = []
        self._last_accepted_valuation: int | None = None

    # -- valuation -----------------------------------------------------------

    def submit_valuation(
        self,
        readings: list[OracleReading],
        now: int,
    ) -> ValuationEvent:
        """Reconcile oracle readings and update NAV if they survive.

        A failed reconciliation is recorded, not discarded: "we declined
        to publish, and here is why" is part of the audit trail.
        """
        result = self.reconciler.reconcile(
            readings, now, previous_value=self._last_accepted_valuation
        )

        applied = False
        if result.ok and result.value is not None:
            self.nav.set_valuation(result.value, now)
            self._last_accepted_valuation = result.value
            applied = True
            self._snapshot(now)

        event = ValuationEvent(timestamp=now, reconciliation=result, applied=applied)
        self.valuations.append(event)
        return event

    def record_income(self, amount: int, now: int, note: str = "") -> IncomeEvent:
        """Record income received by the asset, such as rent."""
        self.nav.add_income(amount, now)
        event = IncomeEvent(timestamp=now, amount=amount, note=note)
        self.income.append(event)
        self._snapshot(now)
        return event

    def _snapshot(self, now: int) -> NavSnapshot:
        snap = self.nav.snapshot(now)
        self.nav_history.append(snap)
        return snap

    # -- compliance ----------------------------------------------------------

    def enforce(self, now: int | None = None) -> EnforcementResult:
        """Bring on-chain freeze state in line with compliance rules."""
        self.enforcer.refresh_balances()
        return self.enforcer.sync(now)

    def check_transfer(
        self,
        sender: str,
        recipient: str,
        amount: int,
        now: int | None = None,
    ) -> ComplianceResult:
        """Would this transfer be permitted?

        Answers without submitting anything, so the UI can explain a
        refusal before the user spends gas discovering it.
        """
        if now is None:
            now = self.chain.now()
        self.enforcer.refresh_balances()
        return self.engine.check_transfer(sender, recipient, amount, now)

    def attempt_transfer(
        self,
        sender: str,
        recipient: str,
        amount: int,
        now: int | None = None,
    ) -> tuple[ComplianceResult, str | None]:
        """Check compliance, then submit if permitted.

        Returns the compliance verdict and the chain's error if the
        submission itself failed. Both can fail independently: rules may
        permit a transfer the chain still rejects for insufficient
        balance.
        """
        verdict = self.check_transfer(sender, recipient, amount, now)
        if not verdict.allowed:
            return verdict, None

        receipt = self.chain.transfer(self.asset.asset_id, sender, recipient, amount)
        return verdict, None if receipt.ok else receipt.error

    # -- distribution --------------------------------------------------------

    def distribute_income(self, amount: int, now: int) -> dict[str, int]:
        """Pay accrued income out to eligible holders, pro-rata.

        Entitlements are denominated in the asset's currency, not in
        shares. Minting shares pro-rata would be a stock split: every
        holder's fraction of an unchanged pool stays the same, NAV per
        share falls proportionally, and nobody is better off.

        A real distribution removes value from the vehicle, so NAV per
        share falls by the amount paid. The income already benefited
        holders when it was recorded -- this is them realising it.

        Ineligible holders are excluded. Their entitlement stays in the
        vehicle rather than being redistributed, since it is owed to
        them once they become compliant again.

        Returns an address -> amount-paid map, in currency units.
        """
        self.enforcer.refresh_balances()
        supply = self.engine.total_supply()
        if supply == 0:
            return {}

        payable = self.nav.pay_distribution(amount, now)
        if payable == 0:
            return {}

        eligible = self.engine.eligible_addresses(now)
        entitlements: dict[str, int] = {}

        for address in eligible:
            holder = self.engine.holders[address]
            if holder.balance == 0:
                continue
            share = (payable * holder.balance) // supply
            if share > 0:
                entitlements[address] = share

        # Only the eligible portion actually leaves the vehicle; return
        # the remainder so it stays available to distribute later.
        unclaimed = payable - sum(entitlements.values())
        if unclaimed > 0:
            self.nav.accrued_income += unclaimed
            self.nav.distributed -= unclaimed

        self.distributions.append(
            DistributionEvent(
                timestamp=now,
                total_paid=sum(entitlements.values()),
                per_holder=dict(entitlements),
                withheld=unclaimed,
            )
        )
        self._snapshot(now)
        return entitlements

    # -- reporting -----------------------------------------------------------

    def holder_views(self, now: int | None = None) -> list[HolderView]:
        """Per-holder state for the UI, sorted by holding size."""
        if now is None:
            now = self.chain.now()

        self.enforcer.refresh_balances()
        on_chain = self.chain.holders(self.asset.asset_id)

        views: list[HolderView] = []
        for address, holder in self.engine.holders.items():
            state = on_chain.get(address)
            balance = state.balance if state else 0
            views.append(
                HolderView(
                    address=address,
                    label=_label(address),
                    jurisdiction=holder.jurisdiction,
                    balance=balance,
                    value=self.nav.value_of(balance),
                    frozen_on_chain=state.frozen if state else False,
                    compliance=self.engine.check_holder(address, now),
                )
            )

        views.sort(key=lambda v: v.balance, reverse=True)
        return views

    def summary(self, now: int | None = None) -> dict:
        """Asset-level state for the UI header."""
        if now is None:
            now = self.chain.now()

        views = self.holder_views(now)
        ineligible = [v for v in views if not v.compliance.allowed]

        return {
            "asset_id": self.asset.asset_id,
            "name": self.asset.name,
            "symbol": self.asset.symbol,
            "nav_per_share": self.nav.nav_per_share,
            "total_value": self.nav.net_value,
            "total_shares": self.nav.total_shares,
            "asset_value": self.nav.asset_value,
            "accrued_income": self.nav.accrued_income,
            "accrued_fees": self.nav.accrued_fees,
            "distributable": self.nav.distributable,
            "distributed": self.nav.distributed,
            "holder_count": sum(1 for v in views if v.balance > 0),
            "frozen_count": sum(1 for v in views if v.frozen_on_chain),
            "ineligible_count": len(ineligible),
            "drift": self.enforcer.drift(now),
            "epoch": now,
        }

    def rejected_readings(self) -> list[tuple[int, str, str]]:
        """Every oracle reading ever rejected, as (timestamp, source, reason).

        Worth surfacing in the UI: it is evidence the reconciler is doing
        something, not just averaging.
        """
        out: list[tuple[int, str, str]] = []
        for event in self.valuations:
            for rejected in event.reconciliation.rejected:
                out.append(
                    (event.timestamp, rejected.reading.source, rejected.reason.value)
                )
        return out


def _label(address: str) -> str:
    """Friendly name for an address, falling back to a truncated form."""
    try:
        from scenario import label_for

        return label_for(address)
    except ImportError:  # pragma: no cover
        return f"{address[:6]}...{address[-4:]}"
