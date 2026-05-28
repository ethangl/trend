from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class Side(Enum):
    LONG = 1
    SHORT = -1


class OrderType(Enum):
    MARKET = "MKT"
    STOP = "STP"
    LIMIT = "LMT"


@dataclass(frozen=True)
class Bar:
    ts: datetime  # tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass
class Order:
    id: int
    side: Side
    qty: int
    type: OrderType
    price: float
    oco_group: int | None = None
    active: bool = True


@dataclass(frozen=True)
class Fill:
    order_id: int
    ts: datetime
    side: Side
    qty: int
    price: float


@dataclass(frozen=True)
class Position:
    qty: int = 0  # signed; +n long, -n short
    avg_price: float = 0.0


@dataclass
class TradeRecord:
    """One completed round-trip trade. Strategies record these as a ledger."""
    session_date: date
    side: Side
    qty: int
    entry_price: float
    exit_price: float
    pnl_usd: float
