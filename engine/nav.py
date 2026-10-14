"""Net asset value computation.

NAV per share is what an investor's token is actually worth:

    nav_per_share = (asset_value + accrued_income - accrued_fees) / shares

Two deliberate design choices here.

First, the token does not rebase. Balances stay fixed and value accrues
in NAV instead. Rebasing tokens break wallets, explorers, and every
integration downstream, so real funds do not work that way either.

Second, fees accrue continuously on ACT/365 rather than being charged
in lumps. A holder who exits mid-month should pay for the days they
held, not the whole month.
"""

from __future__ import annotations

from .fixedpoint import accrue_pro_rata, apply_bps, div
from .models import AssetConfig, NavSnapshot


class NavEngine:
    """Tracks valuation, income, and fees for one asset over time."""

    def __init__(self, asset: AssetConfig, total_shares: int, inception: int):
        if total_shares < 0:
            raise ValueError("total_shares cannot be negative")
        self.asset = asset
        self.total_shares = total_shares
        self.asset_value = 0
        self.accrued_income = 0
        self.accrued_fees = 0
        self.last_accrual = inception

    # -- state transitions ---------------------------------------------------

    def set_valuation(self, value: int, now: int) -> None:
        """Record a new reconciled asset valuation.

        Fees are accrued up to `now` before the valuation changes, so the
        old value is charged for the period it was actually in effect.
        """
        self.accrue_fees(now)
        self.asset_value = value

    def add_income(self, amount: int, now: int) -> None:
        """Record income received by the asset, such as rent."""
        if amount < 0:
            raise ValueError("income cannot be negative")
        self.accrue_fees(now)
        self.accrued_income += amount

    def accrue_fees(self, now: int) -> int:
        """Accrue management fees up to `now`. Returns the amount added.

        Idempotent for a given timestamp: calling it twice at the same
        `now` accrues nothing the second time.
        """
        elapsed = now - self.last_accrual
        if elapsed <= 0:
            return 0

        annual_fee = apply_bps(self.gross_value, self.asset.management_fee_bps)
        fee = accrue_pro_rata(annual_fee, elapsed)

        self.accrued_fees += fee
        self.last_accrual = now
        return fee

    def issue_shares(self, shares: int) -> None:
        if shares < 0:
            raise ValueError("cannot issue negative shares")
        self.total_shares += shares

    def redeem_shares(self, shares: int) -> None:
        if shares < 0:
            raise ValueError("cannot redeem negative shares")
        if shares > self.total_shares:
            raise ValueError("cannot redeem more shares than exist")
        self.total_shares -= shares

    # -- derived values ------------------------------------------------------

    @property
    def gross_value(self) -> int:
        """Asset value plus income, before fees."""
        return self.asset_value + self.accrued_income

    @property
    def net_value(self) -> int:
        """What the fund is worth to holders, after fees."""
        return self.gross_value - self.accrued_fees

    @property
    def nav_per_share(self) -> int:
        """Value of a single share, scaled.

        With no shares issued there is nothing to divide, so this is zero
        rather than an error -- an asset can be configured before anyone
        subscribes.
        """
        if self.total_shares == 0:
            return 0
        return div(self.net_value, self.total_shares)

    def value_of(self, balance: int) -> int:
        """What a holder's balance is currently worth."""
        from .fixedpoint import mul

        return mul(balance, self.nav_per_share)

    def snapshot(self, now: int | None = None) -> NavSnapshot:
        """Current NAV as an immutable record for publishing on-chain."""
        epoch = self.last_accrual if now is None else now
        if now is not None:
            self.accrue_fees(now)
        return NavSnapshot(
            nav_per_share=self.nav_per_share,
            total_value=self.net_value,
            total_shares=self.total_shares,
            epoch=epoch,
            accrued_income=self.accrued_income,
            accrued_fees=self.accrued_fees,
        )
