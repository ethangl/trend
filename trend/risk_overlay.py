"""Portfolio-level risk overlay: instrument-diversification multiplier (IDM) and
a causal volatility-target scaler.

These are the two portfolio-level levers the per-cell Clenow strategies lack.
Each cell sizes itself independently to a fixed per-position risk (Clenow's
volatility parity, ~0.10% daily impact). That leaves two things on the table:

  1. Diversification (IDM). A basket of N imperfectly-correlated cells has a
     portfolio volatility far below the sum of the cells' volatilities. The IDM
     is the factor by which you can scale *every* position up and still sit at
     the same portfolio risk you'd have from a single cell. For equal risk
     weights and an average pairwise correlation rho-bar,

         IDM = 1 / sqrt( (1/N) + (1 - 1/N) * rho_bar )

     (= sqrt(N) when uncorrelated, 1 when perfectly correlated). Carver caps it
     around 2.5.

  2. Volatility targeting (overlay). Realized portfolio vol drifts with the
     regime and with how many cells are live. A causal scaler nudges positions
     so realized vol tracks a chosen target:

         scaler_t = clamp( target_vol / underlying_vol_{t-1}, 0, leverage_cap )

     The trailing vol must be the *underlying* (multiplier-1) return vol, not the
     vol of the already-overlaid stream. Targeting off the overlaid stream chases
     its own tail and lands at sqrt(target * base_vol) — a geometric-mean
     undershoot. Live, you recover the underlying vol by de-levering the observed
     returns by the multiplier you were running.

Everything here is pure Python and causal: a value applied to day t is computed
only from returns strictly before t. No look-ahead.
"""
from __future__ import annotations

import math

TRADING_DAYS = 252


def ewma_vol(
    returns: list[float],
    span: int,
    min_periods: int = 40,
    trading_days: int = TRADING_DAYS,
) -> list[float | None]:
    """Causal trailing annualized volatility, one value per return.

    ``out[i]`` is the annualized EWMA vol estimated from ``returns[:i]`` only
    (strictly prior days), so it is safe to multiply against ``returns[i]``
    without look-ahead. ``None`` until ``min_periods`` returns are available;
    that seed window is variance-initialized with its sample (population) std,
    after which the standard ``var_t = (1-a) var_{t-1} + a r_t^2`` recursion runs
    with ``a = 2/(span+1)``.
    """
    alpha = 2.0 / (span + 1.0)
    out: list[float | None] = [None] * len(returns)
    var: float | None = None
    seed: list[float] = []
    for i, r in enumerate(returns):
        if var is not None:
            out[i] = math.sqrt(var * trading_days)
        # Fold today's return into the estimate used for *future* days.
        if var is None:
            seed.append(r)
            if len(seed) >= min_periods:
                mean = sum(seed) / len(seed)
                var = sum((x - mean) ** 2 for x in seed) / len(seed)
        else:
            var = (1 - alpha) * var + alpha * r * r
    return out


def pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of two equal-length series, or None if undefined."""
    n = len(a)
    if n != len(b) or n < 2:
        return None
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def average_offdiag_correlation(series: list[list[float]]) -> float:
    """Mean pairwise Pearson correlation across the off-diagonal of the set.

    Undefined pairs (a flat, zero-variance cell) are skipped. Returns 0.0 when
    no pair is estimable.
    """
    n = len(series)
    if n < 2:
        return 0.0
    cors: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            c = pearson(series[i], series[j])
            if c is not None:
                cors.append(c)
    return sum(cors) / len(cors) if cors else 0.0


def idm(avg_corr: float, n: int, cap: float = 2.5, corr_floor: float = 0.0) -> float:
    """Instrument-diversification multiplier for equal risk weights.

    ``avg_corr`` is floored at ``corr_floor`` (default 0) because you cannot bank
    on negative correlations persisting; result is capped at ``cap``.
    """
    if n <= 1:
        return 1.0
    rho = max(avg_corr, corr_floor)
    val = 1.0 / math.sqrt((1.0 / n) + (1.0 - 1.0 / n) * rho)
    return min(val, cap)


def apply_vol_target(
    base_returns: list[float],
    target_ann_vol: float,
    span: int = 63,
    min_periods: int = 40,
    leverage_cap: float = 3.0,
    trading_days: int = TRADING_DAYS,
) -> tuple[list[float], list[float]]:
    """Causal volatility-target overlay applied to a portfolio return stream.

    ``base_returns`` is the underlying (multiplier-1) return stream. The
    multiplier for day t is ``target / trailing_underlying_vol`` measured from
    ``base_returns[:t]`` (strictly prior, no look-ahead), clamped to
    ``[0, leverage_cap]``. During the seed window (before the vol estimate is
    available) the multiplier is 1.0. Returns ``(scaled_returns, multipliers)``.

    Targeting the *underlying* vol (rather than the already-scaled output) is
    what makes realized vol converge to the target instead of to the
    geometric-mean undershoot — see the module docstring.
    """
    vols = ewma_vol(base_returns, span, min_periods, trading_days)
    scaled: list[float] = []
    mults: list[float] = []
    for r, v in zip(base_returns, vols):
        if v is None or v <= 0:
            m = 1.0
        else:
            m = min(target_ann_vol / v, leverage_cap)
        mults.append(m)
        scaled.append(m * r)
    return scaled, mults


class RiskOverlayController:
    """Stateful, causal vol-target overlay for the live loop.

    Each completed session, feed `update()` the portfolio's *underlying*
    (multiplier-1) mark-to-market return for that day. The controller folds it
    into a trailing EWMA vol estimate and returns the risk multiplier to apply to
    subsequent *new entries* (`target / trailing_vol`, clamped to the leverage
    cap). It's causal in the live sense: the multiplier is computed from sessions
    that have closed and applied to entries that haven't happened yet.

    "Underlying" matters: the observed live return is already scaled by whatever
    multiplier was in force when the positions were opened, so the caller must
    de-lever it (divide by the in-force multiplier) before passing it in — see
    the module docstring on why targeting the scaled stream undershoots.

    State is JSON-roundtrippable (`to_dict`/`from_dict`) so the trailing-vol
    estimate survives the loop's restarts instead of cold-starting at 1.0 daily.
    """

    def __init__(self, target_ann_vol: float, span: int = 63,
                 min_periods: int = 40, leverage_cap: float = 3.0,
                 trading_days: int = TRADING_DAYS):
        self.target_ann_vol = target_ann_vol
        self.span = span
        self.min_periods = min_periods
        self.leverage_cap = leverage_cap
        self.trading_days = trading_days
        self._alpha = 2.0 / (span + 1.0)
        self._var: float | None = None
        self._seed: list[float] = []
        self._multiplier = 1.0

    @property
    def multiplier(self) -> float:
        """Current multiplier to feed `Runner.set_risk_multiplier`."""
        return self._multiplier

    @property
    def trailing_vol(self) -> float | None:
        """Current annualized trailing-vol estimate, or None during warmup."""
        if self._var is None:
            return None
        return math.sqrt(self._var * self.trading_days)

    def update(self, underlying_return: float) -> float:
        """Fold one session's underlying return in; return the new multiplier."""
        if self._var is None:
            self._seed.append(underlying_return)
            if len(self._seed) >= self.min_periods:
                mean = sum(self._seed) / len(self._seed)
                self._var = sum((x - mean) ** 2 for x in self._seed) / len(self._seed)
        else:
            self._var = ((1 - self._alpha) * self._var
                         + self._alpha * underlying_return * underlying_return)
        tv = self.trailing_vol
        if tv is None or tv <= 0:
            self._multiplier = 1.0
        else:
            self._multiplier = min(self.target_ann_vol / tv, self.leverage_cap)
        return self._multiplier

    def to_dict(self) -> dict:
        return {
            "target_ann_vol": self.target_ann_vol,
            "span": self.span,
            "min_periods": self.min_periods,
            "leverage_cap": self.leverage_cap,
            "trading_days": self.trading_days,
            "var": self._var,
            "seed": list(self._seed),
            "multiplier": self._multiplier,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RiskOverlayController":
        c = cls(
            target_ann_vol=d["target_ann_vol"],
            span=d.get("span", 63),
            min_periods=d.get("min_periods", 40),
            leverage_cap=d.get("leverage_cap", 3.0),
            trading_days=d.get("trading_days", TRADING_DAYS),
        )
        c._var = d.get("var")
        c._seed = list(d.get("seed", []))
        c._multiplier = d.get("multiplier", 1.0)
        return c


def return_stats(returns: list[float], trading_days: int = TRADING_DAYS) -> dict:
    """Annualized return/vol/Sharpe and max drawdown for a fractional return
    stream sized off a fixed notional (additive P&L, so drawdown is on the
    cumulative sum)."""
    n = len(returns)
    if n < 2:
        return {"ann_ret": 0.0, "ann_vol": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    ann_ret = mean * trading_days
    ann_vol = std * math.sqrt(trading_days)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        eq += r
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return {"ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe, "max_dd": max_dd}
