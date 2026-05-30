#!/usr/bin/env python3
"""Measure the effect of a portfolio-level risk overlay (IDM + vol-target) on the
existing 3-strategy × N-market Clenow portfolio.

Unlike `all_strategies_backtest.py`, which sums each cell's *realized-at-close*
P&L (lumpy — a winner only registers when it closes), this script computes each
cell's true daily **mark-to-market** equity change. MTM is the correct basis for
a volatility-target overlay: a vol estimate built from realized-only P&L would
cut size right after a big trade closes, which is nonsense.

It then reports, all causally (no look-ahead):
  - the base portfolio's risk/return on MTM P&L,
  - the diversification actually present (avg pairwise cell correlation -> IDM),
  - a sweep of annualized vol targets, showing the risk/return and the average
    leverage the overlay would apply to hit each target.

Nothing here changes live sizing. It produces the evidence to choose a target;
the chosen multiplier is then fed to `Runner.set_risk_multiplier`.

Usage:
    python scripts/risk_overlay_backtest.py
    python scripts/risk_overlay_backtest.py --targets 0.06,0.08,0.10,0.12,0.15
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend import risk_overlay as ro  # noqa: E402
from trend.data import load_bars_csv  # noqa: E402
from trend.sim_broker import SimBroker  # noqa: E402
from trend.types import Bar  # noqa: E402
from scripts.all_strategies_backtest import (  # noqa: E402
    EXCLUDED_CELLS, MARKETS, STRATEGIES, make_strategy,
)

ET = ZoneInfo("America/New_York")


def cell_mtm_daily(strategy_name: str, bars: list[Bar], point_value: float,
                   commission: float, risk_factor: float,
                   portfolio_value: float) -> dict[date, float]:
    """Daily mark-to-market equity change ($) for one (strategy × market) cell.

    Equity = cumulative realized (incl. commissions) + unrealized on the open
    position. Daily P&L is the change in equity, so it captures both closed
    trades and the daily move on held positions."""
    broker = SimBroker(point_value=point_value, commission_per_contract=commission)
    strat = make_strategy(strategy_name, broker, point_value,
                          risk_factor, portfolio_value)
    daily: dict[date, float] = {}
    prev_equity = 0.0
    for b in bars:
        broker.on_bar(b)
        strat.on_bar(b)
        unreal = broker.position_qty * (b.close - broker.position_avg) * point_value
        equity = broker.total_realized + unreal
        d = b.ts.astimezone(ET).date()
        daily[d] = daily.get(d, 0.0) + (equity - prev_equity)
        prev_equity = equity
    return daily


def fmt(stats: dict, pv: float, extra: str = "") -> str:
    return (f"{stats['ann_ret']*100:>8.2f}%{stats['ann_vol']*100:>8.2f}%"
            f"{stats['sharpe']:>8.2f}{stats['max_dd']/1*100:>9.2f}%{extra}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--portfolio", type=float, default=300_000.0)
    p.add_argument("--risk-factor", type=float, default=0.001)
    p.add_argument("--targets", default="0.06,0.08,0.10,0.12,0.15",
                   help="comma-separated annualized vol targets for the overlay")
    p.add_argument("--vol-span", type=int, default=63,
                   help="EWMA span for the overlay's trailing-vol estimate")
    p.add_argument("--leverage-cap", type=float, default=3.0)
    args = p.parse_args()
    pv = args.portfolio
    targets = [float(x) for x in args.targets.split(",") if x.strip()]

    # ---- run every cell, capture MTM daily P&L -------------------------------
    market_bars: dict[str, list[Bar]] = {}
    for sym, path, _, _ in MARKETS:
        if Path(path).exists():
            market_bars[sym] = load_bars_csv(path)

    cell_daily: dict[tuple[str, str], dict[date, float]] = {}
    for strat_name in STRATEGIES:
        for sym, _, point_value, commission in MARKETS:
            if sym not in market_bars or (strat_name, sym) in EXCLUDED_CELLS:
                continue
            cell_daily[(strat_name, sym)] = cell_mtm_daily(
                strat_name, market_bars[sym], point_value, commission,
                args.risk_factor, pv)

    # ---- align all cells onto one date axis, in fractional returns -----------
    all_dates = sorted(set().union(*(d.keys() for d in cell_daily.values())))
    cell_returns: dict[tuple[str, str], list[float]] = {
        key: [daily.get(d, 0.0) / pv for d in all_dates]
        for key, daily in cell_daily.items()
    }
    base_returns = [sum(cell_returns[k][i] for k in cell_returns)
                    for i in range(len(all_dates))]

    # ---- diagnostics ---------------------------------------------------------
    n_cells = len(cell_daily)
    avg_corr = ro.average_offdiag_correlation(list(cell_returns.values()))
    raw_idm = ro.idm(avg_corr, n_cells, cap=float("inf"))
    implied_idm = ro.idm(avg_corr, n_cells)
    base = ro.return_stats(base_returns)

    print(f"Portfolio ${pv:,.0f}   risk-factor {args.risk_factor:.4f}   "
          f"overlay vol-span {args.vol_span}   leverage cap {args.leverage_cap:g}x")
    print(f"Window {all_dates[0]} -> {all_dates[-1]}  "
          f"({len(all_dates)} trading days, {n_cells} cells)\n")
    print(f"Average pairwise cell correlation : {avg_corr:+.3f}")
    print(f"Diversification ratio (raw IDM)   : {raw_idm:.2f}  (capped {implied_idm:.2f})")
    print(f"  -> cells are highly decorrelated. NOTE: the current sizing already")
    print(f"     SUMS all {n_cells} cells (no 1/N averaging), so this diversification")
    print(f"     is ALREADY reflected in the base vol below — {n_cells} summed cells")
    print(f"     realize {base['ann_vol']*100:.0f}%, not ~{base['ann_vol']*raw_idm*100:.0f}%. "
          f"IDM is the explanation,")
    print(f"     not free headroom: the lever that sets risk is the vol target.\n")

    print(f"{'config':<22}{'ann_ret':>9}{'ann_vol':>9}{'sharpe':>8}"
          f"{'max_dd':>10}{'avg_lev':>9}{'p95_lev':>9}")
    print("-" * 76)
    print(f"{'base (current)':<22}{fmt(base, pv)}{'1.00x':>9}{'1.00x':>9}")

    for t in sorted(targets):
        scaled, mults = ro.apply_vol_target(
            base_returns, t, span=args.vol_span,
            leverage_cap=args.leverage_cap)
        st = ro.return_stats(scaled)
        applied = sorted(m for m in mults if m != 1.0) or [1.0]
        avg_lev = sum(applied) / len(applied)
        p95 = applied[min(len(applied) - 1, int(0.95 * len(applied)))]
        print(f"{'overlay -> ' + f'{t*100:.0f}% vol':<22}{fmt(st, pv)}"
              f"{avg_lev:>8.2f}x{p95:>8.2f}x")

    print("\nNotes:")
    print("  - max_dd is peak-to-trough on cumulative return (% of portfolio).")
    print("  - avg/p95_lev are the overlay multipliers actually applied (post seed).")
    print("  - The overlay both *sets the level* (scales toward the target) and")
    print("    *stabilizes* vol across regimes; compare Sharpe at equal vol to the")
    print("    base to see the stabilization benefit independent of leverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
