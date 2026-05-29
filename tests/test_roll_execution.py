"""Roll execution: IBBroker position-neutral swap, strategy price rebasing,
and the Runner helpers that gate a roll (defer while mid-order)."""
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import pytest

from trend.core_trend import CoreTrendStrategy
from trend.core_trend import State as CoreState
from trend.counter_trend import CounterTrendStrategy
from trend.ib_broker import IBBroker, RollExecutionError, RollResult
from trend.runner import Cell, CellSetup, Runner
from trend.sim_broker import SimBroker
from trend.time_return import TimeReturnStrategy


# ---- FakeIB: synchronous fills on sleep() --------------------------------

class _FillEvent:
    def __init__(self):
        self._cbs = []

    def __iadd__(self, cb):
        self._cbs.append(cb)
        return self

    def fire(self, trade, fill):
        for cb in list(self._cbs):
            cb(trade, fill)


class _Trade:
    def __init__(self, contract, order):
        self.contract = contract
        self.order = order
        self.fillEvent = _FillEvent()
        self._done = False

    def isDone(self):
        return self._done


class FakeIB:
    """Fills every pending MARKET order on the next sleep(), consuming prices
    from `prices` in order. `max_fills` caps how many fills it will ever emit
    (to simulate a leg that doesn't fill — a half-roll)."""

    def __init__(self, prices, max_fills=None):
        self.prices = list(prices)
        self.max_fills = max_fills
        self._fired = 0
        self._pending: list[_Trade] = []

    def placeOrder(self, contract, order):
        t = _Trade(contract, order)
        self._pending.append(t)
        return t

    def sleep(self, secs):
        pending, self._pending = self._pending, []
        for t in pending:
            if self.max_fills is not None and self._fired >= self.max_fills:
                continue
            price = self.prices.pop(0) if self.prices else 0.0
            side = "BOT" if t.order.action == "BUY" else "SLD"
            execu = SimpleNamespace(
                time=datetime(2026, 6, 18, 18, 15),
                price=price, shares=t.order.totalQuantity, side=side,
            )
            t._done = True
            self._fired += 1
            t.fillEvent.fire(t, SimpleNamespace(execution=execu))

    def cancelOrder(self, order):
        pass


def _contract(label):
    return SimpleNamespace(localSymbol=label, conId=hash(label) & 0xFFFF)


# ---- IBBroker.roll_to ----------------------------------------------------

def test_roll_to_long_is_position_neutral():
    ib = FakeIB(prices=[105.0, 106.0])
    old, new = _contract("MBTM6"), _contract("MBTN6")
    b = IBBroker(ib, old, point_value=0.10)
    b.position_qty = 2
    b.position_avg = 100.0

    res = b.roll_to(new)

    assert isinstance(res, RollResult)
    assert res.qty == 2
    assert b.position_qty == 2          # net position preserved
    assert b.contract is new            # now trades the new contract
    assert (res.close_price, res.open_price) == (105.0, 106.0)
    assert res.basis == pytest.approx(1.0)
    assert b.position_avg == pytest.approx(106.0)  # re-based to new contract


def test_roll_to_short_is_position_neutral():
    ib = FakeIB(prices=[90.0, 89.0])
    b = IBBroker(ib, _contract("MESM6"), point_value=5.0)
    b.position_qty = -3
    b.position_avg = 95.0

    res = b.roll_to(_contract("MESU6"))

    assert res.qty == -3
    assert b.position_qty == -3
    assert res.basis == pytest.approx(-1.0)


def test_roll_to_flat_just_swaps_contract():
    ib = FakeIB(prices=[])
    new = _contract("MESU6")
    b = IBBroker(ib, _contract("MESM6"), point_value=5.0)

    res = b.roll_to(new)

    assert res.qty == 0
    assert b.contract is new
    assert b.fills == []


