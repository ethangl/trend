#!/usr/bin/env python3
"""Why did the ETF EWMAC show Sharpe ~1.1 but the futures sleeve only ~0.24?

Tests two hypotheses against the futures data we have, mark-to-market:

  H1  The 1.1 is the SPY *blend* (trend + long equity), not the pure sleeve.
      -> blend the futures EWMAC sleeve with long-MES (an E-mini S&P proxy for
         SPY) and see how far the best Sharpe rises from the sleeve's 0.24.

  H2  The ETF universe (equities/bonds/gold) simply trends better than our
      FX/ag-diluted futures basket.
      -> pool EWMAC over a "strong-trender" subset (equity indices + gold +
         rates) vs the "chop" subset (FX + grains) and compare.

If both lift the number, the apparent contradiction dissolves: 0.24 (pure
sleeve, full basket) and 1.1 (sleeve + long SPY, trendy ETF universe) are the
weakest and strongest points on the same spectrum.

Usage:
    python scripts/ewmac_decompose.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend import risk_overlay as ro  # noqa: E402
from trend.data import load_bars_csv  # noqa: E402
from scripts.all_strategies_backtest import MARKETS  # noqa: E402
from scripts.ewmac_vs_coretrend import cell_mtm, pooled_returns  # noqa: E402

ET = ZoneInfo("America/New_York")
PV = 300_000.0
RF = 0.001

TRENDY = {"MES", "MNQ", "MYM", "MGC", "ZN"}          # equity indices + gold + rates
CHOP = {"M6E", "M6B", "MJY", "M6A", "ZC", "ZS"}      # FX + grains


def best_blend(a_dates, a, b_dates, b):
    """Max Sharpe over w in [0,1] of w*A + (1-w)*B on their common dates."""
    ai = dict(zip(a_dates, a))
    bi = dict(zip(b_dates, b))
    common = sorted(set(a_dates) & set(b_dates))
    ra = [ai[d] for d in common]
    rb = [bi[d] for d in common]
    best_w, best_s = 0.0, -9.9
    for i in range(21):
        w = i / 20
        blend = [w * ra[k] + (1 - w) * rb[k] for k in range(len(common))]
        s = ro.return_stats(blend)["sharpe"]
        if s > best_s:
            best_s, best_w = s, w
    return best_w, best_s, ro.pearson(ra, rb)


def main() -> int:
    bars = {sym: load_bars_csv(p) for sym, p, _, _ in MARKETS if Path(p).exists()}
    pvs = {sym: pv for sym, _, pv, _ in MARKETS}
    comm = {sym: c for sym, _, _, c in MARKETS}

    ewmac = {sym: cell_mtm("EWMAC", bars[sym], pvs[sym], comm[sym], RF, PV)
             for sym in bars}

    def pooled(syms):
        sub = {s: ewmac[s] for s in syms if s in ewmac}
        return pooled_returns(sub, PV)

    d_all, r_all = pooled(bars.keys())
    d_trend, r_trend = pooled(TRENDY)
    d_chop, r_chop = pooled(CHOP)

    # Long-MES buy & hold = SPY proxy (daily fractional returns).
    es = bars["MES"]
    mes_dates = [b.ts.astimezone(ET).date() for b in es[1:]]
    mes_bh = [es[i].close / es[i - 1].close - 1 for i in range(1, len(es))]

    def line(label, rets):
        s = ro.return_stats(rets)
        print(f"{label:<34}{s['ann_ret']*100:>8.2f}%{s['ann_vol']*100:>8.2f}%"
              f"{s['sharpe']:>8.2f}{s['max_dd']*100:>9.2f}%")

    print(f"Mark-to-market, ${PV:,.0f}, risk-factor {RF}\n")
    print(f"{'stream':<34}{'ann_ret':>9}{'ann_vol':>9}{'sharpe':>8}{'max_dd':>10}")
    print("-" * 70)
    line("EWMAC sleeve — full basket", r_all)
    line("EWMAC sleeve — trendy subset", r_trend)
    line("EWMAC sleeve — chop subset", r_chop)
    line("Long MES buy & hold (SPY proxy)", mes_bh)

    print("\nH1 — blend the sleeve with long equity (mirrors the ETF's SPY blend):")
    print("-" * 70)
    for label, dd, rr in [("full-basket sleeve", d_all, r_all),
                          ("trendy-subset sleeve", d_trend, r_trend)]:
        w, s, rho = best_blend(dd, rr, mes_dates, mes_bh)
        print(f"  {label:<24} + long MES: best Sharpe {s:.2f} "
              f"at {w*100:.0f}% sleeve / {(1-w)*100:.0f}% MES  (corr {rho:+.2f})")

    print("\nTakeaways:")
    print("  - If the trendy subset >> full basket, H2 holds: universe matters and")
    print("    our FX/grain markets dilute the trend Sharpe the ETF universe lacks.")
    print("  - If the blends land near ~1.0, H1 holds: the ETF's 1.1 is mostly the")
    print("    long-SPY leg + diversification, NOT the trend signal itself.")
    print("  - Either way, the pure-sleeve-vs-Core-Trend verdict is unaffected:")
    print("    that comparison was trend-vs-trend, the right basis for a *swap*.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
