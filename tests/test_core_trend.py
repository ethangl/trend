from datetime import datetime, timedelta, timezone

from trend.core_trend import CoreTrendConfig, CoreTrendStrategy, State
from trend.sim_broker import SimBroker
from trend.types import Bar, Side


def _b(t: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=t, open=o, high=h, low=l, close=c)


def _series(prices: list[float]) -> list[Bar]:
    """One daily bar per close. Open/high/low set near close for simplicity."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i, p in enumerate(prices):
        ts = start + timedelta(days=i)
        bars.append(_b(ts, p, p + 0.5, p - 0.5, p))
    return bars


def _run(bars: list[Bar], cfg: CoreTrendConfig | None = None):
    broker = SimBroker(point_value=5.0, commission_per_contract=0.0)
    strat = CoreTrendStrategy(broker, cfg or CoreTrendConfig(point_value=5.0))
    for b in bars:
        broker.on_bar(b)
        strat.on_bar(b)
    return strat, broker


def test_warmup_no_trades():
    # 50 flat days — not enough history (need 80+ for slow EMA + 50 breakout).
    strat, broker = _run(_series([100.0] * 50))
    assert strat.state is State.FLAT
    assert broker.position_qty == 0
    assert len(strat.trades) == 0


def test_long_breakout_after_warmup():
    # 100 days flat at 100, then strong uptrend to push close above prior 50-day max.
    prices = [100.0] * 100
    # Slow ramp so EMA40 climbs above EMA80 (trend filter flips up) before the breakout
    for i in range(60):
        prices.append(100.0 + i * 0.5)  # gentle climb
    # Then a clear breakout day
    prices.append(150.0)
    # Then keep going up to ride the trend
    for i in range(30):
        prices.append(150.0 + i * 1.0)

    strat, broker = _run(_series(prices))
    # Should have at least one entry executed
    assert strat.entry_price is not None or len(strat.trades) > 0


def test_trailing_stop_exits_long():
    # Build up, enter long, then drop hard enough that 3 std drawdown triggers exit
    prices = [100.0] * 100
    for i in range(60):
        prices.append(100.0 + i * 0.5)
    prices.append(150.0)        # breakout
    for i in range(20):
        prices.append(150.0 + i * 0.5)  # keep climbing past entry
    # Now collapse
    for i in range(20):
        prices.append(160.0 - i * 2.0)  # rapid drop

    strat, broker = _run(_series(prices))
    # By the end, position should be flat (either via trail or trend flip)
    assert broker.position_qty == 0
    # And we should have at least one closed trade
    assert len(strat.trades) >= 1