def test_roll_to_raises_when_close_does_not_fill():
    ib = FakeIB(prices=[105.0], max_fills=0)  # nothing fills
    b = IBBroker(ib, _contract("MBTM6"), point_value=0.10)
    b.position_qty = 1
    b.position_avg = 100.0

    with pytest.raises(RollExecutionError):
        b.roll_to(_contract("MBTN6"))


def test_roll_to_raises_when_reopen_does_not_fill():
    ib = FakeIB(prices=[105.0, 106.0], max_fills=1)  # only the close fills
    b = IBBroker(ib, _contract("MBTM6"), point_value=0.10)
    b.position_qty = 1
    b.position_avg = 100.0

    with pytest.raises(RollExecutionError):
        b.roll_to(_contract("MBTN6"))


# ---- strategy rebasing ---------------------------------------------------

def test_core_trend_rebase_shifts_all_price_levels():
    s = CoreTrendStrategy(SimBroker())
    s.closes = deque([100.0, 101.0, 102.0], maxlen=240)
    s.ema_fast, s.ema_slow = 101.0, 100.0
    s.entry_price = 101.5
    s.fav_extreme_close = 103.0

    s.rebase_prices(2.0)

    assert list(s.closes) == [102.0, 103.0, 104.0]
    assert (s.ema_fast, s.ema_slow) == (103.0, 102.0)
    assert s.entry_price == 103.5
    assert s.fav_extreme_close == 105.0


def test_rebase_zero_basis_is_noop():
    s = CoreTrendStrategy(SimBroker())
    s.closes = deque([100.0], maxlen=240)
    s.entry_price = 100.0
    s.rebase_prices(0.0)
    assert list(s.closes) == [100.0]
    assert s.entry_price == 100.0


def test_time_return_rebase_shifts_closes_and_entry():
    s = TimeReturnStrategy(SimBroker())
    s.closes = deque([10.0, 11.0], maxlen=255)
    s.entry_price = 10.5
    s.rebase_prices(-1.0)
    assert list(s.closes) == [9.0, 10.0]
    assert s.entry_price == 9.5


def test_counter_trend_rebase_shifts_emas_and_entry():
    s = CounterTrendStrategy(SimBroker())
    s.closes = deque([50.0], maxlen=200)
    s.ema_fast, s.ema_slow = 49.0, 48.0
    s.entry_price = 49.5
    s.rebase_prices(1.5)
    assert list(s.closes) == [51.5]
    assert (s.ema_fast, s.ema_slow) == (50.5, 49.5)
    assert s.entry_price == 51.0


def test_rebase_leaves_diff_based_std_invariant():
    s = CoreTrendStrategy(SimBroker())
    s.closes = deque([100.0, 102.0, 101.0, 104.0], maxlen=240)
    before = s._std_of_changes()
    s.rebase_prices(7.0)
    after = s._std_of_changes()
    assert after == pytest.approx(before)


# ---- Runner roll gating --------------------------------------------------

def _cell(strategy_name, symbol):
    setup = CellSetup(strategy_name=strategy_name, symbol=symbol,
                      data_path="", point_value=5.0)
    broker = SimBroker(point_value=5.0)
    strat = CoreTrendStrategy(broker)
    return Cell(setup=setup, broker=broker, strategy=strat)


def test_cells_for_symbol_groups_by_symbol():
    cells = [_cell("CoreTrend", "MES"), _cell("TimeReturn", "MES"),
             _cell("CoreTrend", "MNQ")]
    r = Runner(cells=cells)
    assert {c.setup.strategy_name for c in r.cells_for_symbol("MES")} == \
        {"CoreTrend", "TimeReturn"}
    assert len(r.cells_for_symbol("MNQ")) == 1


def test_symbol_inflight_true_when_any_cell_mid_order():
    cells = [_cell("CoreTrend", "MES"), _cell("TimeReturn", "MES")]
    r = Runner(cells=cells)
    assert r.symbol_inflight("MES") is False
    cells[1].strategy.state = CoreState.ENTRY_SENT
    assert r.symbol_inflight("MES") is True
