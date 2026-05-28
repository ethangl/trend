from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trend.sim_broker import SimBroker
from trend.types import Bar, OrderType, Side

ET = ZoneInfo("America/New_York")


def bar(ts, o, h, l, c):
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def test_long_stop_fills_when_high_crosses():
    b = SimBroker()
    fills = []
    b.set_on_fill(lambda f: fills.append(f))
    ts = datetime(2026, 1, 6, 15, 0, tzinfo=ET)
    oid = b.place_order(Side.LONG, 1, OrderType.STOP, 5500.25)

    # Bar that doesn't reach the stop
    b.on_bar(bar(ts, 5498, 5500.0, 5497, 5499))
    assert fills == []
    # Bar that crosses
    b.on_bar(bar(ts + timedelta(minutes=1), 5499.5, 5501.0, 5499.0, 5500.5))
    assert len(fills) == 1
    assert fills[0].price == 5500.25
    assert b.position_qty == 1


def test_oco_cancels_sibling_on_fill():
    b = SimBroker()
    ts = datetime(2026, 1, 6, 15, 0, tzinfo=ET)
    long_id = b.place_order(Side.LONG, 1, OrderType.STOP, 5500.0, oco_group=1)
    short_id = b.place_order(Side.SHORT, 1, OrderType.STOP, 5490.0, oco_group=1)
    b.on_bar(bar(ts, 5495, 5501, 5494, 5500))
    assert b.orders[long_id].active is False  # filled
    assert b.orders[short_id].active is False  # cancelled by OCO


def test_round_trip_pnl_and_commissions():
    b = SimBroker(point_value=5.0, commission_per_contract=0.50)
    ts = datetime(2026, 1, 6, 15, 0, tzinfo=ET)
    # Enter long at 5500
    b.place_order(Side.LONG, 2, OrderType.STOP, 5500.0)
    b.on_bar(bar(ts, 5499, 5501, 5499, 5500.5))
    assert b.position_qty == 2
    # Exit at 5510 via opposing stop (sell stop above market not realistic, but
    # the sim just checks price crossings; use a MARKET instead for clarity)
    b.place_order(Side.SHORT, 2, OrderType.MARKET, 0.0)
    b.on_bar(bar(ts + timedelta(minutes=1), 5510, 5511, 5509, 5510))
    # Gross: 2 contracts * (5510 - 5500) * $5 = $100. Comm: 4 * $0.50 = $2.
    assert b.position_qty == 0
    assert b.total_realized == 100.0 - 2.0


def test_partial_reduction_preserves_entry_basis():
    """When closing part of a long position, the remaining contracts must
    still be priced from the original entry — not the partial-exit price."""
    b = SimBroker(point_value=5.0, commission_per_contract=0.0)
    ts = datetime(2026, 1, 6, 15, 0, tzinfo=ET)
    # Enter long 4 @ 5000
    b.place_order(Side.LONG, 4, OrderType.MARKET, 0.0)
    b.on_bar(bar(ts, 5000, 5001, 4999, 5000.5))
    assert b.position_qty == 4 and b.position_avg == 5000.0
    # Partial close: sell 2 @ 5010 (limit fills via bar high)
    b.place_order(Side.SHORT, 2, OrderType.LIMIT, 5010.0)
    b.on_bar(bar(ts + timedelta(minutes=1), 5005, 5012, 5004, 5010.5))
    assert b.position_qty == 2
    assert b.position_avg == 5000.0  # ← the bug-fix assertion
    # Close remaining 2 @ 5000 (round-trip)
    b.place_order(Side.SHORT, 2, OrderType.MARKET, 0.0)
    b.on_bar(bar(ts + timedelta(minutes=2), 5000, 5000.5, 4999.5, 5000))
    # Realized: 2*(5010-5000)*$5 + 2*(5000-5000)*$5 = $100
    assert b.total_realized == 100.0


def test_modify_stop_changes_trigger_price():
    b = SimBroker()
    ts = datetime(2026, 1, 6, 15, 0, tzinfo=ET)
    oid = b.place_order(Side.SHORT, 1, OrderType.STOP, 5490.0)
    b.modify_stop(oid, 5495.0)
    b.on_bar(bar(ts, 5498, 5500, 5494.5, 5496))  # crosses 5495 (the new stop)
    assert b.position_qty == -1
