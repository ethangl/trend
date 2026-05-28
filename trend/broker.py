from datetime import datetime
from typing import Callable, Protocol

from .types import Fill, OrderType, Position, Side


class Broker(Protocol):
    def place_order(
        self,
        side: Side,
        qty: int,
        otype: OrderType,
        price: float,
        oco_group: int | None = None,
    ) -> int: ...

    def cancel(self, order_id: int) -> None: ...

    def modify_stop(self, order_id: int, new_price: float) -> None: ...

    def position(self) -> Position: ...

    def set_on_fill(self, cb: Callable[[Fill], None]) -> None: ...

    def force_close(self, price: float, ts: datetime) -> None:
        """Synthetic close used by the backtest harness for session boundary
        cleanup. Live brokers should raise NotImplementedError."""
        ...
