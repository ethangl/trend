"""Tests for cross-restart state persistence. SimBroker only (no IB)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from trend.persistence import (
    SCHEMA_VERSION, StatePersistenceError, load_state, save_state,
)
from trend.runner import CellSetup, Runner
from trend.types import Side


@pytest.fixture
def tiny_csv(tmp_path: Path) -> Path:
    """~300 days: flat, then a steady climb that triggers trades."""
    csv = tmp_path / "tiny.csv"
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    lines = ["timestamp,open,high,low,close,volume"]
    p = 100.0
    for i in range(120):
        ts = (start + timedelta(days=i)).isoformat()
        lines.append(f"{ts},{p},{p+0.5},{p-0.5},{p},1000")
    for i in range(180):
        p += 0.5
        ts = (start + timedelta(days=120 + i)).isoformat()
        lines.append(f"{ts},{p},{p+0.5},{p-0.5},{p},1000")
    csv.write_text("\n".join(lines))
    return csv


def _fake_args():
    return SimpleNamespace(_paused=False, _skip={"MBT", "MET"})


def _setups(csv):
    return [
        CellSetup("CoreTrend",    "MES", str(csv), 5.0),
        CellSetup("CounterTrend", "MES", str(csv), 5.0),
        CellSetup("TimeReturn",   "MNQ", str(csv), 2.0),
    ]


def test_snapshot_apply_round_trip(tiny_csv):
    src = Runner.from_setups(_setups(tiny_csv), excluded=set())
    src.replay_history()
    snap = src.snapshot_cells()

    dst = Runner.from_setups(_setups(tiny_csv), excluded=set())
    dst.replay_history()
    summary = dst.apply_persisted_state(snap)

    assert summary["skipped_missing_from_runner"] == []
    assert summary["new_cells_force_flat"] == []

    for sc, dc in zip(src.cells, dst.cells):
        # Settled cells (not mid-order) must match exactly.
        if f"{dc.setup.strategy_name}×{dc.setup.symbol}" in summary["degraded"]:
            continue
        assert dc.broker.position_qty == sc.broker.position_qty
        assert dc.broker.position_avg == sc.broker.position_avg
        assert dc.broker.total_realized == sc.broker.total_realized
        assert dc.strategy.to_state_dict() == sc.strategy.to_state_dict()


def test_save_load_file_round_trip(tiny_csv, tmp_path):
    src = Runner.from_setups(_setups(tiny_csv), excluded=set())
    src.replay_history()
    args = _fake_args()
    args._paused = True
    path = tmp_path / "state.json"

    save_state(path, src, args)
    loaded = load_state(path)

    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["global"]["paused"] is True
    assert loaded["global"]["skip"] == ["MBT", "MET"]
    assert len(loaded["cells"]) == len(src.cells)


def test_risk_overlay_persists_and_restores(tiny_csv, tmp_path):
    from trend.risk_overlay import RiskOverlayController

    src = Runner.from_setups(_setups(tiny_csv), excluded=set())
    src.replay_history()
    args = _fake_args()
    overlay = RiskOverlayController(0.10, span=20, min_periods=5)
    for r in [0.004, -0.003, 0.005, -0.002, 0.006, -0.004, 0.003]:
        overlay.update(r)
    args._overlay = overlay
    path = tmp_path / "state.json"

    save_state(path, src, args)
    loaded = load_state(path)
    assert "risk_overlay" in loaded["global"]

    restored = RiskOverlayController.from_dict(loaded["global"]["risk_overlay"])
    assert restored.multiplier == overlay.multiplier
    assert restored.trailing_vol == overlay.trailing_vol


def test_load_missing_returns_none(tmp_path):
    assert load_state(tmp_path / "nope.json") is None


def test_load_corrupt_raises(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json")
    with pytest.raises(StatePersistenceError):
        load_state(p)


def test_load_schema_mismatch_raises(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"schema_version": 999, "cells": []}')
    with pytest.raises(StatePersistenceError):
        load_state(p)


def test_apply_skips_cell_missing_from_runner(tiny_csv):
    src = Runner.from_setups(_setups(tiny_csv), excluded=set())
    src.replay_history()
    snap = src.snapshot_cells()

    # Runner with one fewer cell than the saved state.
    dst = Runner.from_setups(_setups(tiny_csv)[:2], excluded=set())
    dst.replay_history()
    summary = dst.apply_persisted_state(snap)

    assert "TimeReturn×MNQ" in summary["skipped_missing_from_runner"]
    assert summary["new_cells_force_flat"] == []


def test_apply_force_flats_new_cell(tiny_csv):
    # A cell present in the runner but absent from the saved state is a newly
    # added market: it can't hold a real IB position yet, so it must be forced
    # flat (not left at the position it built during replay) — the fix for the
    # MET -24 phantom-position HALT.
    src = Runner.from_setups(_setups(tiny_csv)[:2], excluded=set())
    src.replay_history()
    snap = src.snapshot_cells()

    dst = Runner.from_setups(_setups(tiny_csv), excluded=set())
    dst.replay_history()
    new_cell = next(c for c in dst.cells
                    if (c.setup.strategy_name, c.setup.symbol) == ("TimeReturn", "MNQ"))
    summary = dst.apply_persisted_state(snap)

    assert "TimeReturn×MNQ" in summary["new_cells_force_flat"]
    # Forced genuinely flat: broker zeroed AND strategy lifecycle reset.
    assert new_cell.broker.position_qty == 0
    assert new_cell.broker.position_avg == 0.0
    assert new_cell.strategy.state is type(new_cell.strategy.state).FLAT


def test_inflight_cell_is_degraded(tiny_csv):
    src = Runner.from_setups(
        [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)], excluded=set())
    src.replay_history()
    s = src.cells[0].strategy
    State = type(s.state)
    s.state = State.ENTRY_SENT
    s.pending_order_id = 42
    s.pending_entry_side = Side.LONG
    src.cells[0].broker.position_qty = 0  # entry not yet filled

    snap = src.snapshot_cells()
    dst = Runner.from_setups(
        [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)], excluded=set())
    dst.replay_history()
    summary = dst.apply_persisted_state(snap)

    assert summary["degraded"] == ["CoreTrend×MES"]
    ds = dst.cells[0].strategy
    assert ds.state is type(ds.state).FLAT
    assert ds.pending_order_id is None
    assert dst.cells[0].broker.position_qty == 0


def test_next_id_persists(tiny_csv):
    src = Runner.from_setups(
        [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)], excluded=set())
    src.replay_history()
    src.cells[0].broker._next_id = 77

    snap = src.snapshot_cells()
    dst = Runner.from_setups(
        [CellSetup("CoreTrend", "MES", str(tiny_csv), 5.0)], excluded=set())
    dst.replay_history()
    dst.apply_persisted_state(snap)
    assert dst.cells[0].broker._next_id == 77
