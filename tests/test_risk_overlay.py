"""Tests for the portfolio risk overlay (IDM + causal vol-target)."""
import math

import pytest

from trend import risk_overlay as ro


# ---- ewma_vol: causality + correctness --------------------------------------

def test_ewma_vol_is_none_until_min_periods():
    rets = [0.01] * 10
    out = ro.ewma_vol(rets, span=5, min_periods=4)
    # First 4 estimates unavailable; the rest defined.
    assert out[:4] == [None, None, None, None]
    assert all(v is not None for v in out[4:])


def test_ewma_vol_causal_value_uses_only_prior_returns():
    # The estimate at index i must not depend on returns[i:]. Mutate a later
    # return and confirm earlier estimates are unchanged.
    base = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.0, 0.03]
    a = ro.ewma_vol(base, span=4, min_periods=3)
    mutated = list(base)
    mutated[6] = 5.0  # huge spike late in the series
    b = ro.ewma_vol(mutated, span=4, min_periods=3)
    assert a[:7] == b[:7]  # estimates for days 0..6 (using returns <6) unchanged


def test_ewma_vol_constant_returns_seed():
    # With a constant return the seed variance is 0, so vol is ~0.
    out = ro.ewma_vol([0.01] * 6, span=3, min_periods=3)
    assert out[3] == pytest.approx(0.0)


def test_ewma_vol_annualizes():
    # Constant magnitude alternating returns -> known daily std, annualized.
    rets = [0.01, -0.01] * 50
    out = ro.ewma_vol(rets, span=10, min_periods=10, trading_days=252)
    # daily std ~ 0.01, annualized ~ 0.01*sqrt(252)
    assert out[-1] == pytest.approx(0.01 * math.sqrt(252), rel=0.15)


# ---- correlation + IDM ------------------------------------------------------

def test_pearson_perfect_and_anti():
    a = [1.0, 2.0, 3.0, 4.0]
    assert ro.pearson(a, a) == pytest.approx(1.0)
    assert ro.pearson(a, [-x for x in a]) == pytest.approx(-1.0)


def test_pearson_flat_series_is_none():
    assert ro.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


def test_average_offdiag_skips_undefined_pairs():
    s1 = [1.0, 2.0, 3.0]
    s2 = [3.0, 2.0, 1.0]
    flat = [0.0, 0.0, 0.0]  # zero-variance -> undefined corr, skipped
    avg = ro.average_offdiag_correlation([s1, s2, flat])
    assert avg == pytest.approx(-1.0)  # only the s1/s2 pair counts


def test_idm_uncorrelated_is_sqrt_n():
    assert ro.idm(0.0, 4, cap=10.0) == pytest.approx(2.0)


def test_idm_perfectly_correlated_is_one():
    assert ro.idm(1.0, 8, cap=10.0) == pytest.approx(1.0)


def test_idm_respects_cap_and_corr_floor():
    assert ro.idm(0.0, 100, cap=2.5) == pytest.approx(2.5)
    # Negative correlation is floored to 0 -> behaves like uncorrelated.
    assert ro.idm(-0.5, 4, cap=10.0) == pytest.approx(2.0)


def test_idm_single_instrument():
    assert ro.idm(0.3, 1) == 1.0


# ---- vol-target overlay -----------------------------------------------------

def test_apply_vol_target_seed_is_unscaled():
    rets = [0.01, -0.01, 0.02, -0.02, 0.01]
    scaled, mults = ro.apply_vol_target(rets, 0.10, span=3, min_periods=4)
    # First min_periods multipliers are 1.0 (no estimate yet).
    assert mults[:4] == [1.0, 1.0, 1.0, 1.0]
    assert scaled[:4] == rets[:4]


def test_apply_vol_target_scales_low_vol_up():
    # Low, steady vol well under target -> overlay levers up (toward the cap).
    rets = [0.001, -0.001] * 100
    _, mults = ro.apply_vol_target(rets, 0.20, span=20, leverage_cap=5.0)
    assert mults[-1] > 1.5


def test_apply_vol_target_caps_leverage():
    rets = [0.0001, -0.0001] * 100  # almost no vol -> wants huge leverage
    _, mults = ro.apply_vol_target(rets, 0.20, span=20, leverage_cap=3.0)
    assert max(mults) <= 3.0


