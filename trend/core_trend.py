"""Clenow Core Trend Following — daily-resolution single-instrument strategy.

Spec (per Clenow, *Trading Evolved* ch. 15):
  - Trend filter: EMA(40) of close vs EMA(80) of close. Long-only allowed when
    fast > slow; short-only allowed when fast < slow.
  - Entry: when today's close is a new 50-day extreme (max of prior 50 closes
    for longs, min for shorts) in the direction of the trend filter, enter on
    the NEXT bar's open (a MARKET order).
  - Exit: trailing stop at `trail_mult` × 40-day std of daily price changes,
    measured from the favorable extreme of close since entry. Exit also if the
    trend filter flips. Both checked end-of-bar; market order placed for the
    next bar's open.
  - Sizing: volatility parity. `contracts = (portfolio_value × risk_factor)
    / (std_40 × point_value)`. Default risk_factor is 0.001 (0.10% daily
    impact per position).

State is kept entirely inside the strategy; the SimBroker just executes the
market orders we emit.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .broker import Broker
from .types import Bar, Fill, OrderType, Side, TradeRecord


class State(Enum):
    FLAT = "flat"
    ENTRY_SENT = "entry_sent"
    LONG = "long"
    SHORT = "short"
    EXIT_SENT = "exit_sent"


@dataclass
class CoreTrendConfig:
    fast_ema: int = 40
    slow_ema: int = 80
    breakout_window: int = 50
    std_window: int = 40
    trail_mult: float = 3.0
    risk_factor: float = 0.001
    portfolio_value_usd: float = 300_000.0
    point_value: float = 5.0
    max_contracts: int = 100  # safety cap so quiet markets can't blow up qty


class CoreTrendStrategy:
    def __init__(self, broker: Broker, cfg: CoreTrendConfig | None = None):
        self.broker = broker
        self.cfg = cfg or CoreTrendConfig()

        # Rolling state — keep enough history for the longest window.
        keep = max(cfg.slow_ema * 3 if cfg else 240,
                   (cfg or CoreTrendConfig()).breakout_window + 5)
        self.closes: deque[float] = deque(maxlen=keep)

        self.ema_fast: float | None = None
        self.ema_slow: float | None = None
        self._alpha_fast = 2.0 / ((cfg or CoreTrendConfig()).fast_ema + 1)
        self._alpha_slow = 2.0 / ((cfg or CoreTrendConfig()).slow_ema + 1)

        # Position lifecycle
        self.state: State = State.FLAT
        self.entry_price: float | None = None
        self.entry_qty: int = 0  # signed: +n long, -n short
        self.fav_extreme_close: float | None = None  # highest close since entry (long) / lowest (short)
        self.pending_entry_side: Side | None = None
        self.pending_order_id: int | None = None
        self.trades: list[TradeRecord] = []
        self.last_session_date: date | None = None

        broker.set_on_fill(self._on_fill)

    # ---- persistence ----

    def to_state_dict(self) -> dict:
        """Path-dependent state only. Derivable fields (closes, EMAs, std,
        last_session_date) are recomputed by replay+catch-up, not persisted."""
        return {
            "state": self.state.name,
            "entry_price": self.entry_price,
            "entry_qty": self.entry_qty,
            "fav_extreme_close": self.fav_extreme_close,
            "pending_entry_side": self.pending_entry_side.name
                                  if self.pending_entry_side else None,
            "pending_order_id": self.pending_order_id,
            "trades": [t.to_dict() for t in self.trades],
        }

    def apply_state_dict(self, d: dict) -> None:
        self.state = State[d["state"]]
        self.entry_price = d["entry_price"]
        self.entry_qty = d["entry_qty"]
        self.fav_extreme_close = d["fav_extreme_close"]
        self.pending_entry_side = (Side[d["pending_entry_side"]]
                                   if d["pending_entry_side"] else None)
        self.pending_order_id = d["pending_order_id"]
        self.trades = [TradeRecord.from_dict(t) for t in d["trades"]]

    # ---- main bar handler ----

    def on_bar(self, bar: Bar) -> None:
        self.last_session_date = bar.ts.date()
        self.closes.append(bar.close)
        self._update_emas(bar.close)

        cfg = self.cfg
        required_history = max(cfg.slow_ema, cfg.breakout_window, cfg.std_window) + 1
        if len(self.closes) < required_history:
            return

        std = self._std_of_changes()
        if std is None or std <= 0:
            return

        trend_up = (self.ema_fast or 0) > (self.ema_slow or 0)

        # Update favorable extreme & manage exits while in a position.
        if self.state is State.LONG:
            assert self.fav_extreme_close is not None
            self.fav_extreme_close = max(self.fav_extreme_close, bar.close)
            drawdown = self.fav_extreme_close - bar.close
            if drawdown >= cfg.trail_mult * std or not trend_up:
                self._send_exit()
            return

        if self.state is State.SHORT:
            assert self.fav_extreme_close is not None
            self.fav_extreme_close = min(self.fav_extreme_close, bar.close)
            rally = bar.close - self.fav_extreme_close
            if rally >= cfg.trail_mult * std or trend_up:
                self._send_exit()
            return

        if self.state is not State.FLAT:
            # In ENTRY_SENT or EXIT_SENT — waiting for next-bar fill. Don't act.
            return

        # FLAT: look for entry signal.
        closes_list = list(self.closes)
        # Compare today's close to the highest/lowest of the prior breakout_window closes.
        prior = closes_list[-(cfg.breakout_window + 1):-1]
        if not prior:
            return
        prior_max = max(prior)
        prior_min = min(prior)

        if trend_up and bar.close > prior_max:
            self._send_entry(Side.LONG, std)
        elif (not trend_up) and bar.close < prior_min:
            self._send_entry(Side.SHORT, std)

    # ---- helpers ----

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
        var = sum((d - mean) ** 2 for d in diffs) / n  # population std (ddof=0)
        return var ** 0.5

    def _size(self, std: float) -> int:
        target = self.cfg.portfolio_value_usd * self.cfg.risk_factor
        denom = std * self.cfg.point_value
        if denom <= 0:
            return 0
        qty = int(target / denom)
        return max(0, min(self.cfg.max_contracts, qty))

    def _send_entry(self, side: Side, std: float) -> None:
        qty = self._size(std)
        if qty <= 0:
            return
        self.pending_entry_side = side
        self.pending_order_id = self.broker.place_order(
            side, qty, OrderType.MARKET, 0.0
        )
        self.state = State.ENTRY_SENT

    def _send_exit(self) -> None:
        pos = self.broker.position()
        if pos.qty == 0:
            self.state = State.FLAT
            return
        side = Side.SHORT if pos.qty > 0 else Side.LONG
        self.pending_order_id = self.broker.place_order(
            side, abs(pos.qty), OrderType.MARKET, 0.0
        )
        self.state = State.EXIT_SENT

    def _on_fill(self, fill: Fill) -> None:
        if self.state is State.ENTRY_SENT and fill.order_id == self.pending_order_id:
            assert self.pending_entry_side is not None
            self.entry_price = fill.price
            signed = fill.qty if self.pending_entry_side is Side.LONG else -fill.qty
            self.entry_qty = signed
            self.fav_extreme_close = fill.price
            self.state = State.LONG if signed > 0 else State.SHORT
            self.pending_order_id = None
            return

        if self.state is State.EXIT_SENT and fill.order_id == self.pending_order_id:
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
            self.fav_extreme_close = None
            self.pending_order_id = None
            self.state = State.FLAT
