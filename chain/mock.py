"""In-memory chain for development and tests.

Models the parts of Substrate's Assets pallet that Parcel relies on,
including the behaviours that are easy to forget:

- a frozen account cannot send *or* receive
- transfers that would drop a balance below the asset's minimum
  reap the dust rather than leaving an unusable remainder
- minting to a frozen account is refused

Getting these right here means the real client has a specification to
match, and the demo behaves identically with or without a live node.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .interface import AccountState, AssetMetadata, TxReceipt


@dataclass
class _Asset:
    metadata: AssetMetadata
    min_balance: int
    balances: dict[str, int] = field(default_factory=dict)
    frozen: set[str] = field(default_factory=set)


class MockChain:
    """A deterministic, in-memory stand-in for Portaldot.

    Time is explicit: call `advance(seconds)` to move the clock. Nothing
    depends on wall-clock time, so tests and demos are reproducible.
    """

    def __init__(self, start_time: int = 1_760_000_000, start_block: int = 1):
        self._assets: dict[int, _Asset] = {}
        self._time = start_time
        self._block = start_block
        self._tx_count = 0
        self.extrinsics: list[tuple[str, tuple]] = []

    # -- test controls -------------------------------------------------------

    def advance(self, seconds: int, blocks: int | None = None) -> None:
        """Move the clock forward. Defaults to one block per 6 seconds,
        matching typical Substrate block time."""
        self._time += seconds
        self._block += blocks if blocks is not None else max(1, seconds // 6)

    def _receipt(self, name: str, args: tuple) -> TxReceipt:
        self._tx_count += 1
        self.extrinsics.append((name, args))
        digest = hashlib.sha256(f"{name}{args}{self._tx_count}".encode()).hexdigest()
        return TxReceipt(ok=True, tx_hash=f"0x{digest[:64]}", block=self._block)

    @staticmethod
    def _fail(error: str) -> TxReceipt:
        return TxReceipt(ok=False, error=error)

    # -- reads ---------------------------------------------------------------

    def block_number(self) -> int:
        return self._block

    def now(self) -> int:
        return self._time

    def asset_metadata(self, asset_id: int) -> AssetMetadata | None:
        asset = self._assets.get(asset_id)
        return asset.metadata if asset else None

    def total_supply(self, asset_id: int) -> int:
        asset = self._assets.get(asset_id)
        if asset is None:
            return 0
        return sum(asset.balances.values())

    def account_state(self, asset_id: int, address: str) -> AccountState:
        asset = self._assets.get(asset_id)
        if asset is None:
            return AccountState(balance=0, frozen=False)
        return AccountState(
            balance=asset.balances.get(address, 0),
            frozen=address in asset.frozen,
        )

    def holders(self, asset_id: int) -> dict[str, AccountState]:
        asset = self._assets.get(asset_id)
        if asset is None:
            return {}
        addresses = set(asset.balances) | asset.frozen
        return {addr: self.account_state(asset_id, addr) for addr in addresses}

    # -- writes --------------------------------------------------------------

    def create_asset(
        self,
        asset_id: int,
        metadata: AssetMetadata,
        min_balance: int,
    ) -> TxReceipt:
        if asset_id in self._assets:
            return self._fail(f"asset {asset_id} already exists")
        if min_balance < 0:
            return self._fail("min_balance cannot be negative")
        self._assets[asset_id] = _Asset(metadata=metadata, min_balance=min_balance)
        return self._receipt("Assets.create", (asset_id, metadata.symbol))

    def mint(self, asset_id: int, to: str, amount: int) -> TxReceipt:
        asset = self._assets.get(asset_id)
        if asset is None:
            return self._fail(f"unknown asset {asset_id}")
        if amount <= 0:
            return self._fail("mint amount must be positive")
        if to in asset.frozen:
            return self._fail(f"account {to} is frozen")

        new_balance = asset.balances.get(to, 0) + amount
        if new_balance < asset.min_balance:
            return self._fail("resulting balance below asset minimum")

        asset.balances[to] = new_balance
        return self._receipt("Assets.mint", (asset_id, to, amount))

    def burn(self, asset_id: int, from_: str, amount: int) -> TxReceipt:
        asset = self._assets.get(asset_id)
        if asset is None:
            return self._fail(f"unknown asset {asset_id}")
        if amount <= 0:
            return self._fail("burn amount must be positive")

        balance = asset.balances.get(from_, 0)
        if balance < amount:
            return self._fail("insufficient balance")

        remaining = balance - amount
        if remaining < asset.min_balance:
            # Dust is reaped rather than left stranded.
            asset.balances.pop(from_, None)
        else:
            asset.balances[from_] = remaining
        return self._receipt("Assets.burn", (asset_id, from_, amount))

    def transfer(self, asset_id: int, from_: str, to: str, amount: int) -> TxReceipt:
        asset = self._assets.get(asset_id)
        if asset is None:
            return self._fail(f"unknown asset {asset_id}")
        if amount <= 0:
            return self._fail("transfer amount must be positive")

        # Frozen accounts can neither send nor receive. The receiving
        # half is what makes compliance enforcement actually bite.
        if from_ in asset.frozen:
            return self._fail(f"account {from_} is frozen")
        if to in asset.frozen:
            return self._fail(f"account {to} is frozen")

        balance = asset.balances.get(from_, 0)
        if balance < amount:
            return self._fail("insufficient balance")

        recipient_balance = asset.balances.get(to, 0) + amount
        if recipient_balance < asset.min_balance:
            return self._fail("resulting balance below asset minimum")

        remaining = balance - amount
        if remaining < asset.min_balance:
            asset.balances.pop(from_, None)
        else:
            asset.balances[from_] = remaining
        asset.balances[to] = recipient_balance

        return self._receipt("Assets.transfer", (asset_id, from_, to, amount))

    def freeze(self, asset_id: int, address: str) -> TxReceipt:
        asset = self._assets.get(asset_id)
        if asset is None:
            return self._fail(f"unknown asset {asset_id}")
        asset.frozen.add(address)
        return self._receipt("Assets.freeze", (asset_id, address))

    def thaw(self, asset_id: int, address: str) -> TxReceipt:
        asset = self._assets.get(asset_id)
        if asset is None:
            return self._fail(f"unknown asset {asset_id}")
        asset.frozen.discard(address)
        return self._receipt("Assets.thaw", (asset_id, address))
