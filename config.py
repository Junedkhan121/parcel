"""Configuration.

Every chain-specific value lives here and nowhere else. Switching from
the mock to a local dev node to Portaldot testnet is one environment
variable, not a code change.

This matters because Portaldot's published V3 testnet hostnames had no
DNS records during the build window. Parcel was developed entirely
against the mock and the local dev node, which is only painless because
the chain choice was never baked into the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class Backend(str, Enum):
    MOCK = "mock"
    LOCAL = "local"
    TESTNET = "testnet"


@dataclass(frozen=True)
class ChainProfile:
    """Everything needed to reach one chain."""

    name: str
    rpc: str | None
    ss58_format: int = 42
    token_symbol: str = "tPOTv3"
    token_decimals: int = 14
    explorer: str | None = None


# Values below come from the official PortaldotV3 testnet guide.
# The testnet hostnames did not resolve during the build window --
# verify them before relying on the testnet profile.
PROFILES: dict[Backend, ChainProfile] = {
    Backend.MOCK: ChainProfile(
        name="Mock (in-memory)",
        rpc=None,
    ),
    Backend.LOCAL: ChainProfile(
        name="Portaldot dev node",
        rpc="ws://127.0.0.1:9944",
    ),
    Backend.TESTNET: ChainProfile(
        name="PortaldotV3 Testnet",
        rpc="wss://testnetv3-node.feso-apps.xyz",
        explorer="https://node-console.feso-apps.xyz/",
    ),
}

# EVM-side values from the guide. Parcel does not use the EVM, but a
# hybrid extension would need these.
EVM_RPC = "https://testnetv3-eth-rpc.feso-apps.xyz"
EVM_CHAIN_ID = 420_420_777


@dataclass(frozen=True)
class Settings:
    backend: Backend
    profile: ChainProfile
    asset_id: int
    signer_uri: str | None
    signer_mnemonic: str | None

    @property
    def is_mock(self) -> bool:
        return self.backend is Backend.MOCK


def load() -> Settings:
    """Read settings from the environment.

    PARCEL_BACKEND   mock | local | testnet   (default: mock)
    PARCEL_ASSET_ID  integer asset id          (default: 42)
    PARCEL_SIGNER_URI       dev account, e.g. //Alice (default on local)
    PARCEL_SIGNER_MNEMONIC  seed phrase for a real account
    """
    raw_backend = os.getenv("PARCEL_BACKEND", Backend.MOCK.value).lower()
    try:
        backend = Backend(raw_backend)
    except ValueError:
        valid = ", ".join(b.value for b in Backend)
        raise ValueError(
            f"PARCEL_BACKEND must be one of: {valid} (got {raw_backend!r})"
        ) from None

    signer_uri = os.getenv("PARCEL_SIGNER_URI")
    signer_mnemonic = os.getenv("PARCEL_SIGNER_MNEMONIC")

    # A dev node ships with well-known pre-funded accounts, so default
    # to //Alice rather than making the developer supply a seed phrase.
    if backend is Backend.LOCAL and not (signer_uri or signer_mnemonic):
        signer_uri = "//Alice"

    return Settings(
        backend=backend,
        profile=PROFILES[backend],
        asset_id=int(os.getenv("PARCEL_ASSET_ID", "42")),
        signer_uri=signer_uri,
        signer_mnemonic=signer_mnemonic,
    )


def build_chain(settings: Settings | None = None):
    """Construct the chain client for the configured backend."""
    settings = settings or load()

    if settings.is_mock:
        from chain.mock import MockChain

        return MockChain()

    from chain.portaldot import PortaldotChain

    if settings.profile.rpc is None:
        raise ValueError(f"no RPC configured for backend {settings.backend.value}")

    return PortaldotChain(
        url=settings.profile.rpc,
        signer_mnemonic=settings.signer_mnemonic,
        signer_uri=settings.signer_uri,
        ss58_format=settings.profile.ss58_format,
    )
