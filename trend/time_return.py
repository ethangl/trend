"""Clenow Time Return Trend Model — daily-resolution, monthly rebalance.

Spec (per Clenow, *Trading Evolved* ch. 16):
  - At the start of each new month, check today's close vs:
      6 months ago (~125 trading days) AND
      12 months ago (~250 trading days)
  - If today's close is higher than BOTH → target long.
  - If today's close is lower than BOTH → target short.
  - Otherwise → target flat.
  - No stops, no targets — hold the full month.
  - Sizing: volatility parity (40-day std of daily price changes).
  - Position size set at entry; not re-sized while a direction is held
    (per Clenow: "no regular rebalancing of position sizes is done").
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .broker import Broker
from .types import Bar, Fill, OrderType, Side, TradeRecord


class State(Enum):
    FLAT = "flat"
    PENDING = "pending"   # close or open in flight; waiting for fill(s)
    LONG = "long"
    SHORT = "short"


@dataclass
class TimeReturnConfig:
    short_window_days: int = 125     # ~6 months
    long_window_days: int = 250      # ~12 months
    std_window: int = 40
    risk_factor: float = 0.001
    portfolio_value_usd: float = 300_000.0
    point_value: float = 5.0
    max_contracts: int = 100


class TimeReturnStrategy:
    def __init__(self, broker: Broker, cfg: TimeReturnConfig | None = None):
        self.broker = broker
        self.cfg = cfg or TimeReturnConfig()
        # Need at least long_window+1 closes before we can evaluate.
        self.closes: deque[float] = deque(maxlen=self.cfg.long_window_days + 5)
        self.last_month: int | None = None

        self.state: State = State.FLAT
        self.entry_price: float | None = None
        self.entry_qty: int = 0  # signed
        self.last_session_date: date | None = None

        # When flipping (close + reopen), we emit two orders and need to track
        # which is which.
        self.pending_close_id: int | None = None
        self.pending_open_id: int | None = None
        self.pending_open_signed_qty: int = 0

        self.trades: list[TradeRecord] = []
        broker.set_on_fill(self._on_fill)

    # ---- persistence ----

    def to_state_dict(self) -> dict:
        """Path-dependent state only. Derivable fields (closes, last_month,
        last_session_date) are recomputed by replay+catch-up, not persisted."""
        return {
            "state": self.state.name,
            "entry_price": self.entry_price,
            "entry_qty": self.entry_qty,
            "pending_close_id": self.pending_close_id,
            "pending_open_id": self.pending_open_id,
            "pending_open_signed_qty": self.pending_open_signed_qty,
            "trades": [t.to_dict() for t in self.trades],
        }

    def apply_state_dict(self, d: dict) -> None:
        self.state = State[d["state"]]
        self.entry_price = d["entry_price"]
        self.entry_qty = d["entry_qty"]
        self.pending_close_id = d["pending_close_id"]
        self.pending_open_id = d["pending_open_id"]
        self.pending_open_signed_qty = d["pending_open_signed_qty"]
        self.trades = [TradeRecord.from_dict(t) for t in d["trades"]]

    # ---- bar handler ----

    def on_bar(self, bar: Bar) -> None:
        self.last_session_date = bar.ts.date()
        self.closes.append(bar.close)

        if len(self.closes) <= self.cfg.long_window_days:
            self.last_month = bar.ts.month
            return

        current_month = bar.ts.month
        if self.last_month is None:
            self.last_month = current_month
            return
        if current_month == self.last_month:
            return
        # New month → rebalance.
        self.last_month = current_month

        if self.state is State.PENDING:
            # Last month's rebalance hasn't fully filled yet — skip this one.
            return

        std = self._std_of_changes()
        if std is None or std <= 0:
            return

        closes_list = list(self.closes)
        today_close = closes_list[-1]
        six_back = closes_list[-self.cfg.short_window_days - 1]
        twelve_back = closes_list[-self.cfg.long_window_days - 1]

        if today_close > six_back and today_close > twelve_back:
            target_side: Side | None = Side.LONG
        elif today_close < six_back and today_close < twelve_back:
            target_side = Side.SHORT
        else:
            target_side = None

        self._rebalance_to(target_side, std)

    # ---- helpers ----

    def _std_of_changes(self) -> float | None:
        if len(self.closes) < self.cfg.std_window + 1:
            return None
        closes = list(self.closes)[-(self.cfg.std_window + 1):]
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        n = len(diffs)
        mean = sum(diffs) / n
        var = sum((d - mean) ** 2 for d in diffs) / n
        return var ** 0.5

    def _size(self, std: float) -> int:
        target = self.cfg.portfolio_value_usd * self.cfg.risk_factor
        denom = std * self.cfg.point_value
        if denom <= 0:
            return 0
        return max(0, min(self.cfg.max_contracts, int(target / denom)))

    def _rebalance_to(self, target_side: Side | None, std: float) -> None:
        current_qty = self.broker.position().qty
        current_side: Side | None = (
            Side.LONG if current_qty > 0 else (Side.SHORT if current_qty < 0 else None)
        )

        # Already where we want to be — same direction, hold qty (per spec).
        if target_side == current_side and target_side is not None:
            return
        if target_side is None and current_side is None:
            return

        # Need to close any existing position?
        if current_qty != 0:
            close_side = Side.SHORT if current_qty > 0 else Side.LONG
            self.pending_close_id = self.broker.place_order(
                close_side, abs(current_qty), OrderType.MARKET, 0.0
            )
            self.state = State.PENDING

        # Need to open a new direction?
        if target_side is not None:
            new_qty = self._size(std)
            if new_qty <= 0:
                # Sizing too small — skip the open (we'll just close if we had one).
                return
            open_side = target_side
            self.pending_open_id = self.broker.place_order(
                open_side, new_qty, OrderType.MARKET, 0.0
            )
            self.pending_open_signed_qty = (
                new_qty if target_side is Side.LONG else -new_qty
            )
            self.state = State.PENDING

    def _on_fill(self, fill: Fill) -> None:
        if fill.order_id == self.pending_close_id:
            # Record the closed trade
            assert self.entry_price is not None and self.last_session_date is not None
            direction = 1 if self.entry_qty > 0 else -1
            pnl_pts = (fill.price - self.entry_price) * direction
            pnl_usd = pnl_pts * self.cfg.point_value * abs(self.entry_qty)
            self.trades.append(TradeRecord(
                session_date=self.last_session_date,
                side=Side.LONG if self.entry_qty > 0 else Side.SHORT,
                qty=abs(self.entry_qty),
                entry_price=self.entry_price,
                exit_price=fill.price,
                pnl_usd=pnl_usd,
            ))
            self.entry_price = None
            self.entry_qty = 0
            self.pending_close_id = None
            # If no open order is pending, we're flat.
            if self.pending_open_id is None:
                self.state = State.FLAT
            return

        if fill.order_id == self.pending_open_id:
            self.entry_price = fill.price
            self.entry_qty = self.pending_open_signed_qty
            self.pending_open_id = None
            self.pending_open_signed_qty = 0
            self.state = State.LONG if self.entry_qty > 0 else State.SHORT
