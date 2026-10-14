"""Chain abstraction.

Everything the engine needs from a chain, expressed as one protocol.
Two implementations exist: an in-memory mock for development and tests,
and a Portaldot client that issues real extrinsics.

This boundary exists because Portaldot's V3 testnet was unreachable when
Parcel was built (the published RPC hostnames had no DNS records). Rather
than block, the whole system runs against the mock and swaps to the real
chain by changing one config value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AssetMetadata:
    name: str
    symbol: str
    decimals: int


@dataclass(frozen=True)
class AccountState:
    """An account's position in one asset, as the chain sees it."""

    balance: int
    frozen: bool


@dataclass(frozen=True)
class TxReceipt:
    """Result of submitting an extrinsic."""

    ok: bool
    tx_hash: str | None = None
    block: int | None = None
    error: str | None = None


class ChainError(Exception):
    """Raised when a chain operation fails irrecoverably."""


@runtime_checkable
class ChainClient(Protocol):
    """The operations Parcel needs from Portaldot's Assets pallet.

    Deliberately narrow. Parcel does not need generic chain access --
    it needs to create an asset, move units, and freeze accounts that
    fail compliance.
    """

    # -- reads ---------------------------------------------------------------

    def block_number(self) -> int:
        """Current best block height."""
        ...

    def now(self) -> int:
        """Chain time in unix seconds.

        Compliance decisions key off this rather than wall-clock time so
        that lock-up expiry is evaluated against the same clock the chain
        will enforce with.
        """
        ...

    def asset_metadata(self, asset_id: int) -> AssetMetadata | None:
        ...

    def total_supply(self, asset_id: int) -> int:
        ...

    def account_state(self, asset_id: int, address: str) -> AccountState:
        """Balance and frozen flag. Unknown accounts read as zero/unfrozen."""
        ...

    def holders(self, asset_id: int) -> dict[str, AccountState]:
        """Every account with a position in the asset."""
        ...

    # -- writes --------------------------------------------------------------

    def create_asset(
        self,
        asset_id: int,
        metadata: AssetMetadata,
        min_balance: int,
    ) -> TxReceipt:
        ...

    def mint(self, asset_id: int, to: str, amount: int) -> TxReceipt:
        ...

    def burn(self, asset_id: int, from_: str, amount: int) -> TxReceipt:
        ...

    def transfer(self, asset_id: int, from_: str, to: str, amount: int) -> TxReceipt:
        """Move units. Must fail if either party is frozen."""
        ...

    def freeze(self, asset_id: int, address: str) -> TxReceipt:
        """Block an account from transferring. This is how compliance
        decisions become enforceable rather than advisory."""
        ...

    def thaw(self, asset_id: int, address: str) -> TxReceipt:
        ...
