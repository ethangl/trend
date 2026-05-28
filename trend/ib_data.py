"""Fetch historical daily bars from IBKR for use by the live runner.

The runner replays CSV history on startup, then needs the latest completed
daily bar per market each day. This module talks to ib_async's
`reqHistoricalData` and converts the result to our `Bar` type so the runner
sees the same shape it gets from CSV.

Kept separate from ib_broker so the broker stays a thin order-routing adapter.
"""
from __future__ import annotations

from datetime import date as date_t, datetime, time, timezone
from typing import Any

from .types import Bar

UTC = timezone.utc


def _to_bar(b: Any) -> Bar:
    """Convert an ib_async BarData to our Bar.

    For daily bars `b.date` is a `date`; for intraday it's a `datetime`. We
    stamp daily bars at UTC midnight of the session date to match the
    Databento CSV convention (`2010-06-07T00:00:00+00:00`).
    """
    raw = b.date
    if isinstance(raw, datetime):
        ts = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    else:
        ts = datetime.combine(raw, time(0, 0), tzinfo=UTC)
    return Bar(
        ts=ts,
        open=float(b.open),
        high=float(b.high),
        low=float(b.low),
        close=float(b.close),
        volume=int(b.volume) if b.volume is not None else 0,
    )


def fetch_daily_bars(
    ib: Any,
    contract: Any,
    n_bars: int = 2,
    what_to_show: str = "TRADES",
) -> list[Bar]:
    """Fetch the last `n_bars` daily bars for `contract`.

    Returns oldest → newest. Uses RTH=False (full futures session). The most
    recent bar may correspond to today's still-incomplete session; callers
    that need a *completed* bar should pop the last bar if its date equals
    today (in the exchange's tz) and the session isn't closed.

    Args:
        ib: connected ib_async.IB
        contract: a qualified futures contract (Future or ContFuture)
        n_bars: how many trailing daily bars to request. We pad the duration
            by +1 day because IB sometimes returns one fewer than requested
            depending on time-of-day relative to session close.
        what_to_show: "TRADES" for futures (typical). Other valid values:
            "MIDPOINT", "BID", "ASK".
    """
    if n_bars < 1:
        raise ValueError("n_bars must be >= 1")

    duration = f"{n_bars + 1} D"
    raw = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="1 day",
        whatToShow=what_to_show,
        useRTH=False,
        formatDate=1,
    )
    bars = [_to_bar(b) for b in raw]
    return bars[-n_bars:] if len(bars) > n_bars else bars


def latest_completed_bar(
    bars: list[Bar],
    today: date_t,
) -> Bar | None:
    """Return the most recent bar whose session date is strictly before `today`.

    `today` should be the date the operator considers "still open" in the
    exchange's tz. For US futures around 17:00 ET we treat the day's bar as
    not-yet-complete until ~17:00 ET (session close + IB publish lag).
    """
    for bar in reversed(bars):
        if bar.ts.date() < today:
            return bar
    return None
