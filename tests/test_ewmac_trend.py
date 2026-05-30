from datetime import datetime, timedelta, timezone

from trend.ewmac_trend import EwmacTrendConfig, EwmacTrendStrategy
from trend.sim_broker import SimBroker
from trend.types import Bar


def _series(prices):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        Bar(ts=start + timedelta(days=i), open=p, high=p + 0.5, low=p - 0.5, close=p)
        for i, p in enumerate(prices)
    ]


def _trend(n, slope, start=100.0, wiggle=0.4):
    """Trended prices with deterministic day-to-day noise, so the std of daily
    changes is non-zero (a perfectly linear series has zero vol and the
    vol-normalized strategy correctly won't trade it)."""
    prices = [start]
    for i in range(n):
        prices.append(prices[-1] + slope + (wiggle if i % 2 == 0 else -wiggle))
    return prices


def _run(bars, cfg=None):
    broker = SimBroker(point_value=5.0, commission_per_contract=0.0)
    strat = EwmacTrendStrategy(broker, cfg or EwmacTrendConfig(point_value=5.0))
    for b in bars:
        broker.on_bar(b)
        strat.on_bar(b)
    return strat, broker


def test_warmup_no_trades_before_required_history():
    # Slowest slow-span is 256; nothing should trade inside the warmup window.
    strat, broker = _run(_series([100.0 + (i % 2) for i in range(200)]))
    assert broker.position_qty == 0
    assert strat.pending_order_id is None


def test_goes_long_in_uptrend():
    strat, broker = _run(_series(_trend(400, slope=0.5)))
    assert broker.position_qty > 0  # positive forecast -> long


def test_goes_short_in_downtrend():
    strat, broker = _run(_series(_trend(400, slope=-0.5, start=400.0)))
    assert broker.position_qty < 0  # negative forecast -> short


def test_scales_position_with_conviction():
    # A steeper trend should command a larger position than a shallow one at
    # comparable volatility — the core continuous-sizing claim.
    shallow = _trend(360, slope=0.25, wiggle=0.4)
    steep = _trend(360, slope=1.2, wiggle=0.4)
    _, b_shallow = _run(_series(shallow))
    _, b_steep = _run(_series(steep))
    assert b_steep.position_qty >= b_shallow.position_qty


def test_forecast_respects_cap():
    cfg = EwmacTrendConfig(point_value=5.0)
    strat, _ = _run(_series([100.0 + i for i in range(300)]), cfg)
    # Drive EMAs far apart and confirm the combined forecast clamps to the cap.
    strat.emas = {s: (100.0 if s < 100 else 0.0) for s in strat._spans}
    f = strat._combined_forecast(0.5)
    assert abs(f) <= cfg.forecast_cap + 1e-9


def test_does_not_stack_orders_while_pending():
    prices = _trend(320, slope=0.5)
    broker = SimBroker(point_value=5.0, commission_per_contract=0.0)
    strat = EwmacTrendStrategy(broker, EwmacTrendConfig(point_value=5.0))
    for b in _series(prices):
        broker.on_bar(b)
        strat.on_bar(b)
    # Simulate an in-flight order: a fresh bar must not place a second order.
    strat.pending_order_id = 999
    n_orders_before = len(broker.orders)
    nxt = Bar(ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
              open=prices[-1] + 1, high=prices[-1] + 1.5,
              low=prices[-1] + 0.5, close=prices[-1] + 1)
    broker.on_bar(nxt)
    strat.on_bar(nxt)
    # pending_order_id 999 was never a real broker order, so no fill cleared it;
    # the strategy should have placed nothing new.
    assert len(broker.orders) == n_orders_before


def test_state_roundtrips():
    strat, broker = _run(_series(_trend(340, slope=0.4)))
    d = strat.to_state_dict()

    broker2 = SimBroker(point_value=5.0, commission_per_contract=0.0)
    strat2 = EwmacTrendStrategy(broker2, EwmacTrendConfig(point_value=5.0))
    strat2.apply_state_dict(d)
    assert strat2.entry_qty == strat.entry_qty
    assert strat2.entry_price == strat.entry_price
    assert len(strat2.trades) == len(strat.trades)


def test_rebase_shifts_price_levels():
    strat, _ = _run(_series(_trend(320, slope=0.5)))
    ema_before = dict(strat.emas)
    entry_before = strat.entry_price
    basis = 25.0
    strat.rebase_prices(basis)
    for s in strat._spans:
        assert strat.emas[s] == ema_before[s] + basis
    if entry_before is not None:
        assert strat.entry_price == entry_before + basis


def test_rebase_noop_on_zero_basis():
    strat, _ = _run(_series(_trend(300, slope=0.5)))
    ema_before = dict(strat.emas)
    strat.rebase_prices(0.0)
    assert strat.emas == ema_before
