#!/usr/bin/env python3
"""Long-running live (paper) trader.

Start ONCE and leave running. On startup it:
  1. connects to IB Gateway (paper) and qualifies front-month contracts,
  2. optionally flattens the account for a clean slate (--flatten-account),
  3. replays CSV history through SimBroker,
  4. catches up any completed daily bars since the CSV end (still on Sim, so
     no historical orders reach IB),
  5. swaps the warmed strategies onto IBBroker,
  6. force-flats on first start (--first-run, the default for a fresh account),
  7. then enters a daily tick loop: each weekday shortly after CME close it
     fetches the day's completed bar per market and feeds every cell.

State lives in memory for the life of the process, so positions stay
reconciled with IBKR without re-replaying. Run under a supervisor
(launchd/systemd) for crash recovery — a fresh start replays + force-flats
again, so flatten the account before relaunching (or use --flatten-account).

Usage:
    .venv/bin/python scripts/run_live_loop.py --flatten-account
    .venv/bin/python scripts/run_live_loop.py --once   # single tick now, then exit
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.all_strategies_backtest import EXCLUDED_CELLS  # noqa: E402
from scripts.run_daily_live import (  # noqa: E402
    EXCHANGES, CellFill, CellOrder, RunLog, _write_log, build_setups,
    compute_roll_basis, fetch_all_new_bars, ibkr_positions_by_symbol,
    qualify_contracts, resolve_next_contract,
)
from trend import roll  # noqa: E402
from trend.commands import default_command_path, read_and_delete_command  # noqa: E402
from trend.ib_broker import IBBroker  # noqa: E402
from trend.ib_data import fetch_daily_bars  # noqa: E402
from trend.persistence import (  # noqa: E402
    StatePersistenceError, default_state_path, load_state, save_state,
)
from trend.reconcile import format_report  # noqa: E402
from trend.runner import CellSetup, Runner, transfer_warm_state  # noqa: E402
from trend.sim_broker import SimBroker  # noqa: E402
from trend.status import SCHEMA_VERSION, default_status_path, write_status  # noqa: E402

ET = ZoneInfo("America/New_York")
log = logging.getLogger("run_live_loop")

# A session dated D is treated as complete once the clock passes this ET time
# (CME equity-index close is 17:00 ET; pad for IB's daily-bar publish lag).
SESSION_COMPLETE_AFTER = dtime(17, 5)

# Physically-deliverable contracts whose First Notice Day precedes expiry — they
# get the longer roll thresholds so IBKR doesn't liquidate us at FND. (Equity
# indexes, rates, and currencies are cash-settled / financial → default window.)
COMMODITY_SYMBOLS = {"MCL", "MGC", "ZC", "ZS"}


def connect_ib(host: str, port: int, client_id: int):
    from ib_async import IB
    ib = IB()
    log.info("connecting to %s:%s clientId=%s", host, port, client_id)
    ib.connect(host, port, clientId=client_id, readonly=False)
    accounts = ib.managedAccounts()
    if not any(a.startswith(("D", "DU", "DF")) for a in accounts):
        ib.disconnect()
        raise SystemExit(f"REFUSING TO RUN on non-paper account: {accounts}")
    log.info("connected to paper account: %s", accounts[0] if accounts else "?")
    return ib


def connect_with_retry(args, *, base_delay: float = 2.0, max_delay: float = 60.0):
    """Connect to IB, retrying with capped backoff while the gateway is
    unreachable. IB Gateway logs out / restarts daily (and the API port goes
    away during that window); without this the loop would crash on the first
    ConnectionRefused and burn the supervisor's restart budget. Instead we wait
    it out and reconnect ourselves once the gateway returns.

    The non-paper-account guard inside connect_ib raises SystemExit, which we
    deliberately do NOT retry — that's a refuse-to-run, not a transient fault.
    """
    delay = base_delay
    attempt = 0
    while True:
        attempt += 1
        try:
            return connect_ib(args.host, args.port, args.client_id)
        except SystemExit:
            raise
        except Exception as e:
            # Don't outlive a dead parent while spinning on reconnect.
            if args.exit_on_orphan and os.getppid() != args._original_ppid:
                log.warning("parent died during reconnect; exiting")
                sys.exit(0)
            log.warning("IB connect attempt %d failed (%s); retrying in %.0fs",
                        attempt, e, delay)
            time.sleep(delay)
            delay = min(delay * 1.5, max_delay)


def process_command(cmd: dict, *, runner, ib, args) -> None:
    """Apply one command from the menubar app. Logs everything; no return value.

    Result of each command is visible to the menubar via the next status.json
    heartbeat — e.g. `paused: true`, an empty positions list after `flatten`, etc.
    """
    name = (cmd.get("command") or "").lower()
    cmd_args = cmd.get("args") or {}
    log.warning("processing command: %s args=%s", name, cmd_args)
    try:
        if name == "pause":
            args._paused = True
            log.warning("loop paused")
        elif name == "resume":
            args._paused = False
            log.warning("loop resumed")
        elif name == "flatten":
            closed = flatten_account(ib)
            runner.force_flat_all_cells()
            unstuck = runner.reset_inflight_strategies()
            # Also reset LONG/SHORT — user explicitly asked for a clean slate.
            for cell in runner.cells:
                state_name = getattr(getattr(cell.strategy, "state", None), "name", "")
                if state_name in {"LONG", "SHORT"}:
                    try:
                        cell.strategy.state = type(cell.strategy.state).FLAT
                        if hasattr(cell.strategy, "pending_order_id"):
                            cell.strategy.pending_order_id = None
                    except AttributeError:
                        pass
            log.warning("flattened %d positions, reset %d inflight strategies",
                        len(closed), unstuck)
            _persist_state(runner, args)
        elif name == "restart":
            log.warning("restart requested — saving state and exiting; "
                        "supervisor will relaunch")
            _persist_state(runner, args)
            ib.disconnect()
            sys.exit(0)
        else:
            log.warning("unknown command: %s", name)
    except SystemExit:
        raise
    except Exception as e:
        log.exception("command %s failed: %s", name, e)


def _persist_state(runner, args) -> None:
    """Best-effort atomic save of path-dependent state. Never raises — a failed
    save must not crash a live tick or abort a shutdown."""
    try:
        save_state(args.state_path, runner, args)
        log.info("saved state to %s", args.state_path)
    except Exception as e:
        log.warning("save_state failed: %s", e)


def flatten_account(ib) -> list[tuple[str, int]]:
    """Close every open IB position with a MARKET order. Returns what it closed."""
    from ib_async import MarketOrder
    open_positions = [p for p in ib.positions() if int(p.position) != 0]
    if not open_positions:
        return []
    # Contracts from ib.positions() carry a conId but no exchange, and IB
    # rejects an order on an exchange-less contract ("Missing order exchange").
    # Qualifying by conId fills in the listing exchange in place.
    ib.qualifyContracts(*[p.contract for p in open_positions])
    closed: list[tuple[str, int]] = []
    for pos in open_positions:
        qty = int(pos.position)
        action = "SELL" if qty > 0 else "BUY"
        order = MarketOrder(action, abs(qty))
        order.tif = "DAY"
        ib.placeOrder(pos.contract, order)
        closed.append((pos.contract.localSymbol, -qty))
    log.warning("flattening account: %s", closed)
    ib.sleep(3.0)
    return closed


def session_complete_cutoff(now_et: datetime) -> date:
    """Latest session date considered complete as of now."""
    if now_et.time() >= SESSION_COMPLETE_AFTER:
        return now_et.date()
    return now_et.date() - timedelta(days=1)


def catch_up(runner: Runner, ib, contracts: dict, cutoff: date,
             n_bars: int) -> int:
    """Tick every completed daily bar after each cell's last_processed_date up
    through `cutoff`, in chronological order. Runs on the SimBroker runner so
    no historical orders reach IB. Returns the number of session dates ticked.
    """
    bars_by_symbol: dict[str, list] = {}
    for sym, contract in contracts.items():
        try:
            bars_by_symbol[sym] = fetch_daily_bars(ib, contract, n_bars=n_bars)
        except Exception as e:
            log.error("catch-up fetch failed for %s: %s", sym, e)
            bars_by_symbol[sym] = []

    dates = sorted({b.ts.date() for bars in bars_by_symbol.values()
                    for b in bars if b.ts.date() <= cutoff})
    ticked = 0
    for d in dates:
        day_bars = {}
        for sym, bars in bars_by_symbol.items():
            match = [b for b in bars if b.ts.date() == d]
            if match:
                day_bars[sym] = match[0]
        if day_bars:
            runner.tick(day_bars)  # runner.tick skips per-cell stale bars
            ticked += 1
            log.info("catch-up ticked session %s (%d symbols)", d, len(day_bars))
    return ticked


def next_run_dt(now_et: datetime, run_hour: int, run_min: int) -> datetime:
    """Next weekday at run_hour:run_min ET strictly after now."""
    target = now_et.replace(hour=run_hour, minute=run_min, second=0, microsecond=0)
    if target <= now_et:
        target += timedelta(days=1)
    while target.weekday() >= 5:  # Sat=5, Sun=6
        target += timedelta(days=1)
    return target


def recent_fills_payload(runner: Runner, n: int = 10) -> list[dict]:
    """Last `n` fills across all cells, oldest → newest."""
    out = []
    for cell in runner.cells:
        for f in cell.broker.fills:
            out.append({
                "ts": f.ts.isoformat(),
                "symbol": cell.setup.symbol,
                "strategy": cell.setup.strategy_name,
                "side": f.side.name,
                "qty": f.qty,
                "price": f.price,
                "order_id": f.order_id,
            })
    out.sort(key=lambda x: x["ts"])
    return out[-n:]


def build_status(*, runner: Runner | None, ib, args, status: str,
                 next_tick_at: datetime | None,
                 last_tick: dict | None,
                 recent_errors: list[dict] | None = None) -> dict:
    """Snapshot of loop state for the menubar app to read."""
    ib_connected = bool(ib) and ib.isConnected()
    expected: dict[str, int] = {}
    if runner is not None:
        expected = {k: v for k, v in runner.positions_by_symbol().items() if v != 0}
    actual: dict[str, int] = {}
    if ib_connected:
        try:
            actual = ibkr_positions_by_symbol(ib)
        except Exception:
            actual = {}
    account = ""
    if ib_connected:
        accs = ib.managedAccounts() or []
        if accs:
            account = accs[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(ET).isoformat(),
        "status": status,
        "account": account,
        "ib_connected": ib_connected,
        "next_tick_at": next_tick_at.isoformat() if next_tick_at else None,
        "last_tick": last_tick,
        "expected_positions": expected,
        "ibkr_positions": dict(actual),
        "skipped_symbols": sorted(getattr(args, "_skip", set())),
        "cells_active": len(runner.cells) if runner else 0,
        "recent_fills": recent_fills_payload(runner) if runner else [],
        "recent_errors": recent_errors or [],
        "paused": getattr(args, "_paused", False),
    }


def sleep_until(ib, target_et: datetime, reconnect,
                heartbeat=None, chunk_secs: float = 2.0) -> None:
    """Pump the IB event loop until target_et, reconnecting if the link drops.

    Calls `heartbeat()` after every `chunk_secs` of sleep. The caller's
    heartbeat decides what to do at that cadence (poll commands, refresh
    status.json, etc.) — see the `make_heartbeat` factory in main().
    """
    while True:
        now = datetime.now(ET)
        remaining = (target_et - now).total_seconds()
        if remaining <= 0:
            return
        chunk = min(chunk_secs, remaining)
        try:
            ib.sleep(chunk)
        except Exception as e:
            # A socket disconnect surfaces as an exception out of ib.sleep
            # (e.g. gateway's daily restart). Don't die — reconnect and keep
            # counting down. reconnect() blocks via connect_with_retry until
            # the gateway is back, so this naturally rides out the outage.
            log.warning("IB error during sleep (%s); reconnecting", e)
            ib = reconnect()
            continue
        if not ib.isConnected():
            log.warning("IB connection dropped during sleep; reconnecting")
            ib = reconnect()
        if heartbeat is not None:
            try:
                heartbeat()
            except SystemExit:
                raise
            except Exception as e:
                log.warning("heartbeat failed: %s", e)


def execute_rolls(runner: Runner, ib, contracts: dict, today: date) -> list[str]:
    """Detect contracts near expiry and roll held positions onto the successor.

    Runs at the top of every tick. For each qualified contract we evaluate roll
    urgency (commodities get the longer FND-aware window); WARN/ROLL_NOW are
    logged. On ROLL_NOW we resolve the next listed expiration and, for every
    cell trading that symbol, close+re-open the position on the new contract
    (position-neutral) and rebase the strategy's price state by the contract
    basis so trailing stops / breakouts stay continuous. `contracts` is mutated
    in place to point at the new front month.

    A roll is deferred (left for the next tick) while any cell for the symbol is
    mid-order. A half-completed roll raises RollExecutionError out of this
    function so the tick HALTs rather than carrying a corrupt book.

    Returns the list of symbols rolled this tick.
    """
    infos = []
    for sym, contract in contracts.items():
        raw = getattr(contract, "lastTradeDateOrContractMonth", "") or ""
        try:
            ltd = roll.parse_ib_expiry(raw)
        except ValueError:
            log.warning("can't parse expiry %r for %s; skipping roll check", raw, sym)
            continue
        infos.append(roll.ContractInfo(
            symbol=sym,
            contract_label=getattr(contract, "localSymbol", sym),
            last_trade_date=ltd,
        ))

    warnings = runner.check_rolls(infos, today, commodity_symbols=COMMODITY_SYMBOLS)
    actionable = roll.needs_action(warnings)
    if actionable:
        log.warning("roll status:\n%s", roll.format_warnings(actionable))

    rolled: list[str] = []
    for w in warnings:
        if w.severity is not roll.Severity.ROLL_NOW:
            continue
        sym = w.symbol
        if runner.symbol_inflight(sym):
            log.warning("ROLL_NOW for %s but a cell is mid-order; deferring roll", sym)
            continue
        old_contract = contracts[sym]
        exchange = EXCHANGES.get(sym)
        if exchange is None:
            log.error("no exchange mapping for %s; cannot roll", sym)
            continue
        new_contract = resolve_next_contract(
            ib, sym, exchange, old_contract.lastTradeDateOrContractMonth
        )
        if new_contract is None:
            log.error("ROLL_NOW for %s but no successor contract resolved", sym)
            continue
        if getattr(new_contract, "conId", None) == getattr(old_contract, "conId", None):
            log.warning("resolved successor for %s equals current contract; skipping", sym)
            continue

        basis = compute_roll_basis(ib, old_contract, new_contract)
        log.warning("rolling %s %s → %s (basis=%.4f)",
                    sym, old_contract.localSymbol, new_contract.localSymbol, basis)
        for cell in runner.cells_for_symbol(sym):
            roll_to = getattr(cell.broker, "roll_to", None)
            if roll_to is None:
                # SimBroker fallback cell (no live contract) — nothing to roll.
                continue
            res = roll_to(new_contract)
            cell.strategy.rebase_prices(basis)
            if res.qty != 0:
                log.warning("  %s×%s rolled %+d @ %.4f→%.4f",
                            cell.setup.strategy_name, sym, res.qty,
                            res.close_price, res.open_price)
        contracts[sym] = new_contract
        rolled.append(sym)
    return rolled


def do_tick(runner: Runner, ib, contracts: dict, args, first_run: bool,
            fills_cursor: dict) -> RunLog:
    """One daily tick: fetch today's completed bars, feed cells, log.

    `fills_cursor` is a dict[id(cell) -> int] mapping each cell to the
    `broker.fills` length we last logged. Each tick captures fills since the
    last logged index — so a fill that arrives 30 minutes after the tick was
    placed (e.g. a MARKET-DAY order filling at the next session open) still
    lands in the *next* tick's record instead of vanishing. Cursor is mutated
    in place after logging.
    """
    run = RunLog(
        started_at=datetime.now(ET).isoformat(),
        host=args.host, port=args.port,
        first_run=first_run,
        portfolio_value_usd=args.portfolio, risk_factor=args.risk_factor,
        cells_active=len(runner.cells), cells_excluded=len(EXCLUDED_CELLS),
    )
    today_et = datetime.now(ET).date()
    run.today_et = today_et.isoformat()

    actual = ibkr_positions_by_symbol(ib)
    run.ibkr_positions = dict(actual)
    expected = {k: v for k, v in runner.positions_by_symbol().items() if v != 0}
    run.expected_positions = expected
    report = runner.reconcile_against(actual, halt_threshold=args.halt_threshold)
    run.reconcile_severity = report.overall.value
    log.info("reconcile severity=%s expected=%s actual=%s",
             report.overall.value, expected, dict(actual))

    # Roll any expiring contracts before fetching/feeding bars, so the day's
    # bar is pulled from (and any orders route to) the new front month. Runs
    # even when paused — an expiring position is risk maintenance, not a new
    # signal. A half-roll raises out of here and HALTs the tick.
    rolled = execute_rolls(runner, ib, contracts, today_et)
    if rolled:
        run.rolled_symbols = rolled

    cutoff = session_complete_cutoff(datetime.now(ET))
    new_bars, fetched, missing = fetch_all_new_bars(ib, contracts, today_et)
    # Never feed an in-progress session's bar (e.g. an off-hours --once run).
    incomplete = {s for s, b in new_bars.items() if b.ts.date() > cutoff}
    if incomplete:
        log.warning("dropping %d in-progress bars (session not complete): %s",
                    len(incomplete), sorted(incomplete))
        new_bars = {s: b for s, b in new_bars.items() if s not in incomplete}
        fetched = {s: d for s, d in fetched.items() if s not in incomplete}
    run.bars_fetched = fetched
    run.bars_missing = missing

    if getattr(args, "_paused", False):
        log.warning("PAUSED — skipping bar tick (clear pause via the menubar)")
        new_bars = {}  # process no bars; reconcile and log still happen

    pre_oids = {id(c): getattr(c.broker, "_next_id", 1) for c in runner.cells}

    runner.tick(new_bars)
    ib.sleep(2.0)

    # Orders placed: strictly those originating in this tick's runner.tick call.
    for cell in runner.cells:
        broker = cell.broker
        prev = pre_oids.get(id(cell), 1)
        cur = getattr(broker, "_next_id", prev)
        ib_trades = getattr(broker, "_trades", None)
        for oid in range(prev, cur):
            if ib_trades and oid in ib_trades:
                o = ib_trades[oid].order
                run.orders_placed.append(CellOrder(
                    strategy=cell.setup.strategy_name, symbol=cell.setup.symbol,
                    order_id=oid, side=o.action, qty=int(o.totalQuantity),
                    otype=o.orderType,
                    price=float(getattr(o, "lmtPrice", 0.0)
                                or getattr(o, "auxPrice", 0.0) or 0.0),
                ))

    # Fills: everything that landed since the prior tick's cursor — catches
    # late fills (e.g. after-hours orders that fill at next session open).
    late_fills = 0
    for cell in runner.cells:
        broker = cell.broker
        prior = fills_cursor.get(id(cell), 0)
        current = len(broker.fills)
        for f in broker.fills[prior:current]:
            run.fills_received.append(CellFill(
                strategy=cell.setup.strategy_name, symbol=cell.setup.symbol,
                order_id=f.order_id, ts=f.ts.isoformat(),
                side=f.side.name, qty=f.qty, price=f.price,
            ))
            if f.ts.replace(tzinfo=None) < datetime.fromisoformat(run.started_at).replace(tzinfo=None):
                late_fills += 1
        fills_cursor[id(cell)] = current

    run.ended_at = datetime.now(ET).isoformat()
    log.info("tick done: %d orders, %d fills%s",
             len(run.orders_placed), len(run.fills_received),
             f" ({late_fills} carried from prior ticks)" if late_fills else "")
    _write_log(args.logfile, run)
    return run


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=14)
    p.add_argument("--portfolio", type=float, default=300_000.0)
    p.add_argument("--risk-factor", type=float, default=0.001)
    # 18:15 ET is just after the 18:00 ET CME maintenance-halt reopen, so the
    # day's bar is already settled (>17:05) AND the market is live — MARKET
    # orders fill immediately instead of resting as _SENT through the
    # 17:00–18:00 ET halt (which a restart would then degrade to flat).
    p.add_argument("--run-hour", type=int, default=18, help="ET hour for daily tick")
    p.add_argument("--run-minute", type=int, default=15, help="ET minute for daily tick")
    p.add_argument("--halt-threshold", type=int, default=0)
    p.add_argument("--catch-up-bars", type=int, default=15,
                   help="Trailing daily bars to fetch for startup catch-up")
    p.add_argument("--no-first-run", action="store_true",
                   help="Skip the startup force-flat (use only if resuming with "
                        "positions you expect replay to reconstruct)")
    p.add_argument("--flatten-account", action="store_true",
                   help="Close all existing IB positions on startup (clean slate)")
    p.add_argument("--once", action="store_true",
                   help="Run one tick immediately, then exit (no loop)")
    p.add_argument("--skip-symbols", default="",
                   help="Comma-separated symbols to exclude entirely (no cells, "
                        "no fetch, no orders) — e.g. MBT,MET while their "
                        "front-month is near expiry and rolling isn't automated")
    p.add_argument("--logfile", default="logs/daily.jsonl")
    p.add_argument("--status-path", default=str(default_status_path()),
                   help="Where to write status.json (read by the menubar app)")
    p.add_argument("--command-path", default=str(default_command_path()),
                   help="Where to read command.json from (written by the menubar app)")
    p.add_argument("--state-path", default=str(default_state_path()),
                   help="Where to persist/restore path-dependent state across "
                        "restarts. If present and valid on startup, the loop "
                        "resumes from it instead of force-flatting.")
    p.add_argument("--exit-on-orphan", action="store_true",
                   help="Exit if the parent process dies (orphaning us). "
                        "Use under a supervisor (the SwiftUI app) so a hard "
                        "kill of the parent doesn't leave us holding the "
                        "clientId. Skip for nohup-style detached runs.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    args._skip = {s.strip().upper() for s in args.skip_symbols.split(",") if s.strip()}
    args._paused = False
    # Snapshot parent PID; if --exit-on-orphan and getppid() ever differs,
    # the supervisor died (probably ⌘R'd from Xcode) and we should exit
    # rather than survive as an orphan holding the IB clientId.
    args._original_ppid = os.getppid()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    state = {"ib": None}

    def reconnect():
        state["ib"] = connect_with_retry(args)
        return state["ib"]

    reconnect()
    ib = state["ib"]

    # ---- Startup ----
    if args._skip:
        log.warning("skipping symbols (no cells, no orders): %s", sorted(args._skip))
    setups = [s for s in build_setups() if s.symbol not in args._skip]

    symbols = sorted({s.symbol for s in setups})
    contracts = qualify_contracts(ib, symbols)
    if len(contracts) < len(symbols):
        log.warning("qualified %d/%d contracts; missing: %s",
                    len(contracts), len(symbols),
                    sorted(set(symbols) - set(contracts)))

    if args.flatten_account:
        flatten_account(ib)

    sim_runner = Runner.from_setups(
        setups, excluded=EXCLUDED_CELLS,
        portfolio_value_usd=args.portfolio, risk_factor=args.risk_factor,
    )
    log.info("replaying history through SimBroker…")
    sim_runner.replay_history()

    cutoff = session_complete_cutoff(datetime.now(ET))
    log.info("catching up completed sessions through %s…", cutoff)
    catch_up(sim_runner, ib, contracts, cutoff, args.catch_up_bars)

    def ib_broker_factory(setup: CellSetup):
        contract = contracts.get(setup.symbol)
        if contract is None:
            log.warning("no contract for %s; falling back to SimBroker", setup.symbol)
            return SimBroker(point_value=setup.point_value,
                             commission_per_contract=setup.commission)
        return IBBroker(ib, contract, point_value=setup.point_value)

    live_runner = Runner.from_setups(
        setups, excluded=EXCLUDED_CELLS,
        portfolio_value_usd=args.portfolio, risk_factor=args.risk_factor,
        broker_factory=ib_broker_factory,
    )
    transfer_warm_state(sim_runner, live_runner)

    # State persistence: if a valid state file exists, resume from it instead
    # of force-flatting. A corrupt/wrong-schema file is fatal — we refuse to
    # start rather than silently flatten (which could close real positions).
    try:
        persisted = load_state(args.state_path)
    except StatePersistenceError as e:
        ib.disconnect()
        raise SystemExit(
            f"FATAL: state file present but unusable: {e}\n"
            f"Resolve manually (inspect/repair/delete {args.state_path}) "
            f"then relaunch. Refusing to force-flat over real positions."
        )

    first_run = False
    if persisted is not None:
        log.warning("RESUMING from persisted state saved at %s",
                    persisted.get("saved_at"))
        summary = live_runner.apply_persisted_state(persisted["cells"])
        args._paused = bool(persisted.get("global", {}).get("paused", False))
        log.warning(
            "restored %d cells (%d degraded mid-order), %d in-state cells "
            "missing from runner, %d new cells left at default; paused=%s",
            len(summary["applied"]), len(summary["degraded"]),
            len(summary["skipped_missing_from_runner"]),
            len(summary["new_cells_left_default"]), args._paused,
        )
        if summary["degraded"]:
            log.warning("degraded (force-flat + reset to FLAT, reconcile will "
                        "HALT if IB shows a position): %s", summary["degraded"])
        if summary["skipped_missing_from_runner"]:
            log.warning("state had cells not in this runner (skipped): %s",
                        summary["skipped_missing_from_runner"])
        if summary["new_cells_left_default"]:
            log.warning("runner has cells not in state (left at replay default): %s",
                        summary["new_cells_left_default"])
    else:
        first_run = not args.no_first_run
        if first_run:
            log.warning("FIRST RUN: forcing all cells flat after replay+catch-up")
            live_runner.force_flat_all_cells()
            unstuck = live_runner.reset_inflight_strategies()
            if unstuck:
                log.warning("reset %d strategies stuck in *_SENT state", unstuck)

    actual = ibkr_positions_by_symbol(ib)
    report = live_runner.reconcile_against(actual, halt_threshold=args.halt_threshold)
    print("\n" + format_report(report))
    log.info("startup reconcile severity=%s", report.overall.value)

    # Fill cursor for cross-tick capture of late fills. On a cold start live
    # IBBrokers have empty .fills (transfer_warm_state does NOT copy fills); on
    # a resume they hold the restored fills, already logged by the prior
    # process. Either way the current length is the right starting cursor.
    fills_cursor = {id(c): len(c.broker.fills) for c in live_runner.cells}

    last_tick: dict | None = None
    next_tick: datetime | None = None

    def emit_status(status: str) -> None:
        try:
            write_status(args.status_path, build_status(
                runner=live_runner, ib=state["ib"], args=args,
                status=status, next_tick_at=next_tick, last_tick=last_tick,
            ))
        except Exception as e:
            log.warning("write_status failed: %s", e)

    # Command + status heartbeat: poll commands every chunk (~2s), refresh
    # status every ~30s. Closure mutates last_status_at across calls.
    last_status_at = [0.0]  # boxed so the nested fn can mutate

    def heartbeat() -> None:
        if args.exit_on_orphan and os.getppid() != args._original_ppid:
            log.warning("parent (PID %d) died; exiting so we don't hold the IB clientId",
                        args._original_ppid)
            _persist_state(live_runner, args)
            try:
                state["ib"].disconnect()
            except Exception:
                pass
            sys.exit(0)
        cmd = read_and_delete_command(args.command_path)
        if cmd is not None:
            process_command(cmd, runner=live_runner, ib=state["ib"], args=args)
            emit_status("paused" if args._paused else "waiting")
            last_status_at[0] = time.monotonic()
            return
        now = time.monotonic()
        if now - last_status_at[0] >= 30.0:
            emit_status("paused" if args._paused else "waiting")
            last_status_at[0] = now

    # On a clean shutdown (supervisor SIGTERM on app quit), persist one final
    # snapshot before exiting so the next launch resumes instead of flatting.
    def _on_sigterm(signum, frame):
        log.warning("SIGTERM received — saving state and exiting cleanly")
        _persist_state(live_runner, args)
        try:
            state["ib"].disconnect()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    emit_status("starting")

    # ---- Tick loop ----
    if args.once:
        once_run = do_tick(live_runner, ib, contracts, args, first_run, fills_cursor)
        last_tick = {
            "started_at": once_run.started_at,
            "ended_at": once_run.ended_at,
            "severity": once_run.reconcile_severity,
            "orders_placed": len(once_run.orders_placed),
            "fills_received": len(once_run.fills_received),
        }
        _persist_state(live_runner, args)
        emit_status("idle")
        state["ib"].disconnect()
        return 0

    log.info("entering daily tick loop (fires weekdays at %02d:%02d ET)",
             args.run_hour, args.run_minute)
    is_first_tick = True
    while True:
        next_tick = next_run_dt(datetime.now(ET), args.run_hour, args.run_minute)
        log.info("next tick at %s", next_tick.isoformat())
        emit_status("waiting")
        sleep_until(state["ib"], next_tick, reconnect, heartbeat=heartbeat)
        ib = state["ib"]
        if not ib.isConnected():
            reconnect()
            ib = state["ib"]
        emit_status("ticking")
        try:
            run = do_tick(live_runner, ib, contracts, args,
                          first_run and is_first_tick, fills_cursor)
            last_tick = {
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "severity": run.reconcile_severity,
                "orders_placed": len(run.orders_placed),
                "fills_received": len(run.fills_received),
            }
            is_first_tick = False
        except Exception as e:  # one bad tick shouldn't kill the process
            log.exception("tick failed: %s", e)
            last_tick = {"started_at": datetime.now(ET).isoformat(),
                         "ended_at": datetime.now(ET).isoformat(),
                         "severity": "error", "error": str(e),
                         "orders_placed": 0, "fills_received": 0}
        _persist_state(live_runner, args)
        emit_status("waiting")


if __name__ == "__main__":
    raise SystemExit(main())
