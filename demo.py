"""Run the Warehouse Unit 7 scenario end to end.

Replays six months of the asset's life -- valuations, rent, compliance
enforcement -- and prints what happened at each step. This is the demo
script, and it doubles as an integration test.

    python demo.py              # against the in-memory mock
    PARCEL_BACKEND=local python demo.py   # against a dev node
"""

from __future__ import annotations

import sys

import scenario
from config import load
from engine.fixedpoint import format_fixed
from engine.models import REASON_TEXT
from service import ParcelService


def rule(char: str = "-", width: int = 72) -> None:
    print(char * width)


def heading(text: str) -> None:
    print()
    rule("=")
    print(text)
    rule("=")


def money(value: int) -> str:
    return f"EUR {format_fixed(value)}"


def main() -> int:
    settings = load()

    heading("PARCEL -- Warehouse Unit 7, Rotterdam")
    print(f"backend:  {settings.profile.name}")
    if settings.profile.rpc:
        print(f"rpc:      {settings.profile.rpc}")
    print(f"asset id: {scenario.ASSET_ID}")

    chain = _connect(settings)
    if chain is None:
        return 1

    # -- set up the asset and distribute shares ----------------------------

    heading("1. Issue the asset")
    minted = scenario.seed_chain(chain)
    for label, amount in minted.items():
        print(f"  {label:<9} {format_fixed(amount, 0):>8} shares")
    print(f"\n  total supply: {format_fixed(chain.total_supply(scenario.ASSET_ID), 0)} shares")

    rules = scenario.rules()
    print("\n  transfer restrictions:")
    print(f"    jurisdictions:   {', '.join(sorted(rules.allowed_jurisdictions))}")
    print(f"    accredited only: {rules.require_accreditation}")
    print(f"    max holders:     {rules.max_holders}")
    print(f"    concentration:   {rules.max_holder_bps / 100:.1f}% max per holder")

    service = ParcelService(
        chain=chain,
        asset=scenario.asset_config(),
        holders=scenario.holder_registry(),
        total_shares=scenario.TOTAL_SHARES,
        inception=scenario.INCEPTION,
    )

    # -- compliance ---------------------------------------------------------

    heading("2. Enforce compliance on-chain")
    result = service.enforce(scenario.INCEPTION)

    if result.plan.is_empty:
        print("  nothing to do -- all holders compliant")
    else:
        for address, reason in result.plan.to_freeze.items():
            label = scenario.label_for(address)
            print(f"  FREEZE  {label:<9} {REASON_TEXT[reason]}")
        for address in result.plan.to_thaw:
            print(f"  THAW    {scenario.label_for(address):<9} now compliant")

    if result.failures:
        print("\n  failures:")
        for address, error in result.failures.items():
            print(f"    {scenario.label_for(address)}: {error}")

    print(f"\n  extrinsics submitted: {result.applied}")

    # -- prove enforcement bites -------------------------------------------

    heading("3. A blocked transfer")
    dave = scenario.INVESTORS["Dave"]
    alice = scenario.INVESTORS["Alice"]
    amount = service.nav.total_shares // 100  # 1% of the issue

    print(f"  Dave attempts to send {format_fixed(amount, 0)} shares to Alice")
    verdict, chain_error = service.attempt_transfer(
        dave, alice, amount, scenario.INCEPTION
    )
    print(f"  compliance: {'ALLOW' if verdict.allowed else 'DENY'} -- {verdict.text}")
    print(f"  reason code: 0x{verdict.reason.value:02x}")

    state = chain.account_state(scenario.ASSET_ID, dave)
    print(f"  on-chain frozen flag for Dave: {state.frozen}")
    print("\n  the chain is enforcing this, not the frontend --")
    print("  Assets::freeze was called, so pallet-assets rejects the transfer")

    # -- a permitted transfer ----------------------------------------------
    print()
    bob = scenario.INVESTORS["Bob"]
    print(f"  Alice attempts to send {format_fixed(amount, 0)} shares to Bob")
    verdict, chain_error = service.attempt_transfer(
        alice, bob, amount, scenario.INCEPTION
    )
    print(f"  compliance: {'ALLOW' if verdict.allowed else 'DENY'} -- {verdict.text}")
    if chain_error:
        print(f"  chain error: {chain_error}")
    else:
        print("  transfer settled")

    # -- six months of operation -------------------------------------------

    heading("4. Six months of valuations and rent")

    rounds = {ts: readings for ts, readings in scenario.valuation_rounds()}
    payments = {ts: (amount, note) for ts, amount, note in scenario.rent_payments()}
    timeline = sorted(set(rounds) | set(payments))

    for ts in timeline:
        month = (ts - scenario.INCEPTION) // scenario.MONTH
        print(f"\n  month {month}:")

        if ts in rounds:
            event = service.submit_valuation(rounds[ts], ts)
            recon = event.reconciliation

            for reading in recon.accepted:
                print(f"    feed  {reading.source:<22} {money(reading.value)}")
            for rejected in recon.rejected:
                print(
                    f"    FEED REJECTED  {rejected.reading.source:<13} "
                    f"{money(rejected.reading.value)}  [{rejected.reason.value}]"
                )

            if event.applied and recon.value is not None:
                print(f"    -> valuation accepted: {money(recon.value)}")
            else:
                breaker = recon.breaker.value if recon.breaker else "unknown"
                print(f"    -> DECLINED to publish: {breaker}")

        if ts in payments:
            amount, note = payments[ts]
            service.record_income(amount, ts, note)
            print(f"    income  {note:<28} {money(amount)}")

        print(f"    NAV/share: {money(service.nav.nav_per_share)}")

    # -- income distribution -----------------------------------------------

    heading("5. Distribute accrued income")
    final_ts = timeline[-1]

    nav_before = service.nav.nav_per_share
    available = service.nav.distributable
    to_distribute = available // 2

    print(f"  income earned, net of fees: {money(available)}")
    print(f"  paying out half:            {money(to_distribute)}")
    print("  (ineligible holders are excluded; their share is retained)\n")

    distributed = service.distribute_income(to_distribute, final_ts)
    for address, share in sorted(distributed.items(), key=lambda kv: -kv[1]):
        print(f"    {scenario.label_for(address):<9} {money(share):>18}")

    skipped = set(service.engine.holders) - set(distributed)
    for address in sorted(skipped):
        label = scenario.label_for(address)
        verdict = service.engine.check_holder(address, final_ts)
        if not verdict.allowed:
            print(f"    {label:<9} {'withheld':>18}  -- {verdict.text}")

    if service.distributions:
        event = service.distributions[-1]
        print(f"\n  total paid out: {money(event.total_paid)}")
        if event.withheld:
            print(f"  retained for ineligible holders: {money(event.withheld)}")

    print(f"\n  NAV/share before: {money(nav_before)}")
    print(f"  NAV/share after:  {money(service.nav.nav_per_share)}")
    print("  NAV falls because value left the vehicle -- that is what")
    print("  makes this a distribution rather than a stock split.")

    # -- final state --------------------------------------------------------

    heading("6. Final state")
    summary = service.summary(final_ts)

    print(f"  asset value:     {money(summary['asset_value'])}")
    print(f"  accrued income:  {money(summary['accrued_income'])}")
    print(f"  accrued fees:    {money(summary['accrued_fees'])}")
    print(f"  distributed:     {money(summary['distributed'])}")
    print(f"  net asset value: {money(summary['total_value'])}")
    print(f"  shares:          {format_fixed(summary['total_shares'], 4)}")
    print(f"  NAV per share:   {money(summary['nav_per_share'])}")
    print()
    print(f"  holders:         {summary['holder_count']}")
    print(f"  frozen:          {summary['frozen_count']}")
    print(f"  drift:           {len(summary['drift'])} (should be 0)")

    print()
    rule()
    print(f"  {'holder':<9} {'juris':<6} {'shares':>12} {'value':>16}  status")
    rule()
    for view in service.holder_views(final_ts):
        status = "frozen" if view.frozen_on_chain else "ok"
        if not view.enforced_correctly:
            status += " !! DRIFT"
        print(
            f"  {view.label:<9} {view.jurisdiction:<6} "
            f"{format_fixed(view.balance, 2):>12} "
            f"{money(view.value):>16}  {status}"
        )
    rule()

    rejected = service.rejected_readings()
    if rejected:
        print(f"\n  oracle readings rejected over the period: {len(rejected)}")
        for ts, source, reason in rejected:
            month = (ts - scenario.INCEPTION) // scenario.MONTH
            print(f"    month {month}: {source} ({reason})")

    print()
    return 0


def _connect(settings):
    """Build the chain client, explaining clearly if it is unreachable."""
    from config import build_chain

    try:
        return build_chain(settings)
    except Exception as exc:  # noqa: BLE001 - this is the user-facing boundary
        print(f"\ncould not reach {settings.profile.name}: {exc}", file=sys.stderr)
        if not settings.is_mock:
            print(
                "\nTo run without a node:\n"
                "    PARCEL_BACKEND=mock python demo.py\n"
                "\nTo run a local dev node:\n"
                "    ./portaldot_dev --dev --tmp\n"
                "    PARCEL_BACKEND=local python demo.py",
                file=sys.stderr,
            )
        return None


if __name__ == "__main__":
    sys.exit(main())
