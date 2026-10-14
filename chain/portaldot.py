"""Portaldot client -- real extrinsics against a Substrate node.

Mirrors MockChain exactly, so the enforcer cannot tell them apart.
Every call maps to a pallet-assets extrinsic or storage query from the
official V3 guide.

This is written against the documented interface but has NOT been
executed against a live chain: Portaldot's published V3 testnet
hostnames had no DNS records during the build window. Behaviour is
specified by MockChain, which the test suite exercises thoroughly.
Expect to adjust storage key names and the timestamp pallet path on
first contact with a real node.
"""

from __future__ import annotations

from .interface import AccountState, AssetMetadata, ChainError, TxReceipt

try:  # pragma: no cover - import guard
    from substrateinterface import Keypair, SubstrateInterface
except ImportError:  # pragma: no cover
    SubstrateInterface = None  # type: ignore[assignment]
    Keypair = None  # type: ignore[assignment]


class PortaldotChain:
    """Talks to a Portaldot node over WebSocket.

    Parameters
    ----------
    url:
        Substrate RPC endpoint. For a local dev node this is
        ws://127.0.0.1:9944.
    signer_mnemonic:
        Seed phrase for the account submitting extrinsics. In dev mode
        the well-known //Alice account is pre-funded and sufficient.
    ss58_format:
        42 for generic Substrate, matching the official demos.
    """

    def __init__(
        self,
        url: str,
        signer_mnemonic: str | None = None,
        signer_uri: str | None = None,
        ss58_format: int = 42,
    ):
        if SubstrateInterface is None:
            raise ChainError(
                "substrate-interface is not installed; "
                "run: pip install substrate-interface"
            )

        self.url = url
        self.substrate = SubstrateInterface(url=url, ss58_format=ss58_format)

        if signer_mnemonic:
            self.signer = Keypair.create_from_mnemonic(signer_mnemonic)
        elif signer_uri:
            # Dev accounts: //Alice, //Bob, and so on.
            self.signer = Keypair.create_from_uri(signer_uri)
        else:
            self.signer = None

    # -- plumbing ------------------------------------------------------------

    def _submit(self, call_module: str, call_function: str, params: dict) -> TxReceipt:
        if self.signer is None:
            return TxReceipt(ok=False, error="no signer configured")

        try:
            call = self.substrate.compose_call(
                call_module=call_module,
                call_function=call_function,
                call_params=params,
            )
            extrinsic = self.substrate.create_signed_extrinsic(
                call=call, keypair=self.signer
            )
            receipt = self.substrate.submit_extrinsic(
                extrinsic, wait_for_inclusion=True
            )
        except Exception as exc:  # noqa: BLE001 - surface any node error
            return TxReceipt(ok=False, error=str(exc))

        if not receipt.is_success:
            return TxReceipt(
                ok=False,
                tx_hash=receipt.extrinsic_hash,
                error=str(receipt.error_message),
            )

        return TxReceipt(
            ok=True,
            tx_hash=receipt.extrinsic_hash,
            block=receipt.block_number,
        )

    def _query(self, module: str, storage: str, params: list | None = None):
        try:
            return self.substrate.query(
                module=module, storage_function=storage, params=params or []
            )
        except Exception as exc:  # noqa: BLE001
            raise ChainError(f"query {module}.{storage} failed: {exc}") from exc

    # -- reads ---------------------------------------------------------------

    def block_number(self) -> int:
        header = self.substrate.get_block_header()
        return int(header["header"]["number"])

    def now(self) -> int:
        """Chain time in seconds, from the timestamp pallet (stored in ms)."""
        result = self._query("Timestamp", "Now")
        return int(result.value) // 1000

    def asset_metadata(self, asset_id: int) -> AssetMetadata | None:
        result = self._query("Assets", "Metadata", [asset_id])
        if result is None or not result.value:
            return None
        value = result.value
        name = value.get("name", "")
        symbol = value.get("symbol", "")
        return AssetMetadata(
            name=_maybe_hex_to_str(name),
            symbol=_maybe_hex_to_str(symbol),
            decimals=int(value.get("decimals", 0)),
        )

    def total_supply(self, asset_id: int) -> int:
        result = self._query("Assets", "Asset", [asset_id])
        if result is None or not result.value:
            return 0
        return int(result.value.get("supply", 0))

    def account_state(self, asset_id: int, address: str) -> AccountState:
        result = self._query("Assets", "Account", [asset_id, address])
        if result is None or not result.value:
            return AccountState(balance=0, frozen=False)
        value = result.value
        return AccountState(
            balance=int(value.get("balance", 0)),
            frozen=_is_frozen(value),
        )

    def holders(self, asset_id: int) -> dict[str, AccountState]:
        """Enumerate every account holding the asset.

        Uses a storage map scan, which is acceptable at demo scale but
        would need an indexer for a production holder base.
        """
        try:
            entries = self.substrate.query_map("Assets", "Account", [asset_id])
        except Exception as exc:  # noqa: BLE001
            raise ChainError(f"holder enumeration failed: {exc}") from exc

        out: dict[str, AccountState] = {}
        for key, value in entries:
            address = str(key.value) if hasattr(key, "value") else str(key)
            data = value.value if hasattr(value, "value") else value
            out[address] = AccountState(
                balance=int(data.get("balance", 0)),
                frozen=_is_frozen(data),
            )
        return out

    # -- writes --------------------------------------------------------------

    def create_asset(
        self,
        asset_id: int,
        metadata: AssetMetadata,
        min_balance: int,
    ) -> TxReceipt:
        created = self._submit(
            "Assets",
            "create",
            {
                "id": asset_id,
                "admin": self._signer_address(),
                "min_balance": min_balance,
            },
        )
        if not created.ok:
            return created

        return self._submit(
            "Assets",
            "set_metadata",
            {
                "id": asset_id,
                "name": metadata.name,
                "symbol": metadata.symbol,
                "decimals": metadata.decimals,
            },
        )

    def mint(self, asset_id: int, to: str, amount: int) -> TxReceipt:
        return self._submit(
            "Assets",
            "mint",
            {"id": asset_id, "beneficiary": to, "amount": amount},
        )

    def burn(self, asset_id: int, from_: str, amount: int) -> TxReceipt:
        return self._submit(
            "Assets",
            "burn",
            {"id": asset_id, "who": from_, "amount": amount},
        )

    def transfer(self, asset_id: int, from_: str, to: str, amount: int) -> TxReceipt:
        """Transfer units.

        pallet-assets signs transfers as the sender, so `from_` must be
        the configured signer. Moving another account's units requires
        an approval and `transfer_approved`.
        """
        if from_ != self._signer_address():
            return TxReceipt(
                ok=False,
                error="signer must be the sending account for a direct transfer",
            )
        return self._submit(
            "Assets",
            "transfer",
            {"id": asset_id, "target": to, "amount": amount},
        )

    def freeze(self, asset_id: int, address: str) -> TxReceipt:
        return self._submit("Assets", "freeze", {"id": asset_id, "who": address})

    def thaw(self, asset_id: int, address: str) -> TxReceipt:
        return self._submit("Assets", "thaw", {"id": asset_id, "who": address})

    # -- helpers -------------------------------------------------------------

    def _signer_address(self) -> str:
        if self.signer is None:
            raise ChainError("no signer configured")
        return self.signer.ss58_address

    def close(self) -> None:
        try:
            self.substrate.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass


def _maybe_hex_to_str(value) -> str:
    """Substrate returns bounded byte strings as hex or raw bytes."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    text = str(value)
    if text.startswith("0x"):
        try:
            return bytes.fromhex(text[2:]).decode("utf-8", errors="replace")
        except ValueError:
            return text
    return text


def _is_frozen(account_data: dict) -> bool:
    """Read the frozen flag across pallet-assets versions.

    Older runtimes expose `is_frozen: bool`; newer ones encode it in a
    `status` enum of Liquid / Frozen / Blocked.
    """
    if "is_frozen" in account_data:
        return bool(account_data["is_frozen"])

    status = account_data.get("status")
    if status is None:
        return False
    if isinstance(status, str):
        return status.lower() in {"frozen", "blocked"}
    if isinstance(status, dict):
        return any(k.lower() in {"frozen", "blocked"} for k in status)
    return False
