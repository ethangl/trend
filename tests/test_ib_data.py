from datetime import date, datetime, timezone
from types import SimpleNamespace

from trend.ib_data import _to_bar, fetch_daily_bars, latest_completed_bar


def _bardata(d, o, h, l, c, v=1000):
    return SimpleNamespace(date=d, open=o, high=h, low=l, close=c, volume=v)


class FakeIB:
    """Stand-in for ib_async.IB capturing reqHistoricalData args."""

    def __init__(self, bars):
        self._bars = bars
        self.calls: list[dict] = []

    def reqHistoricalData(self, contract, **kwargs):
        self.calls.append({"contract": contract, **kwargs})
        return self._bars


def test_to_bar_daily_date_becomes_utc_midnight():
    bar = _to_bar(_bardata(date(2026, 5, 26), 100.0, 101.0, 99.0, 100.5))
    assert bar.ts == datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 101.0, 99.0, 100.5)
    assert bar.volume == 1000


def test_to_bar_intraday_datetime_preserved():
    raw_ts = datetime(2026, 5, 26, 15, 30, tzinfo=timezone.utc)
    bar = _to_bar(_bardata(raw_ts, 1, 2, 0.5, 1.5))
    assert bar.ts == raw_ts


def test_to_bar_naive_datetime_assumed_utc():
    naive = datetime(2026, 5, 26, 15, 30)
    bar = _to_bar(_bardata(naive, 1, 2, 0.5, 1.5))
    assert bar.ts.tzinfo is timezone.utc


def test_to_bar_none_volume_becomes_zero():
    bar = _to_bar(_bardata(date(2026, 5, 26), 1, 2, 0.5, 1.5, v=None))
    assert bar.volume == 0


def test_fetch_daily_bars_requests_one_day_resolution_no_rth():
    bars_in = [_bardata(date(2026, 5, 24), 1, 2, 0, 1.5),
               _bardata(date(2026, 5, 25), 2, 3, 1, 2.5),
               _bardata(date(2026, 5, 26), 3, 4, 2, 3.5)]
    ib = FakeIB(bars_in)
    out = fetch_daily_bars(ib, contract="MES-CONTRACT", n_bars=2)

    assert len(ib.calls) == 1
    call = ib.calls[0]
    assert call["contract"] == "MES-CONTRACT"
    assert call["barSizeSetting"] == "1 day"
    assert call["useRTH"] is False
    assert call["whatToShow"] == "TRADES"
    assert call["durationStr"] == "3 D"  # n_bars + 1 padding
    # Returns oldest → newest, trimmed to n_bars
    assert len(out) == 2
    assert [b.ts.date() for b in out] == [date(2026, 5, 25), date(2026, 5, 26)]


def test_fetch_daily_bars_returns_all_when_fewer_than_requested():
    bars_in = [_bardata(date(2026, 5, 26), 1, 2, 0, 1.5)]
    ib = FakeIB(bars_in)
    out = fetch_daily_bars(ib, contract="MES", n_bars=5)
    assert len(out) == 1


def test_latest_completed_bar_skips_today():
    bars = [
        _to_bar(_bardata(date(2026, 5, 22), 1, 2, 0, 1)),
        _to_bar(_bardata(date(2026, 5, 25), 2, 3, 1, 2)),
        _to_bar(_bardata(date(2026, 5, 26), 3, 4, 2, 3)),  # today
    ]
    latest = latest_completed_bar(bars, today=date(2026, 5, 26))
    assert latest is not None
    assert latest.ts.date() == date(2026, 5, 25)


def test_latest_completed_bar_returns_last_when_all_in_past():
    bars = [
        _to_bar(_bardata(date(2026, 5, 22), 1, 2, 0, 1)),
        _to_bar(_bardata(date(2026, 5, 25), 2, 3, 1, 2)),
    ]
    latest = latest_completed_bar(bars, today=date(2026, 5, 26))
    assert latest is not None
    assert latest.ts.date() == date(2026, 5, 25)


def test_latest_completed_bar_none_when_only_today():
    bars = [_to_bar(_bardata(date(2026, 5, 26), 1, 2, 0, 1))]
    assert latest_completed_bar(bars, today=date(2026, 5, 26)) is None
