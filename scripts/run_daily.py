#!/usr/bin/env python3
"""Replay historical bars through every cell and report the current portfolio state.

This is the dry-run version of the live runner. It does NOT connect to IBKR
yet — that comes after paper is open and we've smoke-tested IBBroker. Output
tells you what each cell thinks it should be holding right now.

Usage:
    .venv/bin/python scripts/run_daily.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.all_strategies_backtest import EXCLUDED_CELLS, MARKETS  # noqa: E402
from trend.runner import CellSetup, Runner  # noqa: E402


def build_setups() -> list[CellSetup]:
    """Cross-product of strategies × markets, mirroring backtest config."""
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--portfolio", type=float, default=300_000.0)
    p.add_argument("--risk-factor", type=float, default=0.001)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    print(f"Portfolio:    ${args.portfolio:,.0f}")
    print(f"Risk factor:  {args.risk_factor:.4f}")
    print()

    runner = Runner.from_setups(
        build_setups(),
        excluded=EXCLUDED_CELLS,
        portfolio_value_usd=args.portfolio,
        risk_factor=args.risk_factor,
    )
    print(f"Cells: {len(runner.cells)} (excluded {len(EXCLUDED_CELLS)})\n")

    print("Replaying history…")
    runner.replay_history()
    print()

    # Per-cell state report
    print(f"{'strategy':<14}{'symbol':<6}{'qty':>6}{'avg_price':>12}"
          f"{'state':<14}{'trades':>8}{'last_bar'}")
    print("-" * 78)
    for s in runner.report():
        last = s["last_processed"].isoformat() if s["last_processed"] else "—"
        print(f"{s['strategy']:<14}{s['symbol']:<6}{s['position_qty']:>6}"
              f"{s['position_avg']:>12,.2f}"
              f"{s['strategy_state'] or '—':<14}{s['trades_recorded']:>8}  {last}")

    # Aggregated target positions per symbol (what we'd want in IBKR overall)
    print(f"\n{'='*60}")
    print("Aggregated target positions per symbol")
    print(f"{'='*60}")
    print(f"{'symbol':<6}{'net_qty':>10}{'cells_contributing':>22}")
    agg = runner.positions_by_symbol()
    by_symbol: dict[str, list[str]] = {}
    for cell in runner.cells:
        if cell.broker.position().qty != 0:
            by_symbol.setdefault(cell.setup.symbol, []).append(
                f"{cell.setup.strategy_name}={cell.broker.position().qty:+d}"
            )
    for sym in sorted(agg):
        contribs = by_symbol.get(sym, [])
        contrib_str = ", ".join(contribs) if contribs else "(all flat)"
        print(f"{sym:<6}{agg[sym]:>+10d}  {contrib_str}")

    # Reconciliation preview: simulate IBKR being flat (first-run scenario)
    print(f"\n{'='*60}")
    print("Reconciliation preview — assuming IBKR is flat (first-run scenario)")
    print(f"{'='*60}")
    from trend.reconcile import format_report, recommended_action  # noqa: E402
    report = runner.reconcile_against(ibkr_positions={}, halt_threshold=0)
    print(format_report(report))
    print()
    print(recommended_action(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
