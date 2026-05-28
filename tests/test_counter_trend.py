from datetime import datetime, timedelta, timezone

from trend.counter_trend import CounterTrendConfig, CounterTrendStrategy, State
from trend.sim_broker import SimBroker
from trend.types import Bar


def _series(prices):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        Bar(ts=start + timedelta(days=i), open=p, high=p + 0.5, low=p - 0.5, close=p)
        for i, p in enumerate(prices)
    ]


def _run(bars, cfg=None):
    broker = SimBroker(point_value=5.0, commission_per_contract=0.0)
    strat = CounterTrendStrategy(broker, cfg or CounterTrendConfig(point_value=5.0))
    for b in bars:
        broker.on_bar(b)
        strat.on_bar(b)
    return strat, broker


def test_warmup_no_trades():
    strat, broker = _run(_series([100.0] * 80))
    assert strat.state is State.FLAT
    assert len(strat.trades) == 0


def test_enters_on_pullback_in_bull_market():
    # Build bull regime (EMA40 > EMA80) — long uptrend
    prices = [100.0]
    for i in range(120):
        prices.append(prices[-1] + 0.5)  # steady climb to ~160
    # Hold near top for a bit so 20-day max is well established
    prices.extend([160.0 + (i % 3) * 0.1 for i in range(15)])
    # Now a sharp pullback — drop ~6 points in a few days (should exceed 3 std)
    prices.extend([158.0, 154.0, 150.0, 147.0, 144.0])
    strat, broker = _run(_series(prices))
    # Should have at least entered (may not have exited yet)
    assert strat.entry_price is not None or len(strat.trades) >= 1


def test_no_entry_in_bear_market():
    # EMA40 < EMA80 — bear regime
    prices = [200.0]
    for i in range(120):
        prices.append(prices[-1] - 0.5)
    # Sharp drop further (would be a pullback in a bull market, but we're bearish)
    prices.extend([prices[-1] - i for i in range(10)])
    strat, broker = _run(_series(prices))
    # Should never enter (long-only model, bear regime)
    assert len(strat.trades) == 0
    assert broker.position_qty == 0
