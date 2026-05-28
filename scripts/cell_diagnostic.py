#!/usr/bin/env python3
"""Rank every (strategy × market) cell by long-run performance and surface
the candidates worth excluding from the portfolio.

A cell is a "drop candidate" if EITHER:
  - Sharpe < -0.30 (clear long-run loss), OR
  - Sharpe < -0.10 AND total < -$10,000 (modest negative but material)

These are deliberately conservative thresholds — we want to drop the cells
where the failure mode is structural, not noise.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.all_strategies_backtest import (  # noqa: E402
    MARKETS, STRATEGIES, run_cell, stats,
)
from trend.data import load_bars_csv  # noqa: E402


SHARPE_HARD = -0.30
SHARPE_SOFT = -0.10
TOTAL_SOFT = -10_000.0


def main() -> int:
    cells = []
    for sym, path, pv, comm in MARKETS:
        if not Path(path).exists():
            continue
        bars = load_bars_csv(path)
        for strat_name in STRATEGIES:
            cells.append(run_cell(strat_name, sym, bars, pv, comm, 0.001, 300_000.0))

    rows = []
    for c in cells:
        s = stats(c.daily_pnl)
        rows.append({
            "strategy": c.strategy,
            "symbol": c.symbol,
            "trades": c.n_trades,
            "total": s["total"],
            "sharpe": s["sharpe"],
            "max_dd": s["max_dd"],
        })

    # Sort ascending by Sharpe (worst first)
    rows.sort(key=lambda r: r["sharpe"])

    print(f"{'strategy':<14}{'symbol':<6}{'trades':>8}{'total':>12}"
          f"{'sharpe':>8}{'max_dd':>10}  drop?")
    print("-" * 88)
    drops = []
    for r in rows:
        drop = (r["sharpe"] < SHARPE_HARD or
                (r["sharpe"] < SHARPE_SOFT and r["total"] < TOTAL_SOFT))
        mark = " ← drop" if drop else ""
        if drop:
            drops.append((r["strategy"], r["symbol"]))
        print(f"{r['strategy']:<14}{r['symbol']:<6}{r['trades']:>8}"
              f"{r['total']:>12,.0f}{r['sharpe']:>8.2f}"
              f"{r['max_dd']:>10,.0f}  {mark}")

    print(f"\nDrop candidates ({len(drops)} cells):")
    for s, m in drops:
        print(f"  - {s} × {m}")

    # Show what dropping would do to portfolio aggregates.
    kept_cells = [c for c in cells if (c.strategy, c.symbol) not in drops]
    dropped_cells = [c for c in cells if (c.strategy, c.symbol) in drops]

    def aggregate(cs):
        union = sorted(set().union(*(c.daily_pnl.keys() for c in cs)))
        return {d: sum(c.daily_pnl.get(d, 0.0) for c in cs) for d in union}

    all_port = stats(aggregate(cells))
    kept_port = stats(aggregate(kept_cells))
    if dropped_cells:
        drop_port = stats(aggregate(dropped_cells))
    else:
        drop_port = stats({})

    print(f"\nPortfolio comparison (full 16y, IN-SAMPLE):")
    print(f"{'':<14}{'cells':>7}{'total':>12}{'sharpe':>8}{'max_dd':>10}")
    print(f"{'all 39':<14}{len(cells):>7}{all_port['total']:>12,.0f}"
          f"{all_port['sharpe']:>8.2f}{all_port['max_dd']:>10,.0f}")
    print(f"{'kept':<14}{len(kept_cells):>7}{kept_port['total']:>12,.0f}"
          f"{kept_port['sharpe']:>8.2f}{kept_port['max_dd']:>10,.0f}")
    if dropped_cells:
        print(f"{'dropped':<14}{len(dropped_cells):>7}{drop_port['total']:>12,.0f}"
              f"{drop_port['sharpe']:>8.2f}{drop_port['max_dd']:>10,.0f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
