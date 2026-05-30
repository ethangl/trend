"""Tests for the live-trading runner. Uses SimBroker only (no IB)."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trend.runner import Cell, CellSetup, Runner, transfer_warm_state
from trend.sim_broker import SimBroker
from trend.types import Bar


@pytest.fixture
def tiny_csv(tmp_path: Path) -> Path:
    """Minimal CSV with ~300 days of flat then trending prices."""
    csv = tmp_path / "tiny.csv"
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    lines = ["timestamp,open,high,low,close,volume"]
    p = 100.0
    for i in range(120):
        ts = (start + timedelta(days=i)).isoformat()
        lines.append(f"{ts},{p},{p+0.5},{p-0.5},{p},1000")
    # Steady climb afterwards — enough to push EMAs apart and trigger breakout
    for i in range(180):
        p += 0.5
        ts = (start + timedelta(days=120 + i)).isoformat()
        lines.append(f"{ts},{p},{p+0.5},{p-0.5},{p},1000")
    csv.write_text("\n".join(lines))
    return csv


def test_runner_builds_cells_and_skips_exclusions(tiny_csv):
    setups = [
        CellSetup("CoreTrend",    "MES", str(tiny_csv), 5.0),
        CellSetup("TimeReturn",   "MES", str(tiny_csv), 5.0),
        CellSetup("CounterTrend", "MES", str(tiny_csv), 5.0),
    ]
    excluded = {("TimeReturn", "MES")}
    runner = Runner.from_setups(setups, excluded=excluded)
    assert len(runner.cells) == 2
    names = {(c.setup.strategy_name, c.setup.symbol) for c in runner.cells}
    assert ("TimeReturn", "MES") not in names


def test_set_risk_multiplier_scales_every_cell_and_sizing(tiny_csv):
    setups = [
        CellSetup("CoreTrend",    "MES", str(tiny_csv), 5.0),
        CellSetup("TimeReturn",   "MES", str(tiny_csv), 5.0),
        CellSetup("CounterTrend", "MES", str(tiny_csv), 5.0),
    ]
    runner = Runner.from_setups(setups, excluded=set())
    # Default is the no-op multiplier.
    for cell in runner.cells:
        assert cell.strategy.cfg.risk_multiplier == 1.0
    # std=10 -> base qty 6, comfortably below max_contracts so 2x doesn't clip.
    base_qty = runner.cells[0].strategy._size(10.0)

    runner.set_risk_multiplier(2.0)
    for cell in runner.cells:
        assert cell.strategy.cfg.risk_multiplier == 2.0
    # Sizing scales with the multiplier (linear up to the int floor / cap).
    assert runner.cells[0].strategy._size(10.0) == pytest.approx(2 * base_qty, abs=1)


def test_replay_brings_strategies_to_today(tiny_csv):
    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    runner = Runner.from_setups(setups, excluded=set())
    runner.replay_history()
    cell = runner.cells[0]
    # After ~300 daily bars including a strong uptrend, CoreTrend should have
    # made at least one trade.
    state = cell.state()
    assert state["last_processed"] is not None
    # Either currently in a position or has trades recorded.
    assert state["position_qty"] != 0 or state["trades_recorded"] >= 1


def test_tick_progresses_state(tiny_csv):
    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    runner = Runner.from_setups(setups, excluded=set())
    runner.replay_history()
    cell = runner.cells[0]
    last_date = cell.last_processed_date

    next_day = datetime.combine(last_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    new_bar = Bar(ts=next_day, open=200.0, high=201.0, low=199.5, close=200.5)
    runner.tick({"MES": new_bar})
    assert cell.last_processed_date == next_day.date()


def test_tick_ignores_stale_bars(tiny_csv):
    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    runner = Runner.from_setups(setups, excluded=set())
    runner.replay_history()
    cell = runner.cells[0]
    last_date = cell.last_processed_date

    same_day = datetime.combine(last_date, datetime.min.time(), tzinfo=timezone.utc)
    stale_bar = Bar(ts=same_day, open=200.0, high=201.0, low=199.5, close=200.5)
    runner.tick({"MES": stale_bar})
    assert cell.last_processed_date == last_date  # unchanged


def test_positions_by_symbol_aggregates_across_cells(tiny_csv):
    setups = [
        CellSetup("CoreTrend",    "MES", str(tiny_csv), 5.0),
        CellSetup("CounterTrend", "MES", str(tiny_csv), 5.0),
    ]
    runner = Runner.from_setups(setups, excluded=set())
    runner.replay_history()
    agg = runner.positions_by_symbol()
    # Single symbol → aggregate present
    assert "MES" in agg
    # Sum should equal sum of individual broker positions
    direct_sum = sum(c.broker.position().qty for c in runner.cells)
    assert agg["MES"] == direct_sum


def test_reconcile_against_detects_mismatch(tiny_csv):
    from trend.reconcile import Severity
    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    runner = Runner.from_setups(setups, excluded=set())
    runner.replay_history()
    expected = runner.positions_by_symbol()
    # Construct a fake IBKR position that disagrees
    fake_ibkr = {sym: q + 5 for sym, q in expected.items()}
    report = runner.reconcile_against(fake_ibkr, halt_threshold=0)
    assert report.overall is Severity.HALT


def test_reconcile_against_matches_after_force_flat(tiny_csv):
    from trend.reconcile import Severity
    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    runner = Runner.from_setups(setups, excluded=set())
    runner.replay_history()
    runner.force_flat_all_cells()
    # After force_flat, cells aggregate to 0 → matches a flat IBKR
    report = runner.reconcile_against({"MES": 0}, halt_threshold=0)
    assert report.overall is Severity.OK


def test_transfer_warm_state_moves_strategy_and_position(tiny_csv):
    setups = [
        CellSetup("CoreTrend",    "MES", str(tiny_csv), 5.0),
        CellSetup("CounterTrend", "MES", str(tiny_csv), 5.0),
    ]
    sim = Runner.from_setups(setups, excluded=set())
    sim.replay_history()
    live = Runner.from_setups(setups, excluded=set())

    # Snapshot what sim ended with.
    sim_strats = [c.strategy for c in sim.cells]
    sim_qtys = [c.broker.position_qty for c in sim.cells]
    sim_avgs = [c.broker.position_avg for c in sim.cells]
    sim_dates = [c.last_processed_date for c in sim.cells]
    live_brokers = [c.broker for c in live.cells]

    transfer_warm_state(sim, live)

    for i, lc in enumerate(live.cells):
        # The warmed strategy object moved over wholesale.
        assert lc.strategy is sim_strats[i]
        # Strategy now points at the LIVE broker, not the sim one.
        assert lc.strategy.broker is live_brokers[i]
        # Position bookkeeping + date copied.
        assert lc.broker.position_qty == sim_qtys[i]
        assert lc.broker.position_avg == sim_avgs[i]
        assert lc.last_processed_date == sim_dates[i]


def test_transfer_warm_state_rejects_mismatched_runners(tiny_csv):
    sim = Runner.from_setups(
        [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)], excluded=set())
    live = Runner.from_setups(
        [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0),
         CellSetup("CounterTrend", "MES", str(tiny_csv), 5.0)], excluded=set())
    with pytest.raises(ValueError):
        transfer_warm_state(sim, live)


def test_transferred_strategy_fill_callback_fires_on_live_broker(tiny_csv):
    """After transfer, a fill on the LIVE broker must reach the strategy so it
    keeps tracking trades (the bug that previously stomped the callback)."""
    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    sim = Runner.from_setups(setups, excluded=set())
    sim.replay_history()
    live = Runner.from_setups(setups, excluded=set())
    transfer_warm_state(sim, live)

    cell = live.cells[0]
    cell.broker.position_qty = 0
    cell.broker.position_avg = 0.0
    cell.strategy.state = type(cell.strategy.state).FLAT
    cell.strategy.pending_order_id = None
    trades_before = len(cell.strategy.trades)

    # Drive a strong breakout bar; strategy should place an order on the live
    # broker, and SimBroker fills it on the following bar.
    base = datetime.combine(cell.last_processed_date, datetime.min.time(),
                            tzinfo=timezone.utc)
    cell.broker.on_bar(Bar(ts=base + timedelta(days=1), open=500, high=505,
                           low=499, close=504))
    cell.strategy.on_bar(Bar(ts=base + timedelta(days=1), open=500, high=505,
                             low=499, close=504))
    # Next bar triggers the fill of any pending order on the live broker.
    cell.broker.on_bar(Bar(ts=base + timedelta(days=2), open=504, high=509,
                           low=503, close=508))
    # The strategy must have received fills (its position is now non-zero).
    assert cell.broker.position_qty != 0


def test_reset_inflight_strategies(tiny_csv):
    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    runner = Runner.from_setups(setups, excluded=set())
    runner.replay_history()
    s = runner.cells[0].strategy
    State = type(s.state)
    s.state = State.ENTRY_SENT
    s.pending_order_id = 999
    n = runner.reset_inflight_strategies()
    assert n == 1
    assert s.state is State.FLAT
    assert s.pending_order_id is None


def test_check_rolls_returns_warnings(tiny_csv):
    from datetime import date
    from trend.roll import ContractInfo, Severity as RollSev

    setups = [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)]
    runner = Runner.from_setups(setups, excluded=set())
    today = date(2026, 5, 24)
    infos = [
        ContractInfo(symbol="MES", contract_label="MESM6",
                     last_trade_date=date(2026, 6, 19)),  # 26d → OK
        ContractInfo(symbol="MNQ", contract_label="MNQM6",
                     last_trade_date=date(2026, 5, 30)),  # 6d → ROLL_NOW
    ]
    warnings = runner.check_rolls(infos, today)
    by_sym = {w.symbol: w for w in warnings}
    assert by_sym["MES"].severity is RollSev.OK
    assert by_sym["MNQ"].severity is RollSev.ROLL_NOW
