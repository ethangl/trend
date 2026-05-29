"""Detect when a futures contract is approaching expiry and needs to be rolled.

Pure date math — no IB dependency. The runner uses this to surface roll
warnings in its daily report. Actual execution of the roll (close old, open
new, migrate position) is intentionally NOT here. Roll execution is a manual
operation in v1 — the consequences of a buggy auto-roll are high enough that
we want a human to push the button.

Default thresholds (`warn_days=14`, `roll_days=7`) target financial contracts
(equity indexes, currencies, rates) whose last-trade-date is what matters.
Commodities have a First Notice Day (FND) that comes *before* expiry; pass a
larger `roll_days` (e.g. 15-20) for those to avoid getting auto-flattened by
IBKR before FND.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Severity(Enum):
    OK = "ok"
    WARN = "warn"
    ROLL_NOW = "roll_now"


@dataclass(frozen=True)
class ContractInfo:
    """Minimum info we need to evaluate roll status."""
    symbol: str           # e.g. "MES"
    contract_label: str   # e.g. "MESU6" or "ESU6"
    last_trade_date: date # contract's last trading day


@dataclass(frozen=True)
class RollWarning:
    symbol: str
    contract_label: str
    last_trade_date: date
    days_until_expiry: int
    severity: Severity


def days_until(target: date, today: date) -> int:
    return (target - today).days


def parse_ib_expiry(s: str) -> date:
    """Parse an IB `lastTradeDateOrContractMonth` into a date.

    IB returns "YYYYMMDD" for qualified futures and sometimes "YYYYMM" for
    contract-month-only specs. A month-only value is treated as the 1st.
    """
    s = s.strip()
    if len(s) >= 8:
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    if len(s) == 6:
        return date(int(s[0:4]), int(s[4:6]), 1)
    raise ValueError(f"unrecognized IB expiry: {s!r}")


def _expiry_key(s: str) -> str:
    """Sortable 8-char key so "YYYYMM" and "YYYYMMDD" order together."""
    return s.strip().ljust(8, "0")[:8]


def next_expiry(expiries: list[str], current: str) -> str | None:
    """Pick the expiration immediately after `current` from `expiries`.

    All values are IB `lastTradeDateOrContractMonth` strings. Returns the
    chronologically-next one strictly after `current`, or None if there is no
    later contract in the list.
    """
    cur_key = _expiry_key(current)
    later = sorted(
        (e for e in expiries if _expiry_key(e) > cur_key),
        key=_expiry_key,
    )
    return later[0] if later else None


def evaluate(
    info: ContractInfo,
    today: date,
    warn_days: int = 14,
    roll_days: int = 7,
) -> RollWarning:
    """Classify roll urgency for a single contract.

    Args:
        info: contract specification
        today: reference date (typically today's date)
        warn_days: severity WARN if expiry is within this many days
        roll_days: severity ROLL_NOW if expiry is within this many days
            (commodities — use larger value to clear First Notice Day)
    """
    days = days_until(info.last_trade_date, today)
    if days <= roll_days:
        sev = Severity.ROLL_NOW
    elif days <= warn_days:
        sev = Severity.WARN
    else:
        sev = Severity.OK
    return RollWarning(
        symbol=info.symbol,
        contract_label=info.contract_label,
        last_trade_date=info.last_trade_date,
        days_until_expiry=days,
        severity=sev,
    )


def evaluate_all(
    infos: list[ContractInfo],
    today: date,
    warn_days: int = 14,
    roll_days: int = 7,
    commodity_symbols: set[str] | None = None,
    commodity_roll_days: int = 20,
    commodity_warn_days: int = 30,
) -> list[RollWarning]:
    """Evaluate a list of contracts. Symbols in `commodity_symbols` get the
    longer commodity thresholds to account for First Notice Day."""
    commodity_symbols = commodity_symbols or set()
    warnings = []
    for info in infos:
        if info.symbol in commodity_symbols:
            warnings.append(evaluate(info, today, commodity_warn_days, commodity_roll_days))
        else:
            warnings.append(evaluate(info, today, warn_days, roll_days))
    return warnings


def format_warnings(warnings: list[RollWarning]) -> str:
    if not warnings:
        return "No contracts to check."
    lines = []
    lines.append(f"{'symbol':<8}{'contract':<10}{'last_trade':<14}{'days':>6}  severity")
    lines.append("-" * 50)
    for w in warnings:
        lines.append(
            f"{w.symbol:<8}{w.contract_label:<10}"
            f"{w.last_trade_date.isoformat():<14}{w.days_until_expiry:>6}  "
            f"{w.severity.value}"
        )
    return "\n".join(lines)


def needs_action(warnings: list[RollWarning]) -> list[RollWarning]:
    """Return only WARN or ROLL_NOW warnings (filter out OK)."""
    return [w for w in warnings if w.severity is not Severity.OK]
