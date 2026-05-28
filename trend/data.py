import csv
from collections import deque
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .types import Bar

ET = ZoneInfo("America/New_York")


class ATR:
    """Rolling ATR. Returns None until `period` bars are seen, then average TR."""

    def __init__(self, period: int):
        self.period = period
        self._trs: deque[float] = deque(maxlen=period)
        self._prev_close: float | None = None
        self.value: float | None = None

    def update(self, bar: Bar) -> None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._trs.append(tr)
        self._prev_close = bar.close
        if len(self._trs) == self.period:
            self.value = sum(self._trs) / self.period


def load_bars_csv(path: str, default_tz: str = "UTC") -> list[Bar]:
    """Load OHLCV CSV. Columns: timestamp,open,high,low,close,volume.

    Timestamps may include a timezone offset; if not, `default_tz` is applied.
    """
    bars: list[Bar] = []
    tzinfo = ZoneInfo(default_tz)
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=tzinfo)
            bars.append(
                Bar(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row.get("volume", 0) or 0)),
                )
            )
    return bars


def daily_atr_series(bars: list[Bar], period: int = 14) -> dict[date, float]:
    """Aggregate RTH 1-min bars into daily OHLC (by ET date) and compute ATR.

    Returns ATR keyed by the session date the value applies to (today's ATR is
    based on history through *yesterday*, suitable for use as a same-day filter
    threshold).
    """
    daily: dict[date, dict[str, float]] = {}
    for b in bars:
        d = b.ts.astimezone(ET).date()
        if d not in daily:
            daily[d] = {"open": b.open, "high": b.high, "low": b.low, "close": b.close}
        else:
            daily[d]["high"] = max(daily[d]["high"], b.high)
            daily[d]["low"] = min(daily[d]["low"], b.low)
            daily[d]["close"] = b.close

    out: dict[date, float] = {}
    trs: deque[float] = deque(maxlen=period)
    prev_close: float | None = None
    for d in sorted(daily):
        dd = daily[d]
        # Today's ATR is yesterday's average — assign to *this* date BEFORE
        # updating with today's TR.
        if len(trs) == period:
            out[d] = sum(trs) / period
        if prev_close is None:
            tr = dd["high"] - dd["low"]
        else:
            tr = max(
                dd["high"] - dd["low"],
                abs(dd["high"] - prev_close),
                abs(dd["low"] - prev_close),
            )
        trs.append(tr)
        prev_close = dd["close"]
    return out
