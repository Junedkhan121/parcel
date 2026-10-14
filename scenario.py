"""The demo asset: Warehouse Unit 7, Rotterdam.

A concrete scenario beats an abstract one. This module builds a
tokenized industrial property with a believable history -- five
investors across three jurisdictions, three months of rent, two
revaluations, and one investor who falls out of compliance.

Everything is deterministic. The same seed produces the same chain
state every run, which matters when demoing live.
"""

from __future__ import annotations

from dataclasses import dataclass

from chain.interface import AssetMetadata, ChainClient
from engine.fixedpoint import to_fixed
from engine.models import AssetConfig, ComplianceRules, Holder, OracleReading

ASSET_ID = 42

NAME = "Warehouse Unit 7, Rotterdam"
SYMBOL = "WH7"

# Industrial unit in the Port of Rotterdam. Valued at EUR 2.4M,
# divided into 1,000 shares of EUR 2,400 each.
INITIAL_VALUATION = to_fixed("2400000")
TOTAL_SHARES = to_fixed("1000")
MANAGEMENT_FEE_BPS = 200  # 2% a year

DAY = 24 * 3600
MONTH = 30 * DAY

# Deterministic start: 2026-01-15 00:00:00 UTC.
INCEPTION = 1_768_435_200


# Five investors. Addresses are the standard Substrate dev accounts so
# the demo works on a --dev node without creating keys.
INVESTORS: dict[str, str] = {
    "Alice": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "Bob": "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "Charlie": "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y",
    "Dave": "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy",
    "Eve": "5HGjWAeFDfFCe27dNhdJPAZLT1BgoLrhh2a3LFoLVb7qEPGu",
}


@dataclass
class ScenarioHolder:
    label: str
    holder: Holder
    shares: int


def holders() -> list[ScenarioHolder]:
    """The investor register.

    Dave is Russian and will fail the jurisdiction allowlist. Eve is
    inside a twelve-month lock-up. Both are deliberate: a compliance
    demo needs someone to actually fail.
    """
    return [
        ScenarioHolder(
            label="Alice",
            shares=to_fixed("400"),
            holder=Holder(
                address=INVESTORS["Alice"],
                jurisdiction="NL",
                accredited=True,
                balance=to_fixed("400"),
            ),
        ),
        ScenarioHolder(
            label="Bob",
            shares=to_fixed("250"),
            holder=Holder(
                address=INVESTORS["Bob"],
                jurisdiction="DE",
                accredited=True,
                balance=to_fixed("250"),
            ),
        ),
        ScenarioHolder(
            label="Charlie",
            shares=to_fixed("200"),
            holder=Holder(
                address=INVESTORS["Charlie"],
                jurisdiction="NL",
                accredited=True,
                balance=to_fixed("200"),
            ),
        ),
        ScenarioHolder(
            label="Dave",
            shares=to_fixed("100"),
            holder=Holder(
                address=INVESTORS["Dave"],
                jurisdiction="RU",  # fails the allowlist
                accredited=True,
                balance=to_fixed("100"),
            ),
        ),
        ScenarioHolder(
            label="Eve",
            shares=to_fixed("50"),
            holder=Holder(
                address=INVESTORS["Eve"],
                jurisdiction="NL",
                accredited=True,
                lockup_until=INCEPTION + 365 * DAY,  # 12-month lock-up
                balance=to_fixed("50"),
            ),
        ),
    ]


def rules() -> ComplianceRules:
    """Restrictions typical of a European private placement.

    EEA investors only, accredited, no single holder above 45% of the
    issue, capped at twenty holders.
    """
    return ComplianceRules(
        allowed_jurisdictions=frozenset({"NL", "DE", "FR", "BE", "LU", "IE"}),
        require_accreditation=True,
        max_holders=20,
        max_holder_bps=4_500,
        paused=False,
    )


def asset_config() -> AssetConfig:
    return AssetConfig(
        asset_id=ASSET_ID,
        name=NAME,
        symbol=SYMBOL,
        management_fee_bps=MANAGEMENT_FEE_BPS,
        rules=rules(),
    )


def asset_metadata() -> AssetMetadata:
    return AssetMetadata(name=NAME, symbol=SYMBOL, decimals=14)


