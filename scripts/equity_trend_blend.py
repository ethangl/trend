#!/usr/bin/env python3
"""C — does a structural long-equity allocation belong alongside the trend system?

The ETF EWMAC's headline Sharpe came mostly from blending the trend sleeve with
long SPY in a 16-year equity bull. Before importing that idea, the honest test is
NOT "does adding equity raise in-sample Sharpe" (it trivially does) but "does the
trend system actually cushion equity drawdowns?" — real diversification vs beta.

This blends the *actual* 3-strategy Clenow trend portfolio (Core + Time + Counter
across all markets, structural exclusions applied), mark-to-market, with a
long-equity leg (long MES buy & hold, an S&P proxy), and reports:

  1. Standalone Sharpe of trend vs equity, and their correlation.
  2. The blend Sharpe across equity weights (the in-sample, bull-flattered view).
  3. The decisive view: the worst equity drawdown windows, and what the trend
     book did during each. Positive trend returns while equity falls = genuine
     crisis cushioning; trend also bleeding = the blend is just averaging.
  4. Calendar-year returns side by side.

Usage:
    python scripts/equity_trend_blend.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend import risk_overlay as ro  # noqa: E402
from trend.data import load_bars_csv  # noqa: E402
from scripts.all_strategies_backtest import EXCLUDED_CELLS, MARKETS, STRATEGIES  # noqa: E402
from scripts.risk_overlay_backtest import cell_mtm_daily  # noqa: E402

ET = ZoneInfo("America/New_York")
PV = 300_000.0
RF = 0.001


def aligned(stream: dict[date, float], dates: list[date]) -> list[float]:
    return [stream.get(d, 0.0) for d in dates]


def compound(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return eq - 1


def underwater_segments(dates, rets):
    """Maximal peak-to-recovery underwater segments of the compounded curve."""
    eq = 1.0
    peak = 1.0
    peak_date = dates[0]
    seg = None
    out = []
    for d, r in zip(dates, rets):
        eq *= (1 + r)
        if eq >= peak:
            if seg is not None:
                seg["recovery"] = d
                out.append(seg)
                seg = None
            peak = eq
            peak_date = d
        else:
            if seg is None:
                seg = {"peak_date": peak_date, "peak": peak,
                       "trough": eq, "trough_date": d, "recovery": None}
            if eq < seg["trough"]:
                seg["trough"] = eq
                seg["trough_date"] = d
    if seg is not None:
        out.append(seg)
    for s in out:
        s["depth"] = (s["trough"] - s["peak"]) / s["peak"]
    return out


def main() -> int:
    bars = {sym: load_bars_csv(p) for sym, p, _, _ in MARKETS if Path(p).exists()}

    # ---- the real 3-strategy trend portfolio, MTM ----
    cell_daily: dict[tuple[str, str], dict[date, float]] = {}
    for strat in STRATEGIES:
        for sym, _, pv, comm in MARKETS:
            if sym not in bars or (strat, sym) in EXCLUDED_CELLS:
                continue
            cell_daily[(strat, sym)] = cell_mtm_daily(strat, bars[sym], pv, comm, RF, PV)
    trend_dates = sorted(set().union(*(d.keys() for d in cell_daily.values())))
    trend_stream = {d: sum(c.get(d, 0.0) for c in cell_daily.values()) / PV
                    for d in trend_dates}

    # ---- long-equity leg: long MES buy & hold (S&P proxy) ----
    es = bars["MES"]
    eq_stream = {es[i].ts.astimezone(ET).date(): es[i].close / es[i - 1].close - 1
                 for i in range(1, len(es))}

    common = sorted(set(trend_stream) & set(eq_stream))
    trend = aligned(trend_stream, common)
    equity = aligned(eq_stream, common)

    s_trend = ro.return_stats(trend)
    s_eq = ro.return_stats(equity)
    rho = ro.pearson(trend, equity)

    print(f"Mark-to-market, ${PV:,.0f}, risk-factor {RF}")
    print(f"Window {common[0]} -> {common[-1]}  ({len(common)} days)\n")
    print(f"{'stream':<24}{'ann_ret':>9}{'ann_vol':>9}{'sharpe':>8}{'max_dd':>10}")
    print("-" * 60)
    for label, s in [("Trend portfolio (3-strat)", s_trend),
                     ("Long equity (MES B&H)", s_eq)]:
        print(f"{label:<24}{s['ann_ret']*100:>8.2f}%{s['ann_vol']*100:>8.2f}%"
              f"{s['sharpe']:>8.2f}{s['max_dd']*100:>9.2f}%")
    print(f"\nTrend / equity daily-return correlation: {rho:+.3f}")

    # ---- blend sweep (in-sample, bull-flattered) ----
    print("\nBlend Sharpe by equity weight (rest to the trend system):")
    print("-" * 60)
    print(f"{'equity wt':>10}{'ann_ret':>10}{'ann_vol':>10}{'sharpe':>9}{'max_dd':>10}")
    best_w, best_s = 0.0, -9.0
    for i in range(21):
        w = i / 20
        blend = [w * equity[k] + (1 - w) * trend[k] for k in range(len(common))]
        s = ro.return_stats(blend)
        if s["sharpe"] > best_s:
            best_s, best_w = s["sharpe"], w
        if i % 5 == 0:
            print(f"{w*100:>9.0f}%{s['ann_ret']*100:>9.2f}%{s['ann_vol']*100:>9.2f}%"
                  f"{s['sharpe']:>9.2f}{s['max_dd']*100:>9.2f}%")
    print(f"\nSharpe-optimal equity weight: {best_w*100:.0f}%  (Sharpe {best_s:.2f})")

    # ---- the decisive view: behavior in equity drawdowns ----
    segs = [s for s in underwater_segments(common, equity) if s["depth"] <= -0.08]
    segs.sort(key=lambda s: s["depth"])
    tidx = dict(zip(common, trend))
    eidx = dict(zip(common, equity))

    print("\nWorst equity drawdown windows — peak->trough decline phase:")
    print("(does the trend book cushion, or bleed alongside?)")
    print("-" * 78)
    print(f"{'peak':>11}{'trough':>11}{'equity':>10}{'trend':>10}{'60/40 blend':>13}")
    print("-" * 78)
    for s in segs[:6]:
        win = [d for d in common if s["peak_date"] < d <= s["trough_date"]]
        eq_ret = compound([eidx[d] for d in win])
        tr_ret = compound([tidx[d] for d in win])
        bl_ret = compound([0.6 * tidx[d] + 0.4 * eidx[d] for d in win])
        print(f"{str(s['peak_date']):>11}{str(s['trough_date']):>11}"
              f"{eq_ret*100:>9.1f}%{tr_ret*100:>9.1f}%{bl_ret*100:>12.1f}%")

    # ---- calendar-year returns ----
    years = sorted({d.year for d in common})
    print("\nCalendar-year returns (compounded):")
    print("-" * 52)
    print(f"{'year':<6}{'equity':>12}{'trend':>12}{'60/40 blend':>14}")
    for y in years:
        wd = [d for d in common if d.year == y]
        eq_y = compound([eidx[d] for d in wd])
        tr_y = compound([tidx[d] for d in wd])
        bl_y = compound([0.6 * tidx[d] + 0.4 * eidx[d] for d in wd])
        print(f"{y:<6}{eq_y*100:>11.1f}%{tr_y*100:>11.1f}%{bl_y*100:>13.1f}%")

    print("\nRead it this way:")
    print("  - Trend returns POSITIVE in the equity-drawdown rows = real crisis")
    print("    cushioning (crisis alpha), the diversification worth paying for.")
    print("  - Trend also negative there = the blend is just averaging two")
    print("    risky books, and the headline Sharpe is mostly the equity bull.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
