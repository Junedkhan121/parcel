import pytest

from chain.interface import AssetMetadata
from chain.mock import MockChain
from engine.fixedpoint import to_fixed

ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"

ASSET = 42
META = AssetMetadata(name="Warehouse Unit 7, Rotterdam", symbol="WH7", decimals=14)


@pytest.fixture
def chain():
    c = MockChain()
    c.create_asset(ASSET, META, min_balance=to_fixed("0.0001"))
    return c


class TestAssetCreation:
    def test_create_and_read_metadata(self, chain):
        meta = chain.asset_metadata(ASSET)
        assert meta is not None
        assert meta.symbol == "WH7"
        assert meta.decimals == 14

    def test_unknown_asset_has_no_metadata(self, chain):
        assert chain.asset_metadata(999) is None

    def test_duplicate_asset_rejected(self, chain):
        receipt = chain.create_asset(ASSET, META, min_balance=0)
        assert not receipt.ok
        assert "already exists" in receipt.error

    def test_operations_on_unknown_asset_fail(self, chain):
        assert not chain.mint(999, ALICE, to_fixed("1")).ok
        assert not chain.transfer(999, ALICE, BOB, to_fixed("1")).ok
        assert not chain.freeze(999, ALICE).ok


class TestMinting:
    def test_mint_increases_balance_and_supply(self, chain):
        receipt = chain.mint(ASSET, ALICE, to_fixed("100"))
        assert receipt.ok
        assert receipt.tx_hash is not None
        assert chain.account_state(ASSET, ALICE).balance == to_fixed("100")
        assert chain.total_supply(ASSET) == to_fixed("100")

    def test_mint_accumulates(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.mint(ASSET, ALICE, to_fixed("50"))
        assert chain.account_state(ASSET, ALICE).balance == to_fixed("150")

    def test_mint_to_frozen_account_rejected(self, chain):
        chain.freeze(ASSET, ALICE)
        receipt = chain.mint(ASSET, ALICE, to_fixed("100"))
        assert not receipt.ok
        assert "frozen" in receipt.error

    def test_non_positive_mint_rejected(self, chain):
        assert not chain.mint(ASSET, ALICE, 0).ok
        assert not chain.mint(ASSET, ALICE, -1).ok


class TestTransfers:
    def test_transfer_moves_value(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        assert chain.transfer(ASSET, ALICE, BOB, to_fixed("30")).ok
        assert chain.account_state(ASSET, ALICE).balance == to_fixed("70")
        assert chain.account_state(ASSET, BOB).balance == to_fixed("30")

    def test_supply_conserved(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        before = chain.total_supply(ASSET)
        chain.transfer(ASSET, ALICE, BOB, to_fixed("30"))
        assert chain.total_supply(ASSET) == before

    def test_insufficient_balance_rejected(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("10"))
        receipt = chain.transfer(ASSET, ALICE, BOB, to_fixed("50"))
        assert not receipt.ok
        assert "insufficient" in receipt.error

    def test_frozen_sender_blocked(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.freeze(ASSET, ALICE)
        receipt = chain.transfer(ASSET, ALICE, BOB, to_fixed("10"))
        assert not receipt.ok
        assert ALICE in receipt.error

    def test_frozen_recipient_blocked(self, chain):
        # The receiving half is what makes compliance enforcement bite:
        # an ineligible account must not be able to acquire the asset.
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.freeze(ASSET, BOB)
        receipt = chain.transfer(ASSET, ALICE, BOB, to_fixed("10"))
        assert not receipt.ok
        assert BOB in receipt.error

    def test_dust_is_reaped(self, chain):
        # Leaving a balance below the minimum would strand unusable dust.
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.transfer(ASSET, ALICE, BOB, to_fixed("100") - 1)
        assert chain.account_state(ASSET, ALICE).balance == 0

    def test_non_positive_transfer_rejected(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        assert not chain.transfer(ASSET, ALICE, BOB, 0).ok


class TestBurning:
    def test_burn_reduces_supply(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        assert chain.burn(ASSET, ALICE, to_fixed("40")).ok
        assert chain.account_state(ASSET, ALICE).balance == to_fixed("60")
        assert chain.total_supply(ASSET) == to_fixed("60")

    def test_cannot_burn_more_than_held(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("10"))
        assert not chain.burn(ASSET, ALICE, to_fixed("50")).ok


class TestFreezing:
    def test_freeze_and_thaw(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        assert not chain.account_state(ASSET, ALICE).frozen

        chain.freeze(ASSET, ALICE)
        assert chain.account_state(ASSET, ALICE).frozen

        chain.thaw(ASSET, ALICE)
        assert not chain.account_state(ASSET, ALICE).frozen

    def test_freeze_is_idempotent(self, chain):
        chain.freeze(ASSET, ALICE)
        assert chain.freeze(ASSET, ALICE).ok
        assert chain.account_state(ASSET, ALICE).frozen

    def test_thaw_unfrozen_account_is_safe(self, chain):
        assert chain.thaw(ASSET, ALICE).ok

    def test_freeze_preserves_balance(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.freeze(ASSET, ALICE)
        # Freezing blocks movement; it does not confiscate.
        assert chain.account_state(ASSET, ALICE).balance == to_fixed("100")

    def test_transfer_works_again_after_thaw(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.freeze(ASSET, ALICE)
        assert not chain.transfer(ASSET, ALICE, BOB, to_fixed("10")).ok
        chain.thaw(ASSET, ALICE)
        assert chain.transfer(ASSET, ALICE, BOB, to_fixed("10")).ok


class TestHolderEnumeration:
    def test_lists_all_holders(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.mint(ASSET, BOB, to_fixed("50"))
        holders = chain.holders(ASSET)
        assert set(holders) == {ALICE, BOB}
        assert holders[ALICE].balance == to_fixed("100")

    def test_includes_frozen_zero_balance_accounts(self, chain):
        # A frozen account with no balance still matters: it is a
        # compliance decision the chain is holding.
        chain.freeze(ASSET, ALICE)
        assert ALICE in chain.holders(ASSET)

    def test_unknown_asset_has_no_holders(self, chain):
        assert chain.holders(999) == {}


class TestClock:
    def test_time_advances(self, chain):
        start = chain.now()
        chain.advance(3600)
        assert chain.now() == start + 3600

    def test_blocks_advance_with_time(self, chain):
        start = chain.block_number()
        chain.advance(60)
        assert chain.block_number() == start + 10  # 6s blocks

    def test_explicit_block_count(self, chain):
        start = chain.block_number()
        chain.advance(60, blocks=1)
        assert chain.block_number() == start + 1


class TestExtrinsicLog:
    def test_records_submitted_calls(self, chain):
        chain.mint(ASSET, ALICE, to_fixed("100"))
        chain.freeze(ASSET, ALICE)
        names = [name for name, _ in chain.extrinsics]
        assert "Assets.create" in names
        assert "Assets.mint" in names
        assert "Assets.freeze" in names

    def test_failed_calls_are_not_logged(self, chain):
        before = len(chain.extrinsics)
        chain.mint(999, ALICE, to_fixed("1"))
        assert len(chain.extrinsics) == before
