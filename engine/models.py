"""Core data models for Parcel.

All monetary values are integers scaled by DECIMALS (14, matching tPOTv3).
Floats are never used for money. See fixedpoint.py for conversion helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


# Portaldot tPOTv3 uses 14 decimals, not the EVM-conventional 18.
DECIMALS = 14
SCALE = 10**DECIMALS


class ReasonCode(IntEnum):
    """Why a compliance check passed or failed.

    Numeric so they can be emitted on-chain cheaply and rendered
    as human-readable text in the frontend.
    """

    OK = 0
    UNKNOWN_HOLDER = 1
    JURISDICTION_NOT_PERMITTED = 2
    ACCREDITATION_REQUIRED = 3
    LOCKUP_ACTIVE = 4
    HOLDER_CAP_REACHED = 5
    PER_HOLDER_LIMIT_EXCEEDED = 6
    SANCTIONED = 7
    ASSET_PAUSED = 8


REASON_TEXT: dict[ReasonCode, str] = {
    ReasonCode.OK: "Permitted",
    ReasonCode.UNKNOWN_HOLDER: "Account is not a registered holder",
    ReasonCode.JURISDICTION_NOT_PERMITTED: "Holder jurisdiction is not permitted for this asset",
    ReasonCode.ACCREDITATION_REQUIRED: "Asset requires an accredited investor",
    ReasonCode.LOCKUP_ACTIVE: "Holder is still within the lock-up period",
    ReasonCode.HOLDER_CAP_REACHED: "Asset has reached its maximum holder count",
    ReasonCode.PER_HOLDER_LIMIT_EXCEEDED: "Transfer would exceed the per-holder ownership cap",
    ReasonCode.SANCTIONED: "Holder appears on the sanctions denylist",
    ReasonCode.ASSET_PAUSED: "Asset transfers are currently paused by the issuer",
}


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class ComplianceResult:
    decision: Decision
    reason: ReasonCode

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def text(self) -> str:
        return REASON_TEXT[self.reason]


@dataclass
class Holder:
    """A registered investor in an asset.

    `address` is an SS58 Substrate address. `balance` is scaled by SCALE.
    """

    address: str
    jurisdiction: str
    accredited: bool = False
    sanctioned: bool = False
    lockup_until: int = 0  # unix seconds; 0 means no lock-up
    balance: int = 0

    def is_locked(self, now: int) -> bool:
        return self.lockup_until > now


@dataclass
class ComplianceRules:
    """Issuer-configured transfer restrictions for one asset.

    Modeled on constraints common to private placements. These are
    configuration, not regulatory advice -- see README.
    """

    allowed_jurisdictions: frozenset[str] = frozenset()
    require_accreditation: bool = False
    max_holders: int | None = None
    # Maximum fraction of total supply one holder may own, in basis points.
    # 10_000 bps = 100%. None means no cap.
    max_holder_bps: int | None = None
    paused: bool = False


@dataclass
class AssetConfig:
    """Identity and economics of a tokenized real-world asset."""

    asset_id: int
    name: str
    symbol: str
    # Management fee charged annually against NAV, in basis points.
    management_fee_bps: int = 0
    rules: ComplianceRules = field(default_factory=ComplianceRules)


@dataclass(frozen=True)
class OracleReading:
    """One valuation submission from one source."""

    source: str
    value: int  # scaled by SCALE
    timestamp: int  # unix seconds


@dataclass(frozen=True)
class NavSnapshot:
    """Computed net asset value at a point in time."""

    nav_per_share: int  # scaled by SCALE
    total_value: int  # scaled by SCALE
    total_shares: int  # scaled by SCALE
    epoch: int  # unix seconds
    accrued_income: int = 0
    accrued_fees: int = 0
