"""Live-trading orchestrator (paper or production).

The Runner owns one Cell per (strategy × market) combination. Each Cell holds
its own broker + strategy. On startup, Cells replay all historical bars from
CSV to bring their internal state (EMAs, std, position, etc.) to "today."
After that, the Runner enters a daily tick loop: fetch the latest bar for
each market, feed it through the strategy, log any resulting orders.

For v1 the brokers are SimBrokers — historical replay is fully simulated.
When the IB Gateway is available, swap each cell's broker to IBBroker and
route orders for real. The Cell/Runner API is broker-agnostic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .broker import Broker
from .core_trend import CoreTrendConfig, CoreTrendStrategy
from .counter_trend import CounterTrendConfig, CounterTrendStrategy
from .data import load_bars_csv
from .sim_broker import SimBroker
from .time_return import TimeReturnConfig, TimeReturnStrategy
from .types import Bar


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CellSetup:
    strategy_name: str  # "CoreTrend" | "TimeReturn" | "CounterTrend"
    symbol: str         # e.g. "MES"
    data_path: str      # CSV with daily bars
    point_value: float
    commission: float = 0.62


def make_strategy(
    name: str,
    broker: Broker,
    point_value: float,
    risk_factor: float,
    portfolio_value: float,
):
    if name == "CoreTrend":
        return CoreTrendStrategy(broker, CoreTrendConfig(
            risk_factor=risk_factor,
            portfolio_value_usd=portfolio_value,
            point_value=point_value,
        ))
    if name == "TimeReturn":
        return TimeReturnStrategy(broker, TimeReturnConfig(
            risk_factor=risk_factor,
            portfolio_value_usd=portfolio_value,
            point_value=point_value,
        ))
    if name == "CounterTrend":
        return CounterTrendStrategy(broker, CounterTrendConfig(
            risk_factor=risk_factor,
            portfolio_value_usd=portfolio_value,
            point_value=point_value,
        ))
    raise ValueError(f"unknown strategy: {name}")


@dataclass
class Cell:
    """One (strategy × market) cell. Owns its broker and strategy."""
    setup: CellSetup
    broker: Broker
    strategy: Any  # CoreTrendStrategy | TimeReturnStrategy | CounterTrendStrategy
    last_processed_date: date | None = None

    def replay_bars(self, bars: list[Bar]) -> None:
        """Feed bars sequentially through broker + strategy."""
        for b in bars:
            # Both SimBroker and IBBroker expose on_bar (SimBroker uses it to
            # process fills; IBBroker treats it as a no-op since fills come
            # from IB events). Be permissive.
            on_bar = getattr(self.broker, "on_bar", None)
            if on_bar is not None:
                on_bar(b)
            self.strategy.on_bar(b)
        if bars:
            self.last_processed_date = bars[-1].ts.date()

    def state(self) -> dict:
        pos = self.broker.position()
        trades = getattr(self.strategy, "trades", [])
        return {
            "strategy":         self.setup.strategy_name,
            "symbol":           self.setup.symbol,
            "position_qty":     pos.qty,
            "position_avg":     pos.avg_price,
            "last_processed":   self.last_processed_date,
            "total_realized":   getattr(self.broker, "total_realized", 0.0),
            "trades_recorded":  len(trades),
            "strategy_state":   getattr(self.strategy.state, "value", str(self.strategy.state))
                                if hasattr(self.strategy, "state") else None,
        }


@dataclass
class Runner:
    """Owns a list of Cells. Drives replay + ticks across all of them."""
    cells: list[Cell] = field(default_factory=list)
    portfolio_value_usd: float = 300_000.0
    risk_factor: float = 0.001

    @classmethod
    def from_setups(
        cls,
        setups: list[CellSetup],
        excluded: set[tuple[str, str]],
        portfolio_value_usd: float = 300_000.0,
        risk_factor: float = 0.001,
        broker_factory=None,
    ) -> "Runner":
        """Build a Runner from setups, skipping excluded (strategy, symbol) cells.

        broker_factory(setup) -> Broker. Defaults to SimBroker (suitable for
        dry-runs and replay). Pass a factory that returns IBBroker for live.
        """
        if broker_factory is None:
            def broker_factory(s: CellSetup) -> Broker:
                return SimBroker(
                    point_value=s.point_value,
                    commission_per_contract=s.commission,
                )

        cells: list[Cell] = []
        for setup in setups:
            if (setup.strategy_name, setup.symbol) in excluded:
                continue
            broker = broker_factory(setup)
            strategy = make_strategy(
                setup.strategy_name, broker, setup.point_value,
                risk_factor, portfolio_value_usd,
            )
            cells.append(Cell(setup=setup, broker=broker, strategy=strategy))
        return cls(
            cells=cells,
            portfolio_value_usd=portfolio_value_usd,
            risk_factor=risk_factor,
        )

    def replay_history(self) -> None:
        """For each cell, load its CSV and replay every bar through the strategy.
        Brings EMAs, std, position state, etc. to the latest historical bar.
        """
        for cell in self.cells:
            path = Path(cell.setup.data_path)
            if not path.exists():
                log.warning("missing data for %s: %s", cell.setup.symbol, path)
                continue
            bars = load_bars_csv(str(path))
            cell.replay_bars(bars)
            log.info(
                "replayed %d bars for %s × %s through %s",
                len(bars), cell.setup.strategy_name, cell.setup.symbol,
                cell.last_processed_date,
            )

    def tick(self, new_bars_by_symbol: dict[str, Bar]) -> None:
        """One scheduled tick: feed each cell's latest bar through its strategy."""
        for cell in self.cells:
            bar = new_bars_by_symbol.get(cell.setup.symbol)
            if bar is None:
                continue
            if (cell.last_processed_date is not None
                    and bar.ts.date() <= cell.last_processed_date):
                log.debug("skip stale bar for %s @ %s", cell.setup.symbol, bar.ts)
                continue
            on_bar = getattr(cell.broker, "on_bar", None)
            if on_bar is not None:
                on_bar(bar)
            cell.strategy.on_bar(bar)
            cell.last_processed_date = bar.ts.date()

    def report(self) -> list[dict]:
        return [cell.state() for cell in self.cells]

    def positions_by_symbol(self) -> dict[str, int]:
        """Aggregate signed positions across cells for the same symbol.
        Useful when reconciling live IBKR positions against what the cells
        collectively want to hold."""
        agg: dict[str, int] = {}
        for cell in self.cells:
            sym = cell.setup.symbol
            agg[sym] = agg.get(sym, 0) + cell.broker.position().qty
        return agg

    def reconcile_against(
        self,
        ibkr_positions: dict[str, int],
        halt_threshold: int = 0,
    ):
        """Compare cells' aggregate positions against IBKR's actual.

        Returns a `reconcile.Report` describing any mismatches and their
        severity. The caller decides what to do (halt, prompt, force-flat
        cells, etc.) — this method is purely diagnostic.
        """
        from . import reconcile

        return reconcile.compute(
            expected=self.positions_by_symbol(),
            actual=ibkr_positions,
            halt_threshold=halt_threshold,
        )

    def check_rolls(
        self,
        contract_infos,  # list[roll.ContractInfo]
        today,           # date
        commodity_symbols: set[str] | None = None,
    ):
        """Return roll warnings for the given contracts. Pure delegation to
        the `roll` module — kept on the Runner so callers have one place to go.
        """
        from . import roll

        return roll.evaluate_all(
            contract_infos, today,
            commodity_symbols=commodity_symbols,
        )

    def reset_inflight_strategies(self) -> int:
        """Reset any strategy stuck mid-order (state ending in _SENT) to FLAT.

        After a force-flat, a strategy that fired an order on its final replay
        bar is still in ENTRY_SENT/EXIT_SENT waiting for a SimBroker fill that
        no longer exists. Clear those so they can act on the next signal.
        Returns the number of strategies reset.
        """
        reset = 0
        for cell in self.cells:
            s = cell.strategy
            state_name = getattr(getattr(s, "state", None), "name", "")
            if state_name in {"ENTRY_SENT", "EXIT_SENT"}:
                try:
                    s.state = type(s.state).FLAT
                    if hasattr(s, "pending_order_id"):
                        s.pending_order_id = None
                    reset += 1
                except AttributeError:
                    pass
        return reset

    # ---- state persistence ----

    _INFLIGHT_STATES = {"ENTRY_SENT", "EXIT_SENT", "PENDING"}

    def snapshot_cells(self) -> list[dict]:
        """Per-cell path-dependent state for persistence. Pairs with
        apply_persisted_state. Broker bookkeeping + strategy state; derivable
        fields (EMAs, deques, std) are omitted — they're rebuilt by replay."""
        from datetime import date as _date

        out: list[dict] = []
        for cell in self.cells:
            b = cell.broker
            daily = getattr(b, "daily_realized", {}) or {}
            out.append({
                "strategy": cell.setup.strategy_name,
                "symbol":   cell.setup.symbol,
                "broker": {
                    "position_qty":   b.position_qty,
                    "position_avg":   b.position_avg,
                    "total_realized": getattr(b, "total_realized", 0.0),
                    "daily_realized": {
                        (k.isoformat() if isinstance(k, _date) else str(k)): v
                        for k, v in daily.items()
                    },
                    "fills":   [f.to_dict() for f in getattr(b, "fills", [])[-100:]],
                    "next_id": getattr(b, "_next_id", 1),
                },
                "strategy_state": cell.strategy.to_state_dict(),
            })
        return out

    def apply_persisted_state(self, cells_state: list[dict]) -> dict:
        """Distribute persisted per-cell state onto brokers + strategies.

        Returns a summary dict: applied cells, cells in state but missing from
        this runner (skipped), cells in this runner missing from state (left at
        default), and cells degraded because they were mid-order at save time.

        Mid-order ('*_SENT'/'PENDING') cells can't be resumed faithfully — the
        live broker's pending IB Trade (and its fill callback) is gone after a
        restart. We degrade those to the cold-start behavior (force-flat broker
        + reset strategy to FLAT) and rely on the post-load reconcile against
        IB to HALT loudly if the order actually filled and left a position.
        """
        from datetime import date as _date

        from .types import Fill

        by_key = {(c["strategy"], c["symbol"]): c for c in cells_state}
        runner_keys = {(c.setup.strategy_name, c.setup.symbol) for c in self.cells}

        applied: list[str] = []
        degraded: list[str] = []
        for cell in self.cells:
            key = (cell.setup.strategy_name, cell.setup.symbol)
            cstate = by_key.get(key)
            if cstate is None:
                continue  # new cell — leave at replay default
            b = cell.broker
            bs = cstate["broker"]
            b.position_qty = bs["position_qty"]
            b.position_avg = bs["position_avg"]
            if hasattr(b, "total_realized"):
                b.total_realized = bs["total_realized"]
            if hasattr(b, "daily_realized"):
                b.daily_realized = {
                    _date.fromisoformat(k): v
                    for k, v in bs["daily_realized"].items()
                }
            if hasattr(b, "fills"):
                b.fills = [Fill.from_dict(f) for f in bs["fills"]]
            b._next_id = bs["next_id"]

            cell.strategy.apply_state_dict(cstate["strategy_state"])

            state_name = getattr(getattr(cell.strategy, "state", None), "name", "")
            if state_name in self._INFLIGHT_STATES:
                b.position_qty = 0
                b.position_avg = 0.0
                cell.strategy.state = type(cell.strategy.state).FLAT
                for attr in ("pending_order_id", "pending_close_id",
                             "pending_open_id"):
                    if hasattr(cell.strategy, attr):
                        setattr(cell.strategy, attr, None)
                degraded.append(f"{key[0]}×{key[1]}")
            else:
                applied.append(f"{key[0]}×{key[1]}")

        missing = sorted(f"{s}×{sym}" for (s, sym) in by_key
                         if (s, sym) not in runner_keys)
        new_cells = sorted(f"{s}×{sym}" for (s, sym) in runner_keys
                           if (s, sym) not in by_key)
        return {
            "applied": applied,
            "degraded": degraded,
            "skipped_missing_from_runner": missing,
            "new_cells_left_default": new_cells,
        }

    def force_flat_all_cells(self) -> None:
        """Reset every cell's broker position to flat, without placing orders.

        Useful on first paper/live startup after replay: cells are warmed up
        internally (EMAs, std, deques) but should start with no actual
        position. Strategies will re-enter when their next signal fires.

        NOTE: this only zeroes the broker's local position tracking. It does
        NOT reset the strategy's own state (e.g., CoreTrend.state may still
        be LONG with a known entry_price). For a fully clean start, the
        strategy state should also be reset — but that requires per-strategy
        knowledge and we leave it to the operator.
        """
        for cell in self.cells:
            broker = cell.broker
            broker.position_qty = 0
            broker.position_avg = 0.0


