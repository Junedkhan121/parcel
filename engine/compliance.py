"""Compliance rule evaluation.

Given a holder registry and an issuer's configured rules, decide whether
a specific transfer is permitted -- and if not, say exactly why.

Rules are evaluated in a fixed order, cheapest and most absolute first,
so a denial always reports the most fundamental reason rather than an
incidental one. A sanctioned holder is reported as sanctioned even if
they also happen to be inside a lock-up.
"""

from __future__ import annotations

from .fixedpoint import bps_of
from .models import (
    AssetConfig,
    ComplianceResult,
    Decision,
    Holder,
    ReasonCode,
)


def _deny(reason: ReasonCode) -> ComplianceResult:
    return ComplianceResult(decision=Decision.DENY, reason=reason)


_ALLOW = ComplianceResult(decision=Decision.ALLOW, reason=ReasonCode.OK)


class ComplianceEngine:
    """Evaluates transfer eligibility for a single asset."""

    def __init__(self, asset: AssetConfig, holders: dict[str, Holder]):
        self.asset = asset
        self.holders = holders

    # -- holder-level checks -------------------------------------------------

    def check_holder(self, address: str, now: int) -> ComplianceResult:
        """Can this account hold the asset at all?

        This is the check that drives on-chain freezing: an account that
        fails here is frozen via Assets::freeze, so the restriction is
        enforced by the chain rather than by our frontend.
        """
        rules = self.asset.rules

        if rules.paused:
            return _deny(ReasonCode.ASSET_PAUSED)

        holder = self.holders.get(address)
        if holder is None:
            return _deny(ReasonCode.UNKNOWN_HOLDER)

        if holder.sanctioned:
            return _deny(ReasonCode.SANCTIONED)

        if rules.allowed_jurisdictions and holder.jurisdiction not in rules.allowed_jurisdictions:
            return _deny(ReasonCode.JURISDICTION_NOT_PERMITTED)

        if rules.require_accreditation and not holder.accredited:
            return _deny(ReasonCode.ACCREDITATION_REQUIRED)

        if holder.is_locked(now):
            return _deny(ReasonCode.LOCKUP_ACTIVE)

        return _ALLOW

    def eligible_addresses(self, now: int) -> set[str]:
        """Every registered address that currently passes holder checks."""
        return {
            address
            for address in self.holders
            if self.check_holder(address, now).allowed
        }

    def ineligible_addresses(self, now: int) -> dict[str, ReasonCode]:
        """Addresses that fail, mapped to why. Drives freeze/thaw and the UI."""
        failures: dict[str, ReasonCode] = {}
        for address in self.holders:
            result = self.check_holder(address, now)
            if not result.allowed:
                failures[address] = result.reason
        return failures

    # -- transfer-level checks ----------------------------------------------

    def check_transfer(
        self,
        sender: str,
        recipient: str,
        amount: int,
        now: int,
    ) -> ComplianceResult:
        """Can `amount` move from sender to recipient right now?

        Both parties must be eligible holders, and the transfer must not
        breach the asset's structural caps.
        """
        sender_check = self.check_holder(sender, now)
        if not sender_check.allowed:
            return sender_check

        recipient_check = self.check_holder(recipient, now)
        if not recipient_check.allowed:
            return recipient_check

        rules = self.asset.rules

        # A new holder joining must not breach the holder-count cap.
        if rules.max_holders is not None:
            recipient_holder = self.holders[recipient]
            joining = recipient_holder.balance == 0 and amount > 0
            if joining and self._active_holder_count() >= rules.max_holders:
                return _deny(ReasonCode.HOLDER_CAP_REACHED)

        # Concentration cap: no single holder above max_holder_bps of supply.
        if rules.max_holder_bps is not None:
            supply = self.total_supply()
            if supply > 0:
                projected = self.holders[recipient].balance + amount
                if bps_of(projected, supply) > rules.max_holder_bps:
                    return _deny(ReasonCode.PER_HOLDER_LIMIT_EXCEEDED)

        return _ALLOW

    # -- helpers ------------------------------------------------------------

    def total_supply(self) -> int:
        return sum(h.balance for h in self.holders.values())

    def _active_holder_count(self) -> int:
        return sum(1 for h in self.holders.values() if h.balance > 0)
