#!/usr/bin/env python3
"""
Volatility-normalized EWMAC trend-following sleeve, diversified and walk-forward tested.

Pipeline
--------
1. Download a candidate ETF universe spanning equities, rates, credit, commodities,
   FX and real estate.
2. Curate a low-redundancy subset via greedy max-min diversification on the return
   correlation matrix (reduces wasted, correlated exposure).
3. For each market, build a combined EWMAC forecast from several speeds, each
   volatility-normalized so the signal is comparable across markets and regimes.
4. Estimate the forecast scalars, forecast-diversification multiplier (FDM) and
   instrument-diversification multiplier (IDM) WALK-FORWARD: at every annual
   rebalance they are re-fit on data available up to that date only.
5. Size each market to a volatility target, aggregate into one portfolio, and apply
   a causal volatility-target overlay so realized vol tracks the target.
6. Blend the sleeve with SPY, choosing the allocation each year on past data only,
   and report out-of-sample performance.

This is research code, not investment advice. A backtest is a necessary but not
sufficient condition for forward edge.

Method follows the systematic-trading framework popularized by Robert Carver
(*Systematic Trading*, *Leveraged Trading*).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # Candidate universe to curate from (broad, deliberately redundant).
    candidates: tuple[str, ...] = (
        "SPY", "EFA", "EEM", "EWJ", "IWM",          # equity
        "TLT", "IEF", "SHY", "TIP", "LQD", "HYG",   # rates + credit
        "GLD", "SLV", "DBC", "USO", "UNG", "DBA",   # commodities
        "FXE", "UUP", "FXY", "FXB",                 # FX
        "VNQ",                                      # real estate
    )
    curated_size: int = 12                  # markets to keep after de-duplication
    benchmark: str = "SPY"                  # blended against / reported alongside

    # EWMAC speeds as (fast_span, slow_span) pairs.
    rules: tuple[tuple[int, int], ...] = ((8, 32), (16, 64), (32, 128), (64, 256))

    vol_span: int = 35                      # EWMA span for return-volatility estimate
    forecast_cap: float = 20.0             # forecast clamped to +/- this (avg |f| ~ 10)
    ann_vol_target: float = 0.20            # target annualized volatility of the sleeve
    overlay_span: int = 63                  # span for the realized-vol overlay
    overlay_leverage_cap: float = 3.0       # max leverage from the vol overlay
    cost_bps: float = 2.0                   # round-trip cost in bps of traded notional
    idm_cap: float = 2.5                    # cap on diversification multipliers

    min_active_markets: int = 6             # sleeve only runs once this many are live
    burn_in_year: int = 2010                # last year used only to seed estimates
    blend_grid: np.ndarray = field(
        default_factory=lambda: np.linspace(0, 1, 21)
    )

    @property
    def daily_vol_target(self) -> float:
        return self.ann_vol_target / np.sqrt(TRADING_DAYS)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def download_prices(tickers, start="1990-01-01") -> pd.DataFrame:
    """Adjusted close prices (dividends/splits handled) for the given tickers."""
    import yfinance as yf

    out = {}
    for t in tickers:
        s = yf.download(t, start=start, auto_adjust=True, progress=False)["Close"]
        out[t] = s.iloc[:, 0] if hasattr(s, "columns") else s
    return pd.DataFrame(out).sort_index()


# --------------------------------------------------------------------------- #
# Universe curation
# --------------------------------------------------------------------------- #
def effective_bets(corr: np.ndarray) -> float:
    """Participation ratio of the correlation matrix eigenvalues.

    Equals N for uncorrelated assets, 1 for perfectly correlated ones; a measure
    of how many genuinely independent bets a universe contains.
    """
    w = np.linalg.eigvalsh(corr)
    w = w[w > 0]
    return float((w.sum() ** 2) / (w ** 2).sum())


def curate_universe(prices: pd.DataFrame, size: int, anchor: str) -> list[str]:
    """Greedy max-min diversification: starting from `anchor`, repeatedly add the
    market whose largest absolute correlation to the chosen set is smallest."""
    corr = prices.pct_change().dropna().corr()
    chosen = [anchor]
    candidates = [c for c in corr.columns if c != anchor]
    while len(chosen) < size and candidates:
        nxt = min(candidates, key=lambda c: corr.loc[c, chosen].abs().max())
        chosen.append(nxt)
        candidates.remove(nxt)
    return chosen


# --------------------------------------------------------------------------- #
# Per-instrument signal construction (all causal)
# --------------------------------------------------------------------------- #
def daily_vol(price: pd.Series, span: int) -> pd.Series:
    """EWMA estimate of daily return volatility."""
    return np.sqrt((price.pct_change() ** 2).ewm(span=span, min_periods=20).mean())


def raw_forecasts(prices: pd.DataFrame, cfg: Config):
    """For each rule, the volatility-normalized (pre-scalar) EWMAC forecast per market,
    plus the standardized-return and daily-vol panels used later for sizing."""
    raw = {rule: {} for rule in cfg.rules}
    std_ret, vols = {}, {}
    for t in prices.columns:
        p = prices[t].dropna()
        vd = daily_vol(p, cfg.vol_span)
        vols[t] = vd
        std_ret[t] = p.pct_change() / vd.shift(1)        # ~unit-vol return
        vol_price = vd * p                               # $ vol per share per day
        for fast, slow in cfg.rules:
            ewmac = p.ewm(span=fast).mean() - p.ewm(span=slow).mean()
            raw[(fast, slow)][t] = ewmac / vol_price
    raw = {rule: pd.DataFrame(raw[rule]) for rule in cfg.rules}
    return raw, pd.DataFrame(std_ret), pd.DataFrame(vols)


# --------------------------------------------------------------------------- #
# Walk-forward estimation helpers
# --------------------------------------------------------------------------- #
def _year_ends(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    years = range(index[0].year, index[-1].year + 1)
    return [pd.Timestamp(f"{y}-12-31") for y in years]


def walk_forward_scalars_and_fdm(raw, cfg: Config):
    """Expanding annual estimates of (a) one forecast scalar per rule, set so the
    average absolute raw forecast equals 10, and (b) the forecast-diversification
    multiplier from the correlation between rules. Each estimate uses only data up
    to the prior year-end, then applies for the following year (no look-ahead)."""
    index = raw[cfg.rules[0]].index
    rules = list(cfg.rules)
    scalar_ser = {r: pd.Series(index=index, dtype=float) for r in rules}
    fdm_ser = pd.Series(index=index, dtype=float)

    cur_scalar = {r: np.nan for r in rules}
    cur_fdm = np.nan
    prev = index[0]
    schedule = _year_ends(index)

    for i, ye in enumerate(schedule):
        mask = (index <= ye) if i == 0 else ((index > prev) & (index <= ye))
        for r in rules:
            scalar_ser[r][mask] = cur_scalar[r]
        fdm_ser[mask] = cur_fdm

        upto = index <= ye
        for r in rules:
            vals = raw[r][upto].abs().values.flatten()
            vals = vals[~np.isnan(vals)]
            if len(vals) > TRADING_DAYS:
                cur_scalar[r] = 10.0 / np.nanmean(vals)
        if not any(np.isnan(v) for v in cur_scalar.values()):
            scaled = {r: (raw[r][upto] * cur_scalar[r]).clip(-cfg.forecast_cap, cfg.forecast_cap)
                      for r in rules}
            rule_means = pd.DataFrame({str(r): scaled[r].mean(axis=1) for r in rules}).dropna()
            if len(rule_means) > TRADING_DAYS:
                c = rule_means.corr().values
                w = np.ones(len(rules)) / len(rules)
                cur_fdm = min(1.0 / np.sqrt(w @ c @ w), cfg.idm_cap)
        prev = ye

    tail = index > schedule[-1]
    for r in rules:
        scalar_ser[r][tail] = cur_scalar[r]
    fdm_ser[tail] = cur_fdm
    return scalar_ser, fdm_ser


def combined_forecast(raw, scalar_ser, fdm_ser, cfg: Config) -> pd.DataFrame:
    """Average the scaled, capped rule forecasts and apply the FDM, re-capping."""
    cap = cfg.forecast_cap
    scaled = {r: raw[r].mul(scalar_ser[r], axis=0).clip(-cap, cap) for r in cfg.rules}
    combined = sum(scaled.values()) / len(cfg.rules)
    return combined.mul(fdm_ser, axis=0).clip(-cap, cap)


def subsystem_returns(forecast, std_ret, vols, cfg: Config):
    """Per-market return of trading that market alone at the vol target, net of costs."""
    pos = forecast.shift(1) / 10.0                       # risk units, lagged (no look-ahead)
    gross = pos * std_ret * cfg.daily_vol_target
    notional = (forecast / 10.0) * cfg.daily_vol_target / vols
    cost = notional.diff().abs() * (cfg.cost_bps / 1e4)
    net = gross - cost
    active = forecast.shift(1).notna() & std_ret.notna()
    return net, active


def walk_forward_idm(sub, active, cfg: Config) -> pd.Series:
    """Expanding annual instrument-diversification multiplier from the average
    correlation between subsystem returns, combined with the live-market count."""
    index = sub.index
    idm_corr = pd.Series(index=index, dtype=float)
    cur = np.nan
    prev = index[0]
    for ye in _year_ends(index) + [index[-1]]:
        mask = (index > prev) & (index <= ye)
        idm_corr[mask] = cur
        hist = sub[index <= ye].dropna(how="all")
        if len(hist) > TRADING_DAYS:
            c = hist.corr().clip(lower=0).values
            n = c.shape[0]
            cur = max((c.sum() - n) / (n * (n - 1)), 0.01)
        prev = ye
    n_active = active.sum(axis=1)
    idm = 1.0 / np.sqrt((1 / n_active) + (1 - 1 / n_active) * idm_corr)
    return idm.clip(upper=cfg.idm_cap), n_active


def assemble_sleeve(sub, active, idm, n_active, cfg: Config) -> pd.Series:
    """Equal-risk-weighted portfolio of active markets, scaled by the IDM, then put
    through a causal volatility-target overlay. Only runs once the basket holds at
    least `min_active_markets` live markets, so the sleeve reflects the diversified
    system rather than a single-market stub from early history."""
    live = n_active >= cfg.min_active_markets
    raw_port = ((sub.where(active).sum(axis=1) / n_active.replace(0, np.nan) * idm)
                .where(live).dropna())
    trailing = raw_port.ewm(span=cfg.overlay_span, min_periods=40).std() * np.sqrt(TRADING_DAYS)
    scaler = (cfg.ann_vol_target / trailing.shift(1)).clip(upper=cfg.overlay_leverage_cap)
    return (raw_port * scaler).dropna(), raw_port


# --------------------------------------------------------------------------- #
# Walk-forward blend with the benchmark
# --------------------------------------------------------------------------- #
def sharpe(returns: pd.Series) -> float:
    r = returns.dropna()
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return r.mean() * TRADING_DAYS / (r.std() * np.sqrt(TRADING_DAYS))


def walk_forward_blend(sleeve, bench_ret, cfg: Config):
    """Each year, pick the sleeve/benchmark mix that maximized Sharpe on all prior
    data, then apply it out-of-sample for that year."""
    weights = pd.Series(index=sleeve.index, dtype=float)
    first_oos = cfg.burn_in_year + 1
    last_year = sleeve.index[-1].year
    for y in range(first_oos, last_year + 1):
        cutoff = pd.Timestamp(f"{y - 1}-12-31")
        rs, rb = sleeve[:cutoff], bench_ret[:cutoff]
        j = rs.dropna().index.intersection(rb.dropna().index)
        best = max(cfg.blend_grid, key=lambda w: sharpe(w * rs[j] + (1 - w) * rb[j]))
        ymask = (sleeve.index >= pd.Timestamp(f"{y}-01-01")) & \
                (sleeve.index <= pd.Timestamp(f"{y}-12-31"))
        weights[ymask] = best
    weights = weights.dropna()
    rs = sleeve.reindex(weights.index)
    rb = bench_ret.reindex(weights.index)
    blend = (weights * rs + (1 - weights) * rb).dropna()
    return blend, weights


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def performance(returns: pd.Series) -> dict:
    r = returns.dropna()
    equity = (1 + r).cumprod()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    return {
        "CAGR": equity.iloc[-1] ** (1 / years) - 1,
        "Vol": r.std() * np.sqrt(TRADING_DAYS),
        "Sharpe": sharpe(r),
        "MaxDD": (equity / equity.cummax() - 1).min(),
    }


def print_table(rows: dict[str, pd.Series]):
    print(f"{'':30s}{'CAGR':>8s}{'Vol':>7s}{'Sharpe':>8s}{'MaxDD':>8s}")
    for label, r in rows.items():
        s = performance(r)
        print(f"{label:30s}{s['CAGR']*100:7.1f}%{s['Vol']*100:6.1f}%"
              f"{s['Sharpe']:8.2f}{s['MaxDD']*100:7.1f}%")


def plot_results(sleeve, bench_ret, blend, weights, cfg: Config, path: str):
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    oos = blend.index
    eq = pd.DataFrame({
        "Trend sleeve": (1 + sleeve.reindex(oos)).cumprod() * 1e5,
        cfg.benchmark: (1 + bench_ret.reindex(oos)).cumprod() * 1e5,
        "WF blend": (1 + blend).cumprod() * 1e5,
    })
    colors = {"Trend sleeve": "#b07aa1", cfg.benchmark: "#111111", "WF blend": "#2e7d32"}

    fig = plt.figure(figsize=(13, 10))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.4, 1], hspace=0.3, wspace=0.22)
    ax1, ax2, ax3 = fig.add_subplot(gs[0, :]), fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])

    for c in eq.columns:
        ax1.plot(eq.index, eq[c], label=c, color=colors[c],
                 lw=2.6 if c == "WF blend" else 2.0, ls="--" if c == cfg.benchmark else "-")
    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax1.set_title("Walk-forward out-of-sample: diversified L/S EWMAC",
                  fontweight="bold", loc="left", fontsize=12)
    ax1.legend(loc="upper left", fontsize=10)
    ax1.grid(True, which="both", alpha=0.18)
    ax1.set_ylabel("Growth of $100k (log)")

    ax2.step(weights.index, weights * 100, where="post", color="#2e7d32", lw=2)
    ax2.fill_between(weights.index, weights * 100, step="post", alpha=0.15, color="#2e7d32")
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("% to trend sleeve")
    ax2.set_title("WF blend weight (re-chosen yearly on past data)", fontsize=10, loc="left")
    ax2.grid(True, alpha=0.18)

    dd = lambda s: (s / s.cummax() - 1) * 100
    ax3.fill_between(eq.index, dd(eq["WF blend"]), 0, color="#2e7d32", alpha=0.35, label="WF blend")
    ax3.plot(eq.index, dd(eq[cfg.benchmark]), color="#111", lw=1.3, label=cfg.benchmark)
    ax3.set_ylabel("Drawdown %")
    ax3.set_title("Blend drawdown vs benchmark", fontsize=10, loc="left")
    ax3.legend(fontsize=9, loc="lower left")
    ax3.grid(True, alpha=0.18)

    fig.savefig(path, dpi=130, bbox_inches="tight")
    print(f"\nSaved chart -> {path}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(cfg: Config, prices: pd.DataFrame | None = None, plot_path: str | None = None):
    if prices is None:
        print("Downloading candidate universe ...")
        prices = download_prices(cfg.candidates)

    universe = curate_universe(prices, cfg.curated_size, cfg.benchmark)
    common = prices[cfg.candidates_present(prices)].pct_change().dropna()
    full_corr = common.corr().values
    cur_corr = prices[universe].pct_change().dropna().corr().values
    print(f"\nCurated universe ({len(universe)}): {universe}")
    print(f"Effective bets:  full {prices.shape[1]}-mkt = {effective_bets(full_corr):.1f}"
          f"  |  curated {len(universe)}-mkt = {effective_bets(cur_corr):.1f}")

    px = prices[universe]
    raw, std_ret, vols = raw_forecasts(px, cfg)
    scalar_ser, fdm_ser = walk_forward_scalars_and_fdm(raw, cfg)
    forecast = combined_forecast(raw, scalar_ser, fdm_ser, cfg)
    sub, active = subsystem_returns(forecast, std_ret, vols, cfg)
    idm, n_active = walk_forward_idm(sub, active, cfg)
    sleeve, _ = assemble_sleeve(sub, active, idm, n_active, cfg)

    bench_ret = px[cfg.benchmark].pct_change().reindex(sleeve.index)
    blend, weights = walk_forward_blend(sleeve, bench_ret, cfg)

    oos = blend.index
    print(f"\nWalk-forward OUT-OF-SAMPLE  {oos[0].date()} -> {oos[-1].date()}"
          f"  (avg weight to trend = {weights.mean():.0%}, range "
          f"{weights.min():.0%}-{weights.max():.0%})")
    print_table({
        "Trend sleeve (WF)": sleeve.reindex(oos),
        "WF blend": blend,
        f"{cfg.benchmark} buy & hold": bench_ret.reindex(oos),
    })
    corr = np.corrcoef(sleeve.reindex(oos).dropna(),
                       bench_ret.reindex(sleeve.reindex(oos).dropna().index))[0, 1]
    print(f"\nSleeve / {cfg.benchmark} correlation (OOS): {corr:.2f}")

    if plot_path:
        plot_results(sleeve, bench_ret, blend, weights, cfg, plot_path)
    return dict(universe=universe, sleeve=sleeve, blend=blend, weights=weights,
                bench=bench_ret)


# small helper so run() works whether or not every candidate downloaded
def _candidates_present(self: Config, prices: pd.DataFrame):
    return [c for c in self.candidates if c in prices.columns]
Config.candidates_present = _candidates_present


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plot", metavar="PATH", default=None,
                    help="save the results chart to this path")
    ap.add_argument("--cache", metavar="CSV", default=None,
                    help="load/save downloaded prices to this CSV to avoid re-downloading")
    args = ap.parse_args()

    cfg = Config()
    prices = None
    if args.cache:
        import os
        if os.path.exists(args.cache):
            prices = pd.read_csv(args.cache, parse_dates=[0], index_col=0).sort_index()
        else:
            prices = download_prices(cfg.candidates)
            prices.to_csv(args.cache)

    run(cfg, prices=prices, plot_path=args.plot)


if __name__ == "__main__":
    main()
