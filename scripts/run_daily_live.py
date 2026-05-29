#!/usr/bin/env python3
"""Live (paper) daily tick: replay → reconcile → fetch → tick → log.

Designed to run once per session shortly after CME equity-index close
(17:00 ET). Recommended cron: `30 17 * * 1-5`.

First-run bootstrap: pass --first-run to force every cell to flat after
replay. Subsequent days reconcile against IB positions and refuse to act
if the diff exceeds --halt-threshold.

Output: human-readable to stdout + one JSON line appended to
logs/daily.jsonl per run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.all_strategies_backtest import EXCLUDED_CELLS, MARKETS  # noqa: E402
from trend.ib_broker import IBBroker  # noqa: E402
from trend.ib_data import fetch_daily_bars  # noqa: E402
from trend.reconcile import Severity, format_report, recommended_action  # noqa: E402
from trend.runner import CellSetup, Runner, transfer_warm_state  # noqa: E402
from trend.sim_broker import SimBroker  # noqa: E402
from trend.types import Bar  # noqa: E402

ET = ZoneInfo("America/New_York")
log = logging.getLogger("run_daily_live")

# Each market's IB primary exchange. The micro & mini share the same
# exchange (CME group). ZN/ZC/ZS are full-size on CBOT.
EXCHANGES: dict[str, str] = {
    "MES": "CME",  "MNQ": "CME",  "M2K": "CME",  "MYM": "CBOT",
    "MCL": "NYMEX","MGC": "COMEX",
    "M6E": "CME",  "M6B": "CME",  "MJY": "CME",  "M6A": "CME",
    "ZN":  "CBOT", "ZC":  "CBOT", "ZS":  "CBOT",
    "MBT": "CME",  "MET": "CME",
}


@dataclass
class CellOrder:
    strategy: str
    symbol: str
    order_id: int
    side: str
    qty: int
    otype: str
    price: float


@dataclass
class CellFill:
    strategy: str
    symbol: str
    order_id: int
    ts: str
    side: str
    qty: int
    price: float


@dataclass
class RunLog:
    started_at: str
    ended_at: str = ""
    host: str = ""
    port: int = 0
    account: str = ""
    first_run: bool = False
    portfolio_value_usd: float = 0.0
    risk_factor: float = 0.0
    cells_active: int = 0
    cells_excluded: int = 0
    today_et: str = ""
    bars_fetched: dict[str, str] = field(default_factory=dict)
    bars_missing: list[str] = field(default_factory=list)
    ibkr_positions: dict[str, int] = field(default_factory=dict)
    expected_positions: dict[str, int] = field(default_factory=dict)
    reconcile_severity: str = ""
    reconcile_halted: bool = False
    rolled_symbols: list[str] = field(default_factory=list)
    orders_placed: list[CellOrder] = field(default_factory=list)
    fills_received: list[CellFill] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def build_setups() -> list[CellSetup]:
    setups = []
    for strategy_name in ("CoreTrend", "TimeReturn", "CounterTrend"):
        for symbol, path, point_value, commission in MARKETS:
            setups.append(CellSetup(
                strategy_name=strategy_name,
                symbol=symbol,
                data_path=path,
                point_value=point_value,
                commission=commission,
            ))
    return setups


def qualify_contracts(ib: Any, symbols: list[str]) -> dict[str, Any]:
    """Qualify the front-month ContFuture for each symbol. Returns dict[symbol → contract]."""
    from ib_async import ContFuture

    out: dict[str, Any] = {}
    for sym in symbols:
        exchange = EXCHANGES.get(sym)
        if exchange is None:
            log.error("no exchange mapping for %s; skipping", sym)
            continue
        cont = ContFuture(symbol=sym, exchange=exchange, currency="USD")
        try:
            qualified = ib.qualifyContracts(cont)
        except Exception as e:
            log.error("qualifyContracts failed for %s on %s: %s", sym, exchange, e)
            continue
        if not qualified or not qualified[0].conId:
            log.error("could not qualify %s on %s", sym, exchange)
            continue
        out[sym] = qualified[0]
        log.info("qualified %s: %s exp %s",
                 sym, qualified[0].localSymbol,
                 qualified[0].lastTradeDateOrContractMonth)
    return out


def resolve_next_contract(
    ib: Any, symbol: str, exchange: str, after_expiry: str
) -> Any | None:
    """Qualify the futures contract whose expiry is immediately after
    `after_expiry`. ContFuture only ever resolves the front month, so we
    enumerate listed expirations via reqContractDetails and pick the successor.

    Returns a fully-qualified `Future` (with conId) or None if no later
    contract is listed / qualification fails.
    """
    from ib_async import Future

    from trend.roll import next_expiry

    try:
        details = ib.reqContractDetails(
            Future(symbol=symbol, exchange=exchange, currency="USD")
        )
    except Exception as e:
        log.error("reqContractDetails failed for %s on %s: %s", symbol, exchange, e)
        return None
    expiries = [d.contract.lastTradeDateOrContractMonth for d in details
                if d.contract.lastTradeDateOrContractMonth]
    nxt = next_expiry(expiries, after_expiry)
    if nxt is None:
        log.error("no expiry after %s for %s (listed: %s)",
                  after_expiry, symbol, sorted(set(expiries)))
        return None
    fut = Future(symbol=symbol, exchange=exchange, currency="USD",
                 lastTradeDateOrContractMonth=nxt)
    try:
        qualified = ib.qualifyContracts(fut)
    except Exception as e:
        log.error("qualifyContracts failed for %s %s: %s", symbol, nxt, e)
        return None
    if not qualified or not qualified[0].conId:
        log.error("could not qualify %s expiry %s", symbol, nxt)
        return None
    return qualified[0]


def compute_roll_basis(ib: Any, old_contract: Any, new_contract: Any) -> float:
    """Estimate the new − old contract price spread from each contract's most
    recent daily close. Returns 0.0 if either fetch is unusable (the roll then
    proceeds without rebasing — a small discontinuity, logged by the caller)."""
    try:
        old_bars = fetch_daily_bars(ib, old_contract, n_bars=1)
        new_bars = fetch_daily_bars(ib, new_contract, n_bars=1)
    except Exception as e:
        log.error("roll basis fetch failed: %s", e)
        return 0.0
    if not old_bars or not new_bars:
        return 0.0
    return new_bars[-1].close - old_bars[-1].close


def ibkr_positions_by_symbol(ib: Any) -> dict[str, int]:
    """Map IB positions back to our internal symbols.

    IB's `Position.contract.symbol` for futures returns the root symbol
    (e.g., "MES"), which matches our internal labels for micros. Full-size
    treasury/grain symbols (ZN/ZC/ZS) also match by root.
    """
    agg: dict[str, int] = {}
    for pos in ib.positions():
        sym = pos.contract.symbol
        qty = int(pos.position)
        if qty == 0:
            continue
        agg[sym] = agg.get(sym, 0) + qty
    return agg


def fetch_all_new_bars(
    ib: Any,
    contracts: dict[str, Any],
    today_et: date,
) -> tuple[dict[str, Bar], dict[str, str], list[str]]:
    """For each contract, fetch trailing bars and pick the latest one whose
    session date is >= today_et's prior session date. We DON'T filter on
    'completed' — the operator is expected to schedule after session close,
    at which point IB's last daily bar is finalized for today.

    Returns (new_bars_by_symbol, fetched_date_by_symbol, missing_symbols).
    """
    new_bars: dict[str, Bar] = {}
    fetched: dict[str, str] = {}
    missing: list[str] = []
    for sym, contract in contracts.items():
        try:
            bars = fetch_daily_bars(ib, contract, n_bars=3)
        except Exception as e:
            log.error("fetch_daily_bars failed for %s: %s", sym, e)
            missing.append(sym)
            continue
        if not bars:
            log.warning("no bars returned for %s", sym)
            missing.append(sym)
            continue
        last = bars[-1]
        new_bars[sym] = last
        fetched[sym] = last.ts.date().isoformat()
    return new_bars, fetched, missing


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=13)
    p.add_argument("--portfolio", type=float, default=300_000.0)
    p.add_argument("--risk-factor", type=float, default=0.001)
    p.add_argument("--first-run", action="store_true",
                   help="Force-flat all cells after replay (only use ONCE per fresh account)")
    p.add_argument("--halt-threshold", type=int, default=0,
                   help="Refuse to tick if any |expected - actual| > this")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + tick into ephemeral SimBrokers instead of IBBrokers; "
                        "useful for sanity-checking the pipeline without placing orders")
    p.add_argument("--logfile", default="logs/daily.jsonl")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    run = RunLog(
        started_at=datetime.now(ET).isoformat(),
        host=args.host, port=args.port,
        first_run=args.first_run,
        portfolio_value_usd=args.portfolio,
        risk_factor=args.risk_factor,
    )

    from ib_async import IB
    ib = IB()
    log.info("connecting to %s:%s clientId=%s", args.host, args.port, args.client_id)
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, readonly=False)
    except Exception as e:
        run.errors.append(f"connect failed: {e}")
        _write_log(args.logfile, run)
        log.error("connection failed: %s", e)
        return 1

    accounts = ib.managedAccounts()
    run.account = accounts[0] if accounts else ""
    if not any(a.startswith(("D", "DU", "DF")) for a in accounts):
        msg = f"REFUSING TO RUN on non-paper account: {accounts}"
        run.errors.append(msg)
        log.error(msg)
        ib.disconnect()
        _write_log(args.logfile, run)
        return 1
    log.info("connected to paper account: %s", run.account)

    # ---- Qualify contracts ----
    symbols = sorted({s.symbol for s in build_setups()})
    contracts = qualify_contracts(ib, symbols)
    if len(contracts) < len(symbols):
        run.errors.append(
            f"qualified {len(contracts)}/{len(symbols)}; missing: "
            f"{sorted(set(symbols) - set(contracts))}"
        )

    # ---- Phase 1: replay through SimBroker ----
    # We NEVER replay through IBBroker — that would submit thousands of real
    # orders to IB during startup. SimBroker is functionally equivalent for
    # warming up strategy internal state (EMAs, std, position bookkeeping).
    setups = build_setups()
    sim_runner = Runner.from_setups(
        setups, excluded=EXCLUDED_CELLS,
        portfolio_value_usd=args.portfolio, risk_factor=args.risk_factor,
    )
    run.cells_active = len(sim_runner.cells)
    run.cells_excluded = len(EXCLUDED_CELLS)
    log.info("built %d cells (%d excluded); replaying history through SimBroker…",
             run.cells_active, run.cells_excluded)
    sim_runner.replay_history()
    expected = sim_runner.positions_by_symbol()
    run.expected_positions = {k: v for k, v in expected.items() if v != 0}
    log.info("post-replay expected positions: %s", run.expected_positions)

    # ---- Phase 2: swap to IBBroker for live tick (unless --dry-run) ----
    if args.dry_run:
        log.info("DRY RUN: continuing with SimBroker; no orders will reach IB")
        runner = sim_runner
    else:
        def ib_broker_factory(setup: CellSetup):
            contract = contracts.get(setup.symbol)
            if contract is None:
                log.warning("no contract for %s; falling back to SimBroker",
                            setup.symbol)
                return SimBroker(point_value=setup.point_value,
                                 commission_per_contract=setup.commission)
            return IBBroker(ib, contract, point_value=setup.point_value)

        live_runner = Runner.from_setups(
            setups, excluded=EXCLUDED_CELLS,
            portfolio_value_usd=args.portfolio, risk_factor=args.risk_factor,
            broker_factory=ib_broker_factory,
        )
        transfer_warm_state(sim_runner, live_runner)
        runner = live_runner

    # ---- Reconcile against IBKR ----
    actual = ibkr_positions_by_symbol(ib)
    run.ibkr_positions = dict(actual)
    report = runner.reconcile_against(actual, halt_threshold=args.halt_threshold)
    run.reconcile_severity = report.overall.value
    print("\n" + format_report(report))
    print("\n" + recommended_action(report))

    # ---- First-run bootstrap ----
    if args.first_run:
        log.warning("FIRST RUN: forcing all cells flat after replay")
        runner.force_flat_all_cells()
        unstuck = runner.reset_inflight_strategies()
        if unstuck:
            log.warning("reset %d strategies stuck in *_SENT state", unstuck)
        run.expected_positions = {}
    elif report.overall is Severity.HALT:
        run.reconcile_halted = True
        run.errors.append("reconciliation severity=HALT (mismatch beyond threshold)")
        log.error("reconciliation mismatch; HALTING before tick")
        _finalize_and_disconnect(ib, args.logfile, run)
        return 2

    # ---- Fetch today's bars + tick ----
    today_et = datetime.now(ET).date()
    run.today_et = today_et.isoformat()
    new_bars, fetched, missing = fetch_all_new_bars(ib, contracts, today_et)
    run.bars_fetched = fetched
    run.bars_missing = missing
    log.info("fetched bars for %d symbols (missing: %s)", len(new_bars), missing)

    # Snapshot pre-tick state. We diff after the tick to capture orders
    # placed and fills received DURING the tick (without overwriting the
    # strategy's own fill callback).
    pre_tick_oids = {id(cell): getattr(cell.broker, "_next_id", 1)
                     for cell in runner.cells}
    pre_tick_fills = {id(cell): len(cell.broker.fills) for cell in runner.cells}

    log.info("running daily tick…")
    runner.tick(new_bars)

    # Allow time for market orders to fill (only meaningful for live mode).
    if not args.dry_run:
        ib.sleep(2.0)

    # Capture orders placed during the tick by diffing _next_id.
    for cell in runner.cells:
        broker = cell.broker
        prev = pre_tick_oids.get(id(cell), 1)
        cur = getattr(broker, "_next_id", prev)
        if cur <= prev:
            continue
        ib_trades = getattr(broker, "_trades", None)
        sim_orders = getattr(broker, "orders", None)
        for oid in range(prev, cur):
            if ib_trades and oid in ib_trades:
                o = ib_trades[oid].order
                run.orders_placed.append(CellOrder(
                    strategy=cell.setup.strategy_name,
                    symbol=cell.setup.symbol,
                    order_id=oid,
                    side=o.action,
                    qty=int(o.totalQuantity),
                    otype=o.orderType,
                    price=float(getattr(o, "lmtPrice", 0.0)
                                or getattr(o, "auxPrice", 0.0) or 0.0),
                ))
            elif sim_orders and oid in sim_orders:
                so = sim_orders[oid]
                run.orders_placed.append(CellOrder(
                    strategy=cell.setup.strategy_name,
                    symbol=cell.setup.symbol,
                    order_id=oid,
                    side=so.side.name,
                    qty=so.qty,
                    otype=so.type.value,
                    price=so.price,
                ))

    # Capture fills received during the tick by diffing broker.fills.
    for cell in runner.cells:
        snap = pre_tick_fills.get(id(cell), 0)
        for f in cell.broker.fills[snap:]:
            run.fills_received.append(CellFill(
                strategy=cell.setup.strategy_name,
                symbol=cell.setup.symbol,
                order_id=f.order_id,
                ts=f.ts.isoformat(),
                side=f.side.name,
                qty=f.qty,
                price=f.price,
            ))

    log.info("orders placed: %d; fills received: %d",
             len(run.orders_placed), len(run.fills_received))

    # ---- Per-cell report ----
    print(f"\n{'='*90}")
    print(f"Per-cell state (post-tick)")
    print(f"{'='*90}")
    print(f"{'strategy':<14}{'symbol':<6}{'qty':>6}{'avg_price':>12}{'state':<14}"
          f"{'trades':>8} last_bar")
    print("-" * 90)
    for s in runner.report():
        last = s["last_processed"].isoformat() if s["last_processed"] else "—"
        print(f"{s['strategy']:<14}{s['symbol']:<6}{s['position_qty']:>6}"
              f"{s['position_avg']:>12,.2f}{s['strategy_state'] or '—':<14}"
              f"{s['trades_recorded']:>8}  {last}")

    _finalize_and_disconnect(ib, args.logfile, run)
    return 0


def _finalize_and_disconnect(ib, logfile: str, run: RunLog) -> None:
    run.ended_at = datetime.now(ET).isoformat()
    try:
        ib.disconnect()
    except Exception as e:
        run.errors.append(f"disconnect failed: {e}")
    _write_log(logfile, run)


def _write_log(path: str, run: RunLog) -> None:
    logfile = Path(path)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as f:
        f.write(json.dumps(asdict(run), default=str) + "\n")
    log.info("appended run log to %s", logfile)


if __name__ == "__main__":
    raise SystemExit(main())
