#!/usr/bin/env python3
"""Head-to-head: continuous multi-speed EWMAC vs Clenow Core Trend.

EWMAC is a candidate *replacement* for Core Trend (both harvest the medium/long
trend premium). Two questions decide its fate, and this script answers both on
the futures basket, mark-to-market:

  1. Does EWMAC beat Core Trend on risk-adjusted return, per market and pooled?
  2. Does EWMAC stay decorrelated from Time Return? (No point swapping in another
     trend sleeve that just double-counts what Time Return already captures.)

It reports per-market Sharpe/return/vol/DD for both, the pooled single-strategy
portfolio for each, and the daily-return correlation of each trend sleeve to the
Time Return sleeve.

Usage:
    python scripts/ewmac_vs_coretrend.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend import risk_overlay as ro  # noqa: E402
from trend.core_trend import CoreTrendConfig, CoreTrendStrategy  # noqa: E402
from trend.data import load_bars_csv  # noqa: E402
from trend.ewmac_trend import EwmacTrendConfig, EwmacTrendStrategy  # noqa: E402
from trend.sim_broker import SimBroker  # noqa: E402
from trend.time_return import TimeReturnConfig, TimeReturnStrategy  # noqa: E402
from trend.types import Bar  # noqa: E402
from scripts.all_strategies_backtest import MARKETS  # noqa: E402

ET = ZoneInfo("America/New_York")


def _make(name, broker, pv, rf, portfolio):
    if name == "EWMAC":
        return EwmacTrendStrategy(broker, EwmacTrendConfig(
            risk_factor=rf, portfolio_value_usd=portfolio, point_value=pv))
    if name == "CoreTrend":
        return CoreTrendStrategy(broker, CoreTrendConfig(
            risk_factor=rf, portfolio_value_usd=portfolio, point_value=pv))
    if name == "TimeReturn":
        return TimeReturnStrategy(broker, TimeReturnConfig(
            risk_factor=rf, portfolio_value_usd=portfolio, point_value=pv))
    raise ValueError(name)


def cell_mtm(name, bars, pv, commission, rf, portfolio) -> dict[date, float]:
    """Daily mark-to-market equity change ($) for one (strategy × market) cell."""
    broker = SimBroker(point_value=pv, commission_per_contract=commission)
    strat = _make(name, broker, pv, rf, portfolio)
    daily: dict[date, float] = {}
    prev_eq = 0.0
    for b in bars:
        broker.on_bar(b)
        strat.on_bar(b)
        eq = broker.total_realized + broker.position_qty * (b.close - broker.position_avg) * pv
        d = b.ts.astimezone(ET).date()
        daily[d] = daily.get(d, 0.0) + (eq - prev_eq)
        prev_eq = eq
    return daily


def pooled_returns(cells: dict[str, dict[date, float]], portfolio: float):
    """Sum per-market daily P&L into one return stream over the union of dates."""
    dates = sorted(set().union(*(d.keys() for d in cells.values())))
    return dates, [sum(c.get(d, 0.0) for c in cells.values()) / portfolio for d in dates]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--portfolio", type=float, default=300_000.0)
    ap.add_argument("--risk-factor", type=float, default=0.001)
    args = ap.parse_args()
    pv = args.portfolio

    market_bars: dict[str, list[Bar]] = {}
    for sym, path, _, _ in MARKETS:
        if Path(path).exists():
            market_bars[sym] = load_bars_csv(path)

    # Per-market MTM daily P&L for each strategy.
    cells: dict[str, dict[str, dict[date, float]]] = {
        "EWMAC": {}, "CoreTrend": {}, "TimeReturn": {}}
    for sym, _, point_value, commission in MARKETS:
        if sym not in market_bars:
            continue
        for strat in cells:
            cells[strat][sym] = cell_mtm(
                strat, market_bars[sym], point_value, commission,
                args.risk_factor, pv)

    # ===== Per-market: EWMAC vs CoreTrend =====
    print(f"Portfolio ${pv:,.0f}   risk-factor {args.risk_factor:.4f}   (mark-to-market)\n")
    print("Per-market Sharpe (ann ret% / vol% / maxDD%)  —  EWMAC vs CoreTrend")
    print("-" * 78)
    print(f"{'market':<8}{'EWMAC sharpe':>14}{'Core sharpe':>14}"
          f"{'EWMAC ret':>11}{'Core ret':>11}{'winner':>10}")
    print("-" * 78)
    ewmac_wins = 0
    for sym in market_bars:
        e = ro.return_stats([v / pv for v in
                             [cells["EWMAC"][sym][d] for d in sorted(cells["EWMAC"][sym])]])
        c = ro.return_stats([v / pv for v in
                             [cells["CoreTrend"][sym][d] for d in sorted(cells["CoreTrend"][sym])]])
        win = "EWMAC" if e["sharpe"] > c["sharpe"] else "Core"
        ewmac_wins += win == "EWMAC"
        print(f"{sym:<8}{e['sharpe']:>14.2f}{c['sharpe']:>14.2f}"
              f"{e['ann_ret']*100:>10.1f}%{c['ann_ret']*100:>10.1f}%{win:>10}")
    print("-" * 78)
    print(f"EWMAC wins {ewmac_wins}/{len(market_bars)} markets on Sharpe\n")

    # ===== Pooled single-strategy portfolios =====
    series = {}
    for strat in cells:
        dates, rets = pooled_returns(cells[strat], pv)
        series[strat] = (dates, rets)

    print("Pooled single-strategy portfolio (all markets, equal sizing)")
    print("-" * 60)
    print(f"{'strategy':<14}{'ann_ret':>9}{'ann_vol':>9}{'sharpe':>8}{'max_dd':>10}")
    for strat in ("EWMAC", "CoreTrend", "TimeReturn"):
        st = ro.return_stats(series[strat][1])
        print(f"{strat:<14}{st['ann_ret']*100:>8.2f}%{st['ann_vol']*100:>8.2f}%"
              f"{st['sharpe']:>8.2f}{st['max_dd']*100:>9.2f}%")

    # ===== Correlations (the decorrelation test) =====
    # Align all three on a common date axis before correlating.
    common = sorted(set(series["EWMAC"][0]) & set(series["CoreTrend"][0])
                    & set(series["TimeReturn"][0]))
    idx = {strat: {d: r for d, r in zip(series[strat][0], series[strat][1])}
           for strat in series}
    aligned = {strat: [idx[strat][d] for d in common] for strat in series}

    print("\nDaily-return correlation between sleeves (the decorrelation test)")
    print("-" * 60)
    ec = ro.pearson(aligned["EWMAC"], aligned["CoreTrend"])
    et = ro.pearson(aligned["EWMAC"], aligned["TimeReturn"])
    ct = ro.pearson(aligned["CoreTrend"], aligned["TimeReturn"])
    print(f"  EWMAC      vs CoreTrend : {ec:+.3f}  (high -> they're redundant)")
    print(f"  EWMAC      vs TimeReturn: {et:+.3f}  (low -> safe to add alongside)")
    print(f"  CoreTrend  vs TimeReturn: {ct:+.3f}  (the incumbent's overlap, for reference)")

    print("\nVerdict guide:")
    print("  - Replace Core Trend with EWMAC only if EWMAC's pooled Sharpe is")
    print("    clearly higher AND its TimeReturn correlation is no worse than")
    print("    Core Trend's — otherwise you'd lose diversification to gain little.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
