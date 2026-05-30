"""Clenow Counter Trend — long-only pullback in confirmed bull regime.

Spec (per Clenow, *Trading Evolved* ch. 17):
  - Bull filter: EMA(40) > EMA(80).
  - Pullback metric: (close - max(close, prior 20 days)) / std_40.
  - Entry: when pullback ≤ -3 standard deviations (i.e., 3+ std below the
    recent 20-day peak) AND we're in a bull regime, enter LONG on the next bar.
  - Exit: 20 trading days held OR EMA40 < EMA80, whichever first.
  - Sizing: volatility parity.
  - Long-only.
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
    ENTRY_SENT = "entry_sent"
    LONG = "long"
    EXIT_SENT = "exit_sent"


@dataclass
class CounterTrendConfig:
    fast_ema: int = 40
    slow_ema: int = 80
    lookback_window: int = 20
    std_window: int = 40
    pullback_threshold: float = -3.0
    hold_days: int = 20
    risk_factor: float = 0.001
    portfolio_value_usd: float = 300_000.0
    point_value: float = 5.0
    max_contracts: int = 100
    risk_multiplier: float = 1.0  # portfolio-level overlay (IDM × vol-target); 1.0 = off


class CounterTrendStrategy:
    def __init__(self, broker: Broker, cfg: CounterTrendConfig | None = None):
        self.broker = broker
        self.cfg = cfg or CounterTrendConfig()
        keep = max(self.cfg.slow_ema * 3, self.cfg.lookback_window + 5, 200)
        self.closes: deque[float] = deque(maxlen=keep)

        self.ema_fast: float | None = None
        self.ema_slow: float | None = None
        self._alpha_fast = 2.0 / (self.cfg.fast_ema + 1)
        self._alpha_slow = 2.0 / (self.cfg.slow_ema + 1)

        self.state: State = State.FLAT
        self.entry_price: float | None = None
        self.entry_qty: int = 0
        self.days_held: int = 0
        self.pending_order_id: int | None = None
        self.last_session_date: date | None = None
        self.trades: list[TradeRecord] = []
        broker.set_on_fill(self._on_fill)

    def to_state_dict(self) -> dict:
        """Path-dependent state only. Derivable fields (closes, EMAs, std,
        last_session_date) are recomputed by replay+catch-up, not persisted."""
        return {
            "state": self.state.name,
            "entry_price": self.entry_price,
            "entry_qty": self.entry_qty,
            "days_held": self.days_held,
            "pending_order_id": self.pending_order_id,
            "trades": [t.to_dict() for t in self.trades],
        }

    def apply_state_dict(self, d: dict) -> None:
        self.state = State[d["state"]]
        self.entry_price = d["entry_price"]
        self.entry_qty = d["entry_qty"]
        self.days_held = d["days_held"]
        self.pending_order_id = d["pending_order_id"]
        self.trades = [TradeRecord.from_dict(t) for t in d["trades"]]

    def rebase_prices(self, basis: float) -> None:
        """Shift every price-level field by `basis` (new − old contract price)
        so the strategy stays continuous across a futures roll. Diff-based stats
        (std) are shift-invariant; trade records are left untouched."""
        if not basis:
            return
        if self.closes:
            self.closes = deque(
                (c + basis for c in self.closes), maxlen=self.closes.maxlen
            )
        if self.ema_fast is not None:
            self.ema_fast += basis
        if self.ema_slow is not None:
            self.ema_slow += basis
        if self.entry_price is not None:
            self.entry_price += basis

    def on_bar(self, bar: Bar) -> None:
        self.last_session_date = bar.ts.date()
        self.closes.append(bar.close)
        self._update_emas(bar.close)

        cfg = self.cfg
        if self.state is State.LONG:
            self.days_held += 1

        required = max(cfg.slow_ema, cfg.std_window, cfg.lookback_window) + 1
        if len(self.closes) < required:
            return

        std = self._std_of_changes()
        if std is None or std <= 0:
            return

        trend_up = (self.ema_fast or 0) > (self.ema_slow or 0)

        if self.state is State.LONG:
            if self.days_held >= cfg.hold_days or not trend_up:
                self._send_exit()
            return

        if self.state is not State.FLAT:
            return

        if not trend_up:
            return

        closes_list = list(self.closes)
        prior_max = max(closes_list[-(cfg.lookback_window + 1):-1])
        pullback = (bar.close - prior_max) / std
        if pullback <= cfg.pullback_threshold:
            self._send_entry(std)

    def _update_emas(self, close: float) -> None:
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
            return
        self.ema_fast = self._alpha_fast * close + (1 - self._alpha_fast) * self.ema_fast
        self.ema_slow = self._alpha_slow * close + (1 - self._alpha_slow) * (self.ema_slow or close)

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
        target = (self.cfg.portfolio_value_usd * self.cfg.risk_factor
                  * self.cfg.risk_multiplier)
        denom = std * self.cfg.point_value
        if denom <= 0:
            return 0
        return max(0, min(self.cfg.max_contracts, int(target / denom)))

    def _send_entry(self, std: float) -> None:
        qty = self._size(std)
        if qty <= 0:
            return
        self.pending_order_id = self.broker.place_order(
            Side.LONG, qty, OrderType.MARKET, 0.0
        )
        self.state = State.ENTRY_SENT

    def _send_exit(self) -> None:
        pos = self.broker.position()
        if pos.qty <= 0:
            self.state = State.FLAT
            return
        self.pending_order_id = self.broker.place_order(
            Side.SHORT, pos.qty, OrderType.MARKET, 0.0
        )
        self.state = State.EXIT_SENT

    def _on_fill(self, fill: Fill) -> None:
        if self.state is State.ENTRY_SENT and fill.order_id == self.pending_order_id:
            self.entry_price = fill.price
            self.entry_qty = fill.qty
            self.days_held = 0
            self.state = State.LONG
            self.pending_order_id = None
            return

        if self.state is State.EXIT_SENT and fill.order_id == self.pending_order_id:
            assert self.entry_price is not None and self.last_session_date is not None
            pnl_pts = fill.price - self.entry_price
            pnl_usd = pnl_pts * self.cfg.point_value * self.entry_qty
            self.trades.append(TradeRecord(
                session_date=self.last_session_date,
                side=Side.LONG,
                qty=self.entry_qty,
                entry_price=self.entry_price,
                exit_price=fill.price,
                pnl_usd=pnl_usd,
            ))
            self.entry_price = None
            self.entry_qty = 0
            self.days_held = 0
            self.pending_order_id = None
            self.state = State.FLAT