# -- valuation history -------------------------------------------------------


def valuation_rounds() -> list[tuple[int, list[OracleReading]]]:
    """Quarterly valuations as (timestamp, readings) pairs.

    Three independent sources: a chartered surveyor, a market index, and
    a broker quote. They disagree slightly, which is realistic -- and the
    reconciler's job is to produce one number from the disagreement.

    The third round contains a deliberately corrupt reading to show
    outlier rejection working on real data rather than a toy case.
    """
    return [
        # Q1: inception valuation. Sources cluster tightly.
        (
            INCEPTION,
            [
                OracleReading("surveyor_nl", to_fixed("2400000"), INCEPTION),
                OracleReading("index_eu_industrial", to_fixed("2412000"), INCEPTION),
                OracleReading("broker_rotterdam", to_fixed("2395000"), INCEPTION),
            ],
        ),
        # Q2: modest appreciation, about 2.5%.
        (
            INCEPTION + 3 * MONTH,
            [
                OracleReading("surveyor_nl", to_fixed("2460000"), INCEPTION + 3 * MONTH),
                OracleReading(
                    "index_eu_industrial", to_fixed("2475000"), INCEPTION + 3 * MONTH
                ),
                OracleReading(
                    "broker_rotterdam", to_fixed("2450000"), INCEPTION + 3 * MONTH
                ),
            ],
        ),
        # Q3: the broker feed misplaces a decimal point. The median and
        # MAD check must discard it rather than let NAV jump 10x.
        (
            INCEPTION + 6 * MONTH,
            [
                OracleReading("surveyor_nl", to_fixed("2510000"), INCEPTION + 6 * MONTH),
                OracleReading(
                    "index_eu_industrial", to_fixed("2498000"), INCEPTION + 6 * MONTH
                ),
                OracleReading(
                    "broker_rotterdam", to_fixed("24980000"), INCEPTION + 6 * MONTH
                ),
                OracleReading(
                    "appraiser_second", to_fixed("2505000"), INCEPTION + 6 * MONTH
                ),
            ],
        ),
    ]


def rent_payments() -> list[tuple[int, int, str]]:
    """Monthly rent as (timestamp, amount, note).

    EUR 14,000 a month on a EUR 2.4M asset is a 7% gross yield, typical
    for Dutch industrial property.
    """
    return [
        (INCEPTION + 1 * MONTH, to_fixed("14000"), "January rent"),
        (INCEPTION + 2 * MONTH, to_fixed("14000"), "February rent"),
        (INCEPTION + 3 * MONTH, to_fixed("14000"), "March rent"),
        (INCEPTION + 4 * MONTH, to_fixed("14000"), "April rent"),
        (INCEPTION + 5 * MONTH, to_fixed("14350"), "May rent (indexed +2.5%)"),
        (INCEPTION + 6 * MONTH, to_fixed("14350"), "June rent"),
    ]


# -- chain seeding -----------------------------------------------------------


def seed_chain(chain: ChainClient) -> dict[str, int]:
    """Create the asset and distribute shares to the investor register.

    Returns a label -> minted amount map. Idempotent only in the sense
    that it will report failures rather than double-mint.
    """
    created = chain.create_asset(
        asset_id=ASSET_ID,
        metadata=asset_metadata(),
        min_balance=to_fixed("0.0001"),
    )
    if not created.ok and "already exists" not in (created.error or ""):
        raise RuntimeError(f"could not create asset: {created.error}")

    minted: dict[str, int] = {}
    for entry in holders():
        receipt = chain.mint(ASSET_ID, entry.holder.address, entry.shares)
        if not receipt.ok:
            raise RuntimeError(f"could not mint to {entry.label}: {receipt.error}")
        minted[entry.label] = entry.shares

    return minted


def holder_registry() -> dict[str, Holder]:
    """The investor register keyed by address, as the engine expects."""
    return {entry.holder.address: entry.holder for entry in holders()}


def label_for(address: str) -> str:
    """Human-readable name for an address, for display purposes."""
    for label, addr in INVESTORS.items():
        if addr == address:
            return label
    return f"{address[:6]}...{address[-4:]}"
