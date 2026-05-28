from collections import defaultdict
from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .types import Bar, Fill, Order, OrderType, Position, Side

ET = ZoneInfo("America/New_York")


class SimBroker:
    """Bar-driven simulator. Feed bars via on_bar(); fills emit to on_fill cb.

    Order semantics:
      MARKET — fills at next bar's open.
      STOP   — buy fills when high >= price; sell when low <= price.
               Fill price = price (no slippage in v1). If the bar opens past
               the stop, fills at the open.
      LIMIT  — buy fills when low <= price; sell when high >= price.

    OCO: orders sharing an oco_group cancel each other on first fill.
    """

    def __init__(self, point_value: float = 5.0, commission_per_contract: float = 0.62):
        self.point_value = point_value
        self.commission = commission_per_contract
        self.orders: dict[int, Order] = {}
        self._next_id = 1
        self.position_qty: int = 0
        self.position_avg: float = 0.0
        self.fills: list[Fill] = []
        self.daily_realized: dict[date, float] = defaultdict(float)
        self.total_realized: float = 0.0
        self._on_fill: Callable[[Fill], None] | None = None

    def place_order(self, side, qty, otype, price, oco_group=None) -> int:
        oid = self._next_id
        self._next_id += 1
        self.orders[oid] = Order(
            id=oid, side=side, qty=qty, type=otype, price=price, oco_group=oco_group
        )
        return oid

    def cancel(self, oid: int) -> None:
        if oid in self.orders:
            self.orders[oid].active = False

    def modify_stop(self, oid: int, new_price: float) -> None:
        o = self.orders.get(oid)
        if o is not None and o.active and o.type is OrderType.STOP:
            o.price = new_price

    def position(self) -> Position:
        return Position(qty=self.position_qty, avg_price=self.position_avg)

    def set_on_fill(self, cb: Callable[[Fill], None]) -> None:
        self._on_fill = cb

    def get_order_price(self, oid: int) -> float | None:
        o = self.orders.get(oid)
        return o.price if o is not None else None

    def force_close(self, price: float, ts: datetime) -> None:
        """Synthetic close of the entire position at `price`. Used when the
        bar stream ends without a natural flatten (early-close days). No
        commission is charged — this is an accounting event, not a real fill.
        """
        if self.position_qty == 0:
            return
        direction = 1 if self.position_qty > 0 else -1
        realized = (
            (price - self.position_avg)
            * direction
            * abs(self.position_qty)
            * self.point_value
        )
        session_date = ts.astimezone(ET).date()
        self.daily_realized[session_date] += realized
        self.total_realized += realized
        self.position_qty = 0
        self.position_avg = 0.0

    # --- driver ----------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        # Ambiguity resolution: assume bullish bars traced open→high→low→close
        # and bearish bars traced open→low→high→close. Process "up-triggered"
        # orders first on bullish bars, "down-triggered" first on bearish.
        # Up-triggered: buy STOP (above), sell LIMIT (above).
        # Down-triggered: sell STOP (below), buy LIMIT (below).
        bullish = bar.close >= bar.open

        def is_up_trigger(o: Order) -> bool:
            if o.type is OrderType.STOP:
                return o.side is Side.LONG
            if o.type is OrderType.LIMIT:
                return o.side is Side.SHORT
            return True  # market — irrelevant, gets opening price

        def sort_key(oid: int) -> int:
            o = self.orders[oid]
            up = is_up_trigger(o)
            return 0 if up == bullish else 1

        active_ids = sorted(
            (oid for oid, o in self.orders.items() if o.active), key=sort_key
        )

        for oid in active_ids:
            o = self.orders.get(oid)
            if o is None or not o.active:
                continue
            fill_price = self._fill_price_for(o, bar)
            if fill_price is not None:
                self._execute_fill(o, fill_price, bar.ts)

    def _fill_price_for(self, o: Order, bar: Bar) -> float | None:
        if o.type is OrderType.MARKET:
            return bar.open
        if o.type is OrderType.STOP:
            if o.side is Side.LONG and bar.high >= o.price:
                return max(o.price, bar.open)
            if o.side is Side.SHORT and bar.low <= o.price:
                return min(o.price, bar.open)
            return None
        if o.type is OrderType.LIMIT:
            if o.side is Side.LONG and bar.low <= o.price:
                return min(o.price, bar.open)
            if o.side is Side.SHORT and bar.high >= o.price:
                return max(o.price, bar.open)
            return None
        return None

    def _execute_fill(self, o: Order, price: float, ts: datetime) -> None:
        o.active = False
        signed = o.qty if o.side is Side.LONG else -o.qty
        prev = self.position_qty
        new_qty = prev + signed

        realized_here = 0.0
        if prev != 0 and (prev > 0) != (signed > 0):
            closing = min(abs(signed), abs(prev))
            direction = 1 if prev > 0 else -1
            realized_here = (price - self.position_avg) * direction * closing * self.point_value

        if new_qty == 0:
            self.position_avg = 0.0
        elif prev == 0 or (prev > 0) == (signed > 0):
            # Opening, or adding in the same direction → weighted avg.
            self.position_avg = (
                self.position_avg * abs(prev) + price * abs(signed)
            ) / abs(new_qty)
        elif (prev > 0) != (new_qty > 0):
            # Flipped sides → reopen at fill price.
            self.position_avg = price
        # else: partial reduction in same direction → leave avg unchanged.

        commission_here = self.commission * o.qty
        net = realized_here - commission_here
        session_date = ts.astimezone(ET).date()
        self.daily_realized[session_date] += net
        self.total_realized += net
        self.position_qty = new_qty

        # OCO: cancel siblings in the same group
        if o.oco_group is not None:
            for oid2, o2 in self.orders.items():
                if oid2 != o.id and o2.active and o2.oco_group == o.oco_group:
                    o2.active = False

        fill = Fill(order_id=o.id, ts=ts, side=o.side, qty=o.qty, price=price)
        self.fills.append(fill)
        if self._on_fill is not None:
            self._on_fill(fill)
