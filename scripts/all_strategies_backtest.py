#!/usr/bin/env python3
"""Run Core Trend + Time Return + Counter Trend across all 4 micro markets and
report per-strategy, per-market, and combined-portfolio metrics.

Architecture: each (strategy × market) combo gets its own SimBroker. 12 brokers
total. Daily P&L is summed across all of them for the portfolio view.

Usage:
    python scripts/all_strategies_backtest.py
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend.core_trend import CoreTrendConfig, CoreTrendStrategy  # noqa: E402
from trend.counter_trend import CounterTrendConfig, CounterTrendStrategy  # noqa: E402
from trend.data import load_bars_csv  # noqa: E402
from trend.sim_broker import SimBroker  # noqa: E402
from trend.time_return import TimeReturnConfig, TimeReturnStrategy  # noqa: E402
from trend.types import Bar  # noqa: E402


# Each row: (label, data_path, point_value, commission_per_contract).
# We backtest on the mini-futures price history (16+ yrs since 2010-06)
# but size with the *micro* contract point values, since live trading will
# use micros. The mini price series is identical to the micro for the same
# underlying — the only thing that changes between mini and micro is contract
# size, which is exactly what point_value captures here.
MARKETS: list[tuple[str, str, float, float]] = [
    # (label, data_path, point_value, commission_per_contract)
    # label = the contract we'd actually trade live. point_value matches that
    # contract size. Backtest data is the mini/full-size with longer history.

    # Equity indexes (live via micros)
    ("MES",  "data/es_1d.csv",  5.0,           0.62),
    ("MNQ",  "data/nq_1d.csv",  2.0,           0.62),
    ("M2K",  "data/rty_1d.csv", 5.0,           0.62),  # ~8y history (RTY launched 2017)
    ("MYM",  "data/ym_1d.csv",  0.50,          0.62),

    # Energy & metals (live via micros)
    ("MCL",  "data/cl_1d.csv",  100.0,         0.62),
    ("MGC",  "data/gc_1d.csv",  10.0,          0.62),

    # Currencies (live via micros)
    ("M6E",  "data/6e_1d.csv",  12_500.0,      1.50),  # USD per EUR
    ("M6B",  "data/6b_1d.csv",  6_250.0,       1.50),  # USD per GBP
    ("MJY",  "data/6j_1d.csv",  1_250_000.0,   1.50),  # USD per JPY (quote in $0.00xxxx)
    ("M6A",  "data/6a_1d.csv",  10_000.0,      1.50),  # USD per AUD

    # Rates & ag (live via full-size; no current micro)
    ("ZN",   "data/zn_1d.csv",  1_000.0,       2.40),  # 10Y T-Note, $1000 per point
    ("ZC",   "data/zc_1d.csv",  50.0,          1.55),  # Corn, $50/cent
    ("ZS",   "data/zs_1d.csv",  50.0,          1.55),  # Soybeans, $50/cent

    # Crypto (live via micros). Backtest on full-size for longer history.
    # ETH has ~5y of CME data, BTC has ~8.5y. Both shorter than the rest.
    ("MBT",  "data/btc_1d.csv", 0.10,          0.62),  # Micro Bitcoin: 0.1 BTC, $0.10/pt
    ("MET",  "data/eth_1d.csv", 0.10,          0.62),  # Micro Ether: 0.1 ETH, $0.10/pt
]

STRATEGIES = ["CoreTrend", "TimeReturn", "CounterTrend"]

# (strategy_name, market_label) pairs to skip. These are cells whose failure
# mode is structural rather than noise — see scripts/cell_diagnostic.py output
# for the rationale. ALL added based on full-period 16y in-sample evidence;
# OOS holds the verdict on whether the exclusions persist forward.
EXCLUDED_CELLS: set[tuple[str, str]] = {
    ("TimeReturn",   "M2K"),  # small caps + multi-month cycles whipsaw monthly rebalance
    ("TimeReturn",   "ZC"),   # corn has seasonal not directional trend
    ("CounterTrend", "M6A"),  # Aussie chronic bear → bull-filter rarely fires
    ("TimeReturn",   "M6A"),  # same Aussie chronic weakness
    ("CoreTrend",    "MET"),  # ETH has been in relative bear since 2021 ATH; symmetric
                              # breakout eats whipsaws (CounterTrend's bull-filter handles it)
}


@dataclass
class Cell:
    strategy: str
    symbol: str
    n_trades: int
    daily_pnl: dict[date, float]   # dense over the market's bar dates
    total_pnl: float


def stats(daily: dict[date, float]) -> dict:
    pnls = list(daily.values())
    n = len(pnls)
    if n == 0:
        return {"n": 0, "total": 0.0, "mean": 0.0, "std": 0.0,
                "sharpe": 0.0, "max_dd": 0.0}
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / max(1, n - 1)
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0
    eq = 0.0; peak = 0.0; dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "total": sum(pnls), "mean": mean, "std": std,
            "sharpe": sharpe, "max_dd": dd}


def correlation(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma = sum(a) / n; mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da > 0 and db > 0 else 0.0


def make_strategy(name: str, broker: SimBroker, point_value: float,
                  risk_factor: float, portfolio_value: float):
    if name == "CoreTrend":
        return CoreTrendStrategy(broker, CoreTrendConfig(
            risk_factor=risk_factor, portfolio_value_usd=portfolio_value,
            point_value=point_value))
    if name == "TimeReturn":
        return TimeReturnStrategy(broker, TimeReturnConfig(
            risk_factor=risk_factor, portfolio_value_usd=portfolio_value,
            point_value=point_value))
    if name == "CounterTrend":
        return CounterTrendStrategy(broker, CounterTrendConfig(
            risk_factor=risk_factor, portfolio_value_usd=portfolio_value,
            point_value=point_value))
    raise ValueError(name)


def run_cell(strategy_name: str, symbol: str, bars: list[Bar],
             point_value: float, commission: float,
             risk_factor: float, portfolio_value: float) -> Cell:
    broker = SimBroker(point_value=point_value, commission_per_contract=commission)
    strat = make_strategy(strategy_name, broker, point_value,
                          risk_factor, portfolio_value)
    for b in bars:
        broker.on_bar(b)
        strat.on_bar(b)
    realized = broker.daily_realized
    dense = {b.ts.date(): realized.get(b.ts.date(), 0.0) for b in bars}
    return Cell(
        strategy=strategy_name,
        symbol=symbol,
        n_trades=len(strat.trades),
        daily_pnl=dense,
        total_pnl=broker.total_realized,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--portfolio", type=float, default=300_000.0)
    p.add_argument("--risk-factor", type=float, default=0.001)
    args = p.parse_args()

    print(f"Portfolio: ${args.portfolio:,.0f}  Risk factor: {args.risk_factor:.4f}\n")

    # Load each market once
    market_bars: dict[str, list[Bar]] = {}
    for sym, path, _, _ in MARKETS:
        if not Path(path).exists():
            print(f"SKIP {sym}: {path} not found", file=sys.stderr)
            continue
        market_bars[sym] = load_bars_csv(path)
        print(f"Loaded {sym}: {len(market_bars[sym])} daily bars")

    # Run all cells (strategy × market) except those structurally excluded.
    cells: list[Cell] = []
    for strat_name in STRATEGIES:
        for sym, _, point_value, commission in MARKETS:
            if sym not in market_bars:
                continue
            if (strat_name, sym) in EXCLUDED_CELLS:
                continue
            cell = run_cell(strat_name, sym, market_bars[sym],
                            point_value, commission,
                            args.risk_factor, args.portfolio)
            cells.append(cell)
    print(f"\nExcluded {len(EXCLUDED_CELLS)} cells: "
          f"{sorted(EXCLUDED_CELLS)}\n")

    # ===== Per-cell grid =====
    print(f"\n{'='*90}")
    print(f"Per (strategy × market) results")
    print(f"{'='*90}")
    print(f"{'strategy':<14}{'symbol':<6}{'trades':>8}{'total':>12}{'mean/d':>9}"
          f"{'std/d':>9}{'sharpe':>8}{'max_dd':>10}")
    print("-" * 90)
    for c in cells:
        s = stats(c.daily_pnl)
        print(f"{c.strategy:<14}{c.symbol:<6}{c.n_trades:>8}{s['total']:>12,.0f}"
              f"{s['mean']:>9,.2f}{s['std']:>9,.2f}{s['sharpe']:>8.2f}"
              f"{s['max_dd']:>10,.0f}")

    # ===== Per-strategy totals =====
    print(f"\n{'='*90}")
    print(f"Per-strategy aggregation (across all markets)")
    print(f"{'='*90}")
    print(f"{'strategy':<14}{'trades':>8}{'total':>12}{'mean/d':>9}{'std/d':>9}"
          f"{'sharpe':>8}{'max_dd':>10}")
    print("-" * 90)
    strat_daily: dict[str, dict[date, float]] = {}
    for strat_name in STRATEGIES:
        union_dates = sorted(set().union(*(c.daily_pnl.keys()
                                            for c in cells if c.strategy == strat_name)))
        merged = {d: sum(c.daily_pnl.get(d, 0.0)
                         for c in cells if c.strategy == strat_name)
                  for d in union_dates}
        strat_daily[strat_name] = merged
        s = stats(merged)
        n_trades = sum(c.n_trades for c in cells if c.strategy == strat_name)
        print(f"{strat_name:<14}{n_trades:>8}{s['total']:>12,.0f}"
              f"{s['mean']:>9,.2f}{s['std']:>9,.2f}{s['sharpe']:>8.2f}"
              f"{s['max_dd']:>10,.0f}")

    # ===== Per-market totals =====
    print(f"\n{'='*90}")
    print(f"Per-market aggregation (across all strategies)")
    print(f"{'='*90}")
    print(f"{'symbol':<8}{'trades':>8}{'total':>12}{'mean/d':>9}{'std/d':>9}"
          f"{'sharpe':>8}{'max_dd':>10}")
    print("-" * 90)
    market_daily: dict[str, dict[date, float]] = {}
    for sym in market_bars:
        union_dates = sorted(set().union(*(c.daily_pnl.keys()
                                            for c in cells if c.symbol == sym)))
        merged = {d: sum(c.daily_pnl.get(d, 0.0)
                         for c in cells if c.symbol == sym)
                  for d in union_dates}
        market_daily[sym] = merged
        s = stats(merged)
        n_trades = sum(c.n_trades for c in cells if c.symbol == sym)
        print(f"{sym:<8}{n_trades:>8}{s['total']:>12,.0f}{s['mean']:>9,.2f}"
              f"{s['std']:>9,.2f}{s['sharpe']:>8.2f}{s['max_dd']:>10,.0f}")

    # ===== Overall portfolio =====
    all_dates = sorted(set().union(*(c.daily_pnl.keys() for c in cells)))
    portfolio_daily = {d: sum(c.daily_pnl.get(d, 0.0) for c in cells) for d in all_dates}
    s_port = stats(portfolio_daily)
    print(f"\n{'='*90}")
    print(f"COMBINED PORTFOLIO (all strategies × all markets)")
    print(f"{'='*90}")
    n_total = sum(c.n_trades for c in cells)
    print(f"trades={n_total}  total=${s_port['total']:,.0f}  "
          f"mean/d=${s_port['mean']:,.2f}  std/d=${s_port['std']:,.2f}  "
          f"Sharpe={s_port['sharpe']:.2f}  max_dd=${s_port['max_dd']:,.0f}")

    ann_ret = s_port['mean'] * 252 / args.portfolio * 100
    ann_vol = s_port['std'] * math.sqrt(252) / args.portfolio * 100
    dd_pct = s_port['max_dd'] / args.portfolio * 100
    print(f"Annualized: return ≈ {ann_ret:+.2f}%  vol ≈ {ann_vol:.2f}%  max DD ≈ {dd_pct:.2f}%")

    # ===== Per-strategy correlations =====
    print(f"\n{'='*90}")
    print(f"Daily P&L correlations between strategies")
    print(f"{'='*90}")
    union = sorted(set().union(*(d.keys() for d in strat_daily.values())))
    series = {s: [strat_daily[s].get(d, 0.0) for d in union] for s in STRATEGIES}
    print(f"{'':>14}" + "".join(f"{s:>14}" for s in STRATEGIES))
    for s1 in STRATEGIES:
        row = f"{s1:>14}"
        for s2 in STRATEGIES:
            if s1 == s2:
                row += f"{'1.000':>14}"
            else:
                row += f"{correlation(series[s1], series[s2]):>+14.3f}"
        print(row)

    # ===== Yearly =====
    print(f"\n{'='*90}")
    print(f"Yearly portfolio P&L by strategy")
    print(f"{'='*90}")
    yearly: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in STRATEGIES:
        for d, v in strat_daily[s].items():
            yearly[d.year][s] += v
    for d, v in portfolio_daily.items():
        yearly[d.year]["TOTAL"] += v
    print(f"{'year':<6}" + "".join(f"{s:>14}" for s in STRATEGIES) + f"{'TOTAL':>14}")
    for y in sorted(yearly):
        row = f"{y:<6}"
        for s in STRATEGIES:
            row += f"{yearly[y][s]:>+14,.0f}"
        row += f"{yearly[y]['TOTAL']:>+14,.0f}"
        print(row)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
