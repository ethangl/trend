#!/usr/bin/env python3
"""Diagnose why M2K (Russell 2000) is the big equity-index loser.

Runs each strategy on MES, MNQ, M2K with bars restricted to RTY's available
window (2017-07 onward), so all three are evaluated on the same period.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.all_strategies_backtest import (  # noqa: E402
    Cell, STRATEGIES, run_cell, stats,
)
from trend.data import load_bars_csv  # noqa: E402

EQUITY_MARKETS = [
    ("MES", "data/es_1d.csv", 5.0, 0.62),
    ("MNQ", "data/nq_1d.csv", 2.0, 0.62),
    ("M2K", "data/rty_1d.csv", 5.0, 0.62),
]

# RTY's first bar is 2017-07-09. Restrict everything to that window.
COMMON_START = date(2017, 7, 9)


def main() -> int:
    print(f"Restricting all markets to bars on/after {COMMON_START}\n")

    cells: list[Cell] = []
    for sym, path, pv, comm in EQUITY_MARKETS:
        bars = [b for b in load_bars_csv(path) if b.ts.date() >= COMMON_START]
        print(f"{sym}: {len(bars)} bars from {bars[0].ts.date()} to {bars[-1].ts.date()}")
        for strat_name in STRATEGIES:
            cells.append(run_cell(strat_name, sym, bars, pv, comm, 0.001, 300_000.0))

    # Per (strategy × market) — apples-to-apples
    print(f"\n{'='*82}")
    print(f"Strategy × Market on common 2017-07 → 2026-05 window")
    print(f"{'='*82}")
    print(f"{'strategy':<14}{'symbol':<6}{'trades':>8}{'total':>12}"
          f"{'sharpe':>8}{'max_dd':>10}")
    print("-" * 82)
    for c in cells:
        s = stats(c.daily_pnl)
        print(f"{c.strategy:<14}{c.symbol:<6}{c.n_trades:>8}{s['total']:>12,.0f}"
              f"{s['sharpe']:>8.2f}{s['max_dd']:>10,.0f}")

    # Per market totals
    print(f"\n{'='*82}")
    print(f"Per-market aggregation (across 3 strategies) on common window")
    print(f"{'='*82}")
    for sym in [m[0] for m in EQUITY_MARKETS]:
        mkt_cells = [c for c in cells if c.symbol == sym]
        union = sorted(set().union(*(c.daily_pnl.keys() for c in mkt_cells)))
        merged = {d: sum(c.daily_pnl.get(d, 0.0) for c in mkt_cells) for d in union}
        s = stats(merged)
        print(f"{sym}: total ${s['total']:>+10,.0f}  Sharpe {s['sharpe']:>+5.2f}  "
              f"max_dd ${s['max_dd']:>+10,.0f}")

    # Yearly per-market
    print(f"\n{'='*82}")
    print(f"Yearly P&L per market (sum of all 3 strategies)")
    print(f"{'='*82}")
    yearly: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for c in cells:
        for d, p in c.daily_pnl.items():
            yearly[d.year][c.symbol] += p
    syms = [m[0] for m in EQUITY_MARKETS]
    print(f"{'year':<6}" + "".join(f"{s:>14}" for s in syms))
    for y in sorted(yearly):
        row = f"{y:<6}"
        for s in syms:
            row += f"{yearly[y][s]:>+14,.0f}"
        print(row)

    # Price-trajectory comparison: how much do the indexes move together?
    print(f"\n{'='*82}")
    print(f"Index endpoints (just so we see the price action)")
    print(f"{'='*82}")
    for sym, path, _, _ in EQUITY_MARKETS:
        bars = [b for b in load_bars_csv(path) if b.ts.date() >= COMMON_START]
        first, last = bars[0].close, bars[-1].close
        peak = max(b.close for b in bars)
        trough = min(b.close for b in bars)
        peak_idx = max(range(len(bars)), key=lambda i: bars[i].close)
        trough_idx = min(range(len(bars)), key=lambda i: bars[i].close)
        total_ret = (last / first - 1) * 100
        max_pct_dd = (1 - trough / peak) * 100 if trough_idx > peak_idx else None
        print(f"{sym}: start={first:.2f}  end={last:.2f}  "
              f"total_return={total_ret:+.1f}%  "
              f"peak={peak:.2f}@{bars[peak_idx].ts.date()}  "
              f"trough={trough:.2f}@{bars[trough_idx].ts.date()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
