#!/usr/bin/env python3
"""Out-of-sample validation for the Clenow 3-strategy × 4-market portfolio.

Splits the daily P&L on the 2024-01-01 boundary:
  Train: through 2023-12-31  (3 years of usable data after HMM-style warmup,
                              though TimeReturn needs 250 days to start
                              trading so most of 2020 is warmup for it)
  Test:  2024-01-01 onward   (~2.4 years)

Reports per-strategy, per-market, and combined-portfolio Sharpe, total, max DD
on each split, so we can see whether the headline ~0.67 portfolio Sharpe holds
in the test period or just rides 2024+ favorable trends.

Usage:
    python scripts/clenow_oos.py
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend.data import load_bars_csv  # noqa: E402

# Reuse the cell-running plumbing from the main runner so we stay in sync.
from scripts.all_strategies_backtest import (  # noqa: E402
    EXCLUDED_CELLS, MARKETS, STRATEGIES, Cell, run_cell, stats,
)

DEFAULT_TRAIN_END = date(2018, 12, 31)  # 9y train / 7+y test on the 16y data


def aggregate(cells: list[Cell]) -> dict[date, float]:
    union = sorted(set().union(*(c.daily_pnl.keys() for c in cells)))
    return {d: sum(c.daily_pnl.get(d, 0.0) for c in cells) for d in union}


def print_block(title: str, rows: list[tuple[str, dict, dict]]) -> None:
    """rows: list of (label, train_stats, test_stats)."""
    print(f"\n{'='*100}")
    print(title)
    print('=' * 100)
    print(f"{'':<22}{'train':>40}{'test':>40}")
    print(f"{'':<22}{'-'*38:>40}{'-'*38:>40}")
    print(f"{'':<22}"
          f"{'days':>8}{'total':>10}{'sharpe':>10}{'max_dd':>10}"
          f"{'days':>8}{'total':>10}{'sharpe':>10}{'max_dd':>10}")
    for label, tr, te in rows:
        print(f"{label:<22}"
              f"{tr['n']:>8}{tr['total']:>10,.0f}{tr['sharpe']:>10.2f}"
              f"{tr['max_dd']:>10,.0f}"
              f"{te['n']:>8}{te['total']:>10,.0f}{te['sharpe']:>10.2f}"
              f"{te['max_dd']:>10,.0f}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--portfolio", type=float, default=300_000.0)
    p.add_argument("--risk-factor", type=float, default=0.001)
    p.add_argument("--train-end", default=DEFAULT_TRAIN_END.isoformat(),
                   help="Last date to include in the train split (YYYY-MM-DD)")
    args = p.parse_args()
    train_end = date.fromisoformat(args.train_end)

    print(f"Portfolio: ${args.portfolio:,.0f}  Risk factor: {args.risk_factor:.4f}")
    print(f"Train: ≤ {train_end}   Test: > {train_end}\n")

    market_bars = {}
    for sym, path, _, _ in MARKETS:
        if not Path(path).exists():
            print(f"SKIP {sym}", file=sys.stderr)
            continue
        market_bars[sym] = load_bars_csv(path)

    # Run all cells except structurally-excluded ones
    cells: list[Cell] = []
    for strat_name in STRATEGIES:
        for sym, _, pv, comm in MARKETS:
            if sym not in market_bars:
                continue
            if (strat_name, sym) in EXCLUDED_CELLS:
                continue
            cells.append(run_cell(strat_name, sym, market_bars[sym],
                                  pv, comm, args.risk_factor, args.portfolio))

    # ---- Per-strategy comparison ----
    per_strat_rows = []
    for strat_name in STRATEGIES:
        strat_cells = [c for c in cells if c.strategy == strat_name]
        merged = aggregate(strat_cells)
        train = {d: p for d, p in merged.items() if d <= train_end}
        test = {d: p for d, p in merged.items() if d > train_end}
        per_strat_rows.append((strat_name, stats(train), stats(test)))
    print_block("Per-strategy (across all 4 markets)", per_strat_rows)

    # ---- Per-market comparison ----
    per_market_rows = []
    for sym in market_bars:
        mkt_cells = [c for c in cells if c.symbol == sym]
        merged = aggregate(mkt_cells)
        train = {d: p for d, p in merged.items() if d <= train_end}
        test = {d: p for d, p in merged.items() if d > train_end}
        per_market_rows.append((sym, stats(train), stats(test)))
    print_block("Per-market (across all 3 strategies)", per_market_rows)

    # ---- Combined portfolio ----
    portfolio = aggregate(cells)
    p_train = {d: v for d, v in portfolio.items() if d <= train_end}
    p_test = {d: v for d, v in portfolio.items() if d > train_end}
    print_block("COMBINED PORTFOLIO",
                [("all strategies × markets", stats(p_train), stats(p_test))])

    # ---- Annualized portfolio metrics on each split ----
    s_tr = stats(p_train); s_te = stats(p_test)
    pv = args.portfolio
    print(f"\nAnnualized portfolio metrics:")
    print(f"{'':<22}{'return':>12}{'vol':>12}{'max_dd_%':>12}")
    print(f"{'train':<22}"
          f"{(s_tr['mean']*252/pv)*100:>+11.2f}%"
          f"{(s_tr['std']*math.sqrt(252)/pv)*100:>11.2f}%"
          f"{(s_tr['max_dd']/pv)*100:>11.2f}%")
    print(f"{'test':<22}"
          f"{(s_te['mean']*252/pv)*100:>+11.2f}%"
          f"{(s_te['std']*math.sqrt(252)/pv)*100:>11.2f}%"
          f"{(s_te['max_dd']/pv)*100:>11.2f}%")

    # ---- Yearly portfolio P&L for context ----
    yearly: dict[int, float] = defaultdict(float)
    for d, v in portfolio.items():
        yearly[d.year] += v
    print(f"\nYearly portfolio P&L:")
    for y in sorted(yearly):
        flag = " (train)" if y <= train_end.year else " (test)"
        print(f"  {y}{flag}: ${yearly[y]:>+10,.0f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
