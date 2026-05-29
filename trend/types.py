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

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "ts": self.ts.isoformat(),
            "side": self.side.name,
            "qty": self.qty,
            "price": self.price,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Fill":
        return cls(
            order_id=d["order_id"],
            ts=datetime.fromisoformat(d["ts"]),
            side=Side[d["side"]],
            qty=d["qty"],
            price=d["price"],
        )


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

    def to_dict(self) -> dict:
        return {
            "session_date": self.session_date.isoformat(),
            "side": self.side.name,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl_usd": self.pnl_usd,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TradeRecord":
        return cls(
            session_date=date.fromisoformat(d["session_date"]),
            side=Side[d["side"]],
            qty=d["qty"],
            entry_price=d["entry_price"],
            exit_price=d["exit_price"],
            pnl_usd=d["pnl_usd"],
        )