def transfer_warm_state(sim_runner: "Runner", live_runner: "Runner") -> None:
    """Move warmed strategy state from a SimBroker-replayed runner onto a
    parallel live (IBBroker) runner.

    We never replay through IBBroker — strategies call broker.place_order on
    every signal during replay, which against a live broker would submit
    thousands of real orders. Instead we replay through SimBroker, then move
    each warmed strategy onto the live runner here.

    For each cell, in lockstep: the strategy object (with its EMAs, std
    windows, trailing extremes, state, and trade ledger) is moved over, its
    broker reference is repointed at the live broker, its fill callback is
    re-registered on the live broker, and the live broker's position
    bookkeeping + last_processed_date are copied from the sim cell.

    Both runners must be built from the same setups in the same order.
    """
    if len(sim_runner.cells) != len(live_runner.cells):
        raise ValueError(
            f"cell count mismatch: sim has {len(sim_runner.cells)}, "
            f"live has {len(live_runner.cells)}"
        )
    for sim_c, live_c in zip(sim_runner.cells, live_runner.cells):
        if (sim_c.setup.strategy_name, sim_c.setup.symbol) != \
           (live_c.setup.strategy_name, live_c.setup.symbol):
            raise ValueError(
                f"cell order mismatch: sim {sim_c.setup.strategy_name}×"
                f"{sim_c.setup.symbol} vs live {live_c.setup.strategy_name}×"
                f"{live_c.setup.symbol}"
            )
        live_c.strategy = sim_c.strategy
        sim_c.strategy.broker = live_c.broker
        set_on_fill = getattr(live_c.broker, "set_on_fill", None)
        on_fill = getattr(sim_c.strategy, "_on_fill", None)
        if set_on_fill is not None and on_fill is not None:
            set_on_fill(on_fill)
        live_c.broker.position_qty = sim_c.broker.position_qty
        live_c.broker.position_avg = sim_c.broker.position_avg
        live_c.last_processed_date = sim_c.last_processed_date
