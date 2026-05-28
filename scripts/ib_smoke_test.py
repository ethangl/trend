#!/usr/bin/env python3
"""Smoke test for trend.ib_broker.IBBroker against an IBKR PAPER account.

Sequence:
  1. Connect to IB Gateway (default port 4002) or TWS (7497) paper.
  2. Confirm the connected account looks like a paper account (DU/DF prefix).
  3. Qualify the MES front-month contract via ContFuture.
  4. Open an IBBroker around that contract.
  5. Place a 1-contract MES MARKET BUY. Wait for fill. Verify on_fill fires.
  6. Place a 1-contract MES MARKET SELL to flatten. Wait for fill.
  7. Verify final position is 0. Disconnect.

Run this with IB Gateway (or TWS) open against your paper account FIRST.
DO NOT POINT THIS AT YOUR LIVE ACCOUNT.

Defaults:
  host       127.0.0.1
  port       4002  (IB Gateway paper). Use 7497 if running TWS.
  client_id  11    (some distinctive value so it doesn't collide)

Usage:
    .venv/bin/python scripts/ib_smoke_test.py
    .venv/bin/python scripts/ib_smoke_test.py --port 7497   # if using TWS
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend.ib_broker import IBBroker  # noqa: E402
from trend.types import OrderType, Side  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002,
                   help="4002=IB Gateway paper; 7497=TWS paper")
    p.add_argument("--client-id", type=int, default=11)
    p.add_argument("--fill-timeout", type=float, default=30.0,
                   help="Seconds to wait for each fill")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt")
    args = p.parse_args()

    from ib_async import ContFuture, IB

    ib = IB()
    print(f"Connecting to {args.host}:{args.port} as clientId={args.client_id}…")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, readonly=False)
    except Exception as e:
        print(f"ERROR connecting: {e}", file=sys.stderr)
        print("Is IB Gateway (or TWS) running with the API enabled?", file=sys.stderr)
        return 1
    print(f"  connected (server v{ib.client.serverVersion()})")

    accounts = ib.managedAccounts()
    print(f"  managed accounts: {accounts}")
    if not any(a.startswith(("D", "DU", "DF")) for a in accounts):
        print("REFUSING TO RUN: account ID doesn't look like a paper account.",
              file=sys.stderr)
        ib.disconnect()
        return 1

    print("\nQualifying MES front-month contract…")
    cont = ContFuture(symbol="MES", exchange="CME", currency="USD")
    qualified = ib.qualifyContracts(cont)
    if not qualified or not qualified[0].conId:
        print("ERROR: could not qualify MES contract", file=sys.stderr)
        ib.disconnect()
        return 1
    contract = qualified[0]
    print(f"  contract: {contract.localSymbol} "
          f"(exp {contract.lastTradeDateOrContractMonth}, conId={contract.conId})")

    if not args.yes:
        ans = input("\nAbout to place: BUY 1 MES MARKET → wait for fill → SELL 1 MES MARKET. "
                    "Continue? [y/N] ").strip().lower()
        if ans not in {"y", "yes"}:
            print("Aborted.")
            ib.disconnect()
            return 0

    broker = IBBroker(ib, contract, point_value=5.0)
    fills_received = []
    broker.set_on_fill(lambda f: fills_received.append(f))

    # --- BUY ---
    print("\nPlacing BUY 1 MES MARKET…")
    buy_oid = broker.place_order(Side.LONG, 1, OrderType.MARKET, 0.0)
    print(f"  local order id: {buy_oid}")
    deadline = time.time() + args.fill_timeout
    while time.time() < deadline and len(fills_received) < 1:
        ib.sleep(0.25)
    if not fills_received:
        print(f"TIMEOUT: no buy fill within {args.fill_timeout}s. "
              "Position MAY still open — check IB Gateway.", file=sys.stderr)
        ib.disconnect()
        return 1
    buy_fill = fills_received[0]
    print(f"  filled: {buy_fill.qty} @ {buy_fill.price} ({buy_fill.side.name})")
    print(f"  broker.position(): {broker.position()}")
    assert broker.position_qty == 1, f"expected position 1, got {broker.position_qty}"

    # --- SELL ---
    print("\nPlacing SELL 1 MES MARKET (flatten)…")
    sell_oid = broker.place_order(Side.SHORT, 1, OrderType.MARKET, 0.0)
    print(f"  local order id: {sell_oid}")
    deadline = time.time() + args.fill_timeout
    while time.time() < deadline and len(fills_received) < 2:
        ib.sleep(0.25)
    if len(fills_received) < 2:
        print(f"TIMEOUT: no sell fill within {args.fill_timeout}s. "
              "POSITION MAY STILL BE OPEN — check IB Gateway.", file=sys.stderr)
        ib.disconnect()
        return 1
    sell_fill = fills_received[1]
    print(f"  filled: {sell_fill.qty} @ {sell_fill.price} ({sell_fill.side.name})")
    print(f"  broker.position(): {broker.position()}")
    print(f"  round-trip realized: ${broker.total_realized:.2f}")

    if broker.position_qty != 0:
        print(f"ERROR: broker thinks position is {broker.position_qty}, expected 0",
              file=sys.stderr)
        ib.disconnect()
        return 1

    print("\nSmoke test PASSED ✅")
    print(f"  fills tracked: {len(broker.fills)}")
    print(f"  total realized P&L: ${broker.total_realized:.2f} "
          f"(buy_slip + sell_slip + 2x commission)")
    ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
