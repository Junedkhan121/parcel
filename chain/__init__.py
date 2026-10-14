"""Chain adapters for Parcel.

`ChainClient` is the protocol. `MockChain` runs in memory for
development and tests; `PortaldotChain` issues real extrinsics.
"""

from .interface import (
    AccountState,
    AssetMetadata,
    ChainClient,
    ChainError,
    TxReceipt,
)
from .mock import MockChain

__all__ = [
    "AccountState",
    "AssetMetadata",
    "ChainClient",
    "ChainError",
    "MockChain",
    "TxReceipt",
    "PortaldotChain",
]


def __getattr__(name: str):
    """Import PortaldotChain lazily so the mock works without
    substrate-interface installed."""
    if name == "PortaldotChain":
        from .portaldot import PortaldotChain

        return PortaldotChain
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
