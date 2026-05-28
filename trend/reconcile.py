"""Position reconciliation between cell-expected and IBKR-actual.

v1 is REPORT-ONLY. Compare what cells collectively want (after replay) against
what IBKR actually shows, produce a human-readable diff, and a structured list
of mismatches. The caller decides what to do — halt, prompt the user,
force-flatten cells, or place catchup orders.

We deliberately don't auto-catch-up in v1 because:
  - First-run scenario (IBKR=0, cells=replay state) calls for resetting cells,
    not placing catchup orders.
  - Cells track position via their own fills; catchup orders placed outside
    any cell would put cells and IBKR permanently out of sync.

Once we have weeks of live operation, we'll know which mismatch scenarios are
common and design auto-actions for them.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(Enum):
    OK = "ok"               # no mismatch
    WARN = "warn"           # mismatch but ≤ threshold
    HALT = "halt"           # mismatch beyond threshold


@dataclass(frozen=True)
class Delta:
    symbol: str
    expected: int           # cells' aggregate signed position
    actual: int             # IBKR's signed position
    delta: int              # expected − actual (what would need to be bought)
    severity: Severity


@dataclass(frozen=True)
class Report:
    deltas: list[Delta]
    overall: Severity


def compute(
    expected: dict[str, int],
    actual: dict[str, int],
    halt_threshold: int = 0,
) -> Report:
    """Compare expected vs actual positions.

    Args:
        expected: cells' aggregate signed position per symbol (from
            Runner.positions_by_symbol())
        actual: IBKR's signed position per symbol (from ib.positions())
        halt_threshold: |delta| above this is severity HALT. 0 means any
            mismatch is HALT. Set to a small number (e.g. 1) to allow
            single-contract rounding differences.

    Symbols present in only one dict are treated as if the other side were 0.
    """
    all_symbols = set(expected) | set(actual)
    deltas: list[Delta] = []
    worst = Severity.OK
    for sym in sorted(all_symbols):
        exp = expected.get(sym, 0)
        act = actual.get(sym, 0)
        d = exp - act
        if d == 0:
            sev = Severity.OK
        elif abs(d) <= halt_threshold:
            sev = Severity.WARN
            worst = _worsen(worst, sev)
        else:
            sev = Severity.HALT
            worst = _worsen(worst, sev)
        deltas.append(Delta(symbol=sym, expected=exp, actual=act, delta=d, severity=sev))
    return Report(deltas=deltas, overall=worst)


def _worsen(a: Severity, b: Severity) -> Severity:
    order = {Severity.OK: 0, Severity.WARN: 1, Severity.HALT: 2}
    return a if order[a] >= order[b] else b


def format_report(report: Report) -> str:
    lines = []
    lines.append(f"{'symbol':<8}{'expected':>10}{'actual':>10}{'delta':>8}  severity")
    lines.append("-" * 50)
    any_mismatch = False
    for d in report.deltas:
        if d.severity is not Severity.OK:
            any_mismatch = True
        lines.append(
            f"{d.symbol:<8}{d.expected:>+10d}{d.actual:>+10d}{d.delta:>+8d}  {d.severity.value}"
        )
    lines.append("-" * 50)
    lines.append(f"Overall: {report.overall.value.upper()}")
    if not any_mismatch:
        lines.append("All positions reconcile cleanly.")
    return "\n".join(lines)


def recommended_action(report: Report) -> str:
    """A human-readable recommendation based on the report."""
    if report.overall is Severity.OK:
        return "No action needed — IBKR positions match what cells expect."
    if report.overall is Severity.WARN:
        return ("Minor mismatch detected. Review the report; consider manual "
                "alignment if it persists across runs.")
    # HALT
    halt_deltas = [d for d in report.deltas if d.severity is Severity.HALT]
    actions = []
    actions.append(
        "Large position mismatch. DO NOT auto-trade — investigate first.\n"
        "Common causes and fixes:\n"
        "  (a) First-ever run: cells expect positions from replay, but IBKR is "
        "flat. Recommended action: reset cells to flat (force_flat) after replay, "
        "let strategies enter from scratch on next signal.\n"
        "  (b) Missed fill: IBKR holds a position from a prior run that cells "
        "don't know about. Recommended action: manually flatten in IBKR, restart "
        "runner, replay should align.\n"
        "  (c) Manual intervention: someone traded outside the runner. "
        "Recommended action: same as (b)."
    )
    actions.append("\nSpecific mismatches requiring action:")
    for d in halt_deltas:
        if d.actual == 0 and d.expected != 0:
            actions.append(
                f"  {d.symbol}: cells expect {d.expected:+d}, IBKR is flat. "
                f"Likely first-run scenario or missed fills."
            )
        elif d.expected == 0 and d.actual != 0:
            actions.append(
                f"  {d.symbol}: cells are flat but IBKR holds {d.actual:+d}. "
                f"Stale position from prior run."
            )
        else:
            actions.append(
                f"  {d.symbol}: cells {d.expected:+d}, IBKR {d.actual:+d}, "
                f"delta {d.delta:+d}."
            )
    return "\n".join(actions)