def test_apply_vol_target_brings_realized_vol_toward_target():
    # A stationary stream overlaid to a target should realize close to it.
    import random
    rng = random.Random(42)
    rets = [rng.gauss(0, 0.005) for _ in range(2000)]  # ~0.005 daily, ~8% ann
    scaled, _ = ro.apply_vol_target(rets, 0.15, span=63, leverage_cap=10.0)
    realized = ro.return_stats(scaled[200:])["ann_vol"]  # skip seed/ramp
    assert realized == pytest.approx(0.15, rel=0.25)


def test_apply_vol_target_causal():
    base = [0.01, -0.01, 0.02, -0.015, 0.005, -0.02, 0.01, 0.03]
    a_scaled, a_mults = ro.apply_vol_target(base, 0.10, span=4, min_periods=3)
    mutated = list(base)
    mutated[6] = 9.0
    b_scaled, b_mults = ro.apply_vol_target(mutated, 0.10, span=4, min_periods=3)
    # Multiplier for day i uses only scaled returns < i, which depend only on
    # base returns < i -> days 0..6 identical despite the spike at index 6.
    assert a_mults[:7] == b_mults[:7]
    assert a_scaled[:6] == b_scaled[:6]


# ---- return_stats -----------------------------------------------------------

def test_return_stats_zero_for_trivial():
    assert ro.return_stats([])["sharpe"] == 0.0
    assert ro.return_stats([0.01])["ann_vol"] == 0.0


def test_return_stats_max_dd():
    # +1, +1, -3, +1 cumulative: peak 2 at index1, trough -1 at index2 -> dd 3.
    stats = ro.return_stats([1.0, 1.0, -3.0, 1.0])
    assert stats["max_dd"] == pytest.approx(3.0)


def test_return_stats_sharpe_sign():
    up = ro.return_stats([0.01, 0.012, 0.009, 0.011, 0.008])
    assert up["sharpe"] > 0


# ---- RiskOverlayController (stateful live engine) ---------------------------

def test_controller_stays_at_one_during_warmup():
    c = ro.RiskOverlayController(0.10, span=5, min_periods=4)
    for _ in range(3):
        assert c.update(0.005) == 1.0
    assert c.multiplier == 1.0
    assert c.trailing_vol is None


def test_controller_targets_vol_after_warmup():
    c = ro.RiskOverlayController(0.10, span=20, min_periods=10, leverage_cap=10.0)
    # Steady |r| = 0.002 -> EWMA(r^2) settles at 0.002^2, so trailing ann vol
    # converges to 0.002*sqrt(252) ~ 3.2%, well under the 10% target -> lever up.
    for _ in range(80):
        c.update(0.002)
    expected = 0.10 / (0.002 * math.sqrt(252))
    assert c.multiplier == pytest.approx(expected, rel=0.05)
    assert 2.5 < c.multiplier < 4.0


def test_controller_levers_down_on_high_vol():
    c = ro.RiskOverlayController(0.10, span=20, min_periods=10, leverage_cap=3.0)
    import random
    rng = random.Random(7)
    for _ in range(300):
        c.update(rng.gauss(0, 0.03))  # ~0.03 daily ~ 48% ann, well over 10% target
    assert c.multiplier < 1.0  # de-levers a too-hot book


def test_controller_respects_leverage_cap():
    c = ro.RiskOverlayController(0.50, span=10, min_periods=5, leverage_cap=2.0)
    for _ in range(40):
        c.update(0.0001)
    assert c.multiplier <= 2.0


def test_controller_roundtrips_through_dict():
    c = ro.RiskOverlayController(0.10, span=15, min_periods=8, leverage_cap=2.5)
    import random
    rng = random.Random(1)
    for _ in range(50):
        c.update(rng.gauss(0, 0.006))
    restored = ro.RiskOverlayController.from_dict(c.to_dict())
    assert restored.multiplier == c.multiplier
    assert restored.trailing_vol == c.trailing_vol
    # Both advance identically from the restored point.
    assert restored.update(0.01) == c.update(0.01)
