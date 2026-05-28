"""IBKR live execution adapter that implements the `Broker` Protocol.

One IBBroker per (strategy × market) cell. All cells share a single connected
`IB` instance (open one connection at the runner level and pass it in). Each
broker tracks its OWN logical position for the cell; IBKR aggregates the real
account position across all cells automatically.

This is intentionally a thin adapter — no smart order routing, no
reconciliation logic. The runner is responsible for:
  - Holding the IB connection
  - Resolving front-month contracts (with rolls)
  - Creating one IBBroker per cell with the right contract
  - Driving strategies on a daily schedule
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .broker import Broker  # noqa: F401  (Protocol reference)
from .types import Fill, OrderType, Position, Side

ET = ZoneInfo("America/New_York")


class IBBroker:
    """Live-execution broker for a single futures contract.

    Order IDs returned by `place_order` are LOCAL to this broker — they are
    our own monotonic ints, separate from IBKR's order IDs. We track the
    mapping internally so callers don't need to care.
    """

    def __init__(self, ib: Any, contract: Any, point_value: float):
        """
        Args:
            ib: a connected `ib_async.IB` instance (caller manages connect/disconnect)
            contract: a fully-qualified `ib_async.Future` for the front-month
                contract this broker trades
            point_value: dollars per 1.0 price change for one contract — used
                only for local P&L bookkeeping
        """
        self.ib = ib
        self.contract = contract
        self.point_value = point_value

        # Local state mirrors SimBroker so the Protocol surface matches.
        self._next_id = 1
        self._trades: dict[int, Any] = {}        # our oid → ib_async Trade
        self.position_qty: int = 0
        self.position_avg: float = 0.0
        self.fills: list[Fill] = []
        self.total_realized: float = 0.0
        self.daily_realized: dict = {}

        self._on_fill: Callable[[Fill], None] | None = None

    # ---- Broker Protocol ----

    def place_order(
        self,
        side: Side,
        qty: int,
        otype: OrderType,
        price: float,
        oco_group: int | None = None,
    ) -> int:
        from ib_async import LimitOrder, MarketOrder, StopOrder

        action = "BUY" if side is Side.LONG else "SELL"
        if otype is OrderType.MARKET:
            order = MarketOrder(action, qty)
        elif otype is OrderType.STOP:
            order = StopOrder(action, qty, price)
        elif otype is OrderType.LIMIT:
            order = LimitOrder(action, qty, price)
        else:
            raise ValueError(f"Unsupported order type: {otype}")

        # Set TIF explicitly. Without it IBKR applies an account "order preset"
        # and emits Error 10349 ("Order TIF was set to DAY based on order
        # preset"), which transiently flips the order to Cancelled before
        # resubmitting. MARKET orders fill same-session → DAY; resting
        # STOP/LIMIT persist across sessions → GTC.
        order.tif = "DAY" if otype is OrderType.MARKET else "GTC"

        oid = self._next_id
        self._next_id += 1
        order.orderRef = f"trend-{oid}"

        # OCO via IB's ocaGroup field. Same group string → IB cancels siblings
        # when one fills. We just stringify the group ID we were passed.
        if oco_group is not None:
            order.ocaGroup = f"trend-oco-{oco_group}"
            order.ocaType = 1  # 1 = cancel with block

        trade = self.ib.placeOrder(self.contract, order)
        self._trades[oid] = trade
        # Wire up fill events for THIS trade only; the lambda closes over oid.
        trade.fillEvent += lambda t, f, _oid=oid: self._on_ib_fill(_oid, f)
        return oid

    def cancel(self, order_id: int) -> None:
        trade = self._trades.get(order_id)
        if trade is None:
            return
        if not trade.isDone():
            self.ib.cancelOrder(trade.order)

    def modify_stop(self, order_id: int, new_price: float) -> None:
        """Cancel-and-replace the stop. (Simpler than amending in-place;
        IBKR's amend can race with fills.)
        """
        trade = self._trades.get(order_id)
        if trade is None or trade.isDone():
            return
        old_order = trade.order
        from ib_async import StopOrder

        new_order = StopOrder(old_order.action, old_order.totalQuantity, new_price)
        new_order.orderRef = old_order.orderRef
        new_order.ocaGroup = old_order.ocaGroup
        new_order.ocaType = old_order.ocaType
        # Cancel old, place new
        self.ib.cancelOrder(old_order)
        new_trade = self.ib.placeOrder(self.contract, new_order)
        self._trades[order_id] = new_trade
        new_trade.fillEvent += lambda t, f, _oid=order_id: self._on_ib_fill(_oid, f)

    def position(self) -> Position:
        return Position(qty=self.position_qty, avg_price=self.position_avg)

    def set_on_fill(self, cb: Callable[[Fill], None]) -> None:
        self._on_fill = cb

    def force_close(self, price: float, ts: datetime) -> None:
        """Synthetic close only makes sense in backtest. In live trading we
        always emit real orders."""
        raise NotImplementedError(
            "force_close is a backtest-only mechanism. Use place_order with "
            "OrderType.MARKET to flatten a live position."
        )

    # ---- IB event handler ----

    def _on_ib_fill(self, our_oid: int, ib_fill: Any) -> None:
        """ib_async fillEvent → our Fill, with position bookkeeping."""
        exec_ = ib_fill.execution
        ts = exec_.time
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        price = float(exec_.price)
        qty = int(exec_.shares)
        side = Side.LONG if exec_.side == "BOT" else Side.SHORT

        signed = qty if side is Side.LONG else -qty
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
            self.position_avg = (
                self.position_avg * abs(prev) + price * abs(signed)
            ) / abs(new_qty)
        elif (prev > 0) != (new_qty > 0):
            self.position_avg = price

        self.total_realized += realized_here
        session_date = ts.astimezone(ET).date()
        self.daily_realized[session_date] = (
            self.daily_realized.get(session_date, 0.0) + realized_here
        )
        self.position_qty = new_qty

        fill = Fill(order_id=our_oid, ts=ts, side=side, qty=qty, price=price)
        self.fills.append(fill)
        if self._on_fill is not None:
            self._on_fill(fill)
