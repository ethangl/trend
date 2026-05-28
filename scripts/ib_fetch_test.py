#!/usr/bin/env python3
"""Live smoke test for trend.ib_data.fetch_daily_bars.

Connects to IB Gateway (or TWS) paper, qualifies a handful of front-month
futures via ContFuture, and fetches the trailing daily bars for each.
Prints what came back so we can eyeball the data before wiring this into
the live runner.

Run with IB Gateway open against your paper account (port 4002).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend.ib_data import fetch_daily_bars, latest_completed_bar  # noqa: E402

ET = ZoneInfo("America/New_York")

# (symbol, exchange) — micros where available, full-size elsewhere.
DEFAULT_PROBES = [
    ("MES", "CME"),
    ("MNQ", "CME"),
    ("MGC", "COMEX"),
    ("MCL", "NYMEX"),
    ("ZN",  "CBOT"),
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002,
                   help="4002=IB Gateway paper; 7497=TWS paper")
    p.add_argument("--client-id", type=int, default=12)
    p.add_argument("--n-bars", type=int, default=5)
    p.add_argument("--symbols", nargs="*",
                   help="Override the default probe list (space-separated symbols). "
                        "Exchange is looked up from the default table.")
    args = p.parse_args()

    from ib_async import ContFuture, IB

    probes = DEFAULT_PROBES
    if args.symbols:
        exch = {s: e for s, e in DEFAULT_PROBES}
        probes = [(s, exch.get(s, "CME")) for s in args.symbols]

    ib = IB()
    print(f"Connecting to {args.host}:{args.port} as clientId={args.client_id}…")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, readonly=True)
    except Exception as e:
        print(f"ERROR connecting: {e}", file=sys.stderr)
        return 1
    print(f"  connected (server v{ib.client.serverVersion()})")

    today_et = datetime.now(ET).date()
    print(f"  today (ET): {today_et}\n")

    rc = 0
    for symbol, exchange in probes:
        print(f"=== {symbol} ({exchange}) ===")
        cont = ContFuture(symbol=symbol, exchange=exchange, currency="USD")
        qualified = ib.qualifyContracts(cont)
        if not qualified or not qualified[0].conId:
            print(f"  could not qualify {symbol}")
            rc = 1
            continue
        contract = qualified[0]
        print(f"  contract: {contract.localSymbol} "
              f"(exp {contract.lastTradeDateOrContractMonth})")

        try:
            bars = fetch_daily_bars(ib, contract, n_bars=args.n_bars)
        except Exception as e:
            print(f"  fetch failed: {e}")
            rc = 1
            continue

        if not bars:
            print("  EMPTY result")
            rc = 1
            continue

        for b in bars:
            print(f"  {b.ts.date().isoformat()}  "
                  f"O={b.open:>10.4f}  H={b.high:>10.4f}  "
                  f"L={b.low:>10.4f}  C={b.close:>10.4f}  V={b.volume}")

        latest = latest_completed_bar(bars, today=today_et)
        if latest is None:
            print("  no completed bar found before today")
        else:
            print(f"  → latest completed: {latest.ts.date()} C={latest.close}")
        print()

    ib.disconnect()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
