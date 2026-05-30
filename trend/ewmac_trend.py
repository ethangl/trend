"""Continuous multi-speed EWMAC trend following (Carver, *Systematic Trading*).

A candidate replacement for Core Trend. Where Core Trend takes a binary
flat/long/short position gated by one EMA(40/80) filter + 50-day breakout, this
holds a *continuous* position whose size scales with trend conviction, blended
across several EWMAC speeds:

  - For each speed (fast_span, slow_span), the raw signal is EMA(fast) - EMA(slow),
    normalized by the instrument's price volatility (std of daily price changes)
    so it is comparable across markets and regimes:  raw = (ema_f - ema_s) / sigma.
  - Each raw is multiplied by a published Carver forecast scalar (so the average
    absolute forecast is ~10) and capped to +/- forecast_cap.
  - The capped forecasts are averaged, multiplied by a forecast-diversification
    multiplier (FDM), and re-capped -> the combined forecast in [-cap, +cap].
  - Target position = (forecast / 10) * vol-parity base, where the base is the
    same volatility-parity sizing the other cells use:
        base = portfolio * risk_factor * risk_multiplier / (sigma * point_value)
    So forecast 10 (the average) reproduces the siblings' position; a max
    forecast of +/-20 doubles it; a weak/neutral forecast holds a fraction.
  - To avoid churning on tiny forecast wiggles, the position is only rebalanced
    when it is at least `buffer_frac` of the base size away from target.

The forecast scalars are Carver's published per-speed constants (universal, not
per-instrument fit) so the strategy stays deterministic and replayable.

Same cell contract as the other strategies: on_bar emits MARKET orders to a
Broker, plus to_state_dict / apply_state_dict / rebase_prices / fill handling.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date

from .broker import Broker
from .types import Bar, Fill, OrderType, Side, TradeRecord


# Carver's published EWMAC forecast scalars by (fast, slow) span. These scale the
# vol-normalized raw signal so its average absolute value is ~10.
_DEFAULT_SPEEDS: tuple[tuple[int, int, float], ...] = (
    (8, 32, 5.30),
    (16, 64, 3.75),
    (32, 128, 2.65),
    (64, 256, 1.87),
)


@dataclass
class EwmacTrendConfig:
    # (fast_span, slow_span, forecast_scalar) per EWMAC speed.
    speeds: tuple[tuple[int, int, float], ...] = _DEFAULT_SPEEDS
    std_window: int = 40
    forecast_cap: float = 20.0
    fdm: float = 1.1                 # forecast-diversification multiplier for the blend
    buffer_frac: float = 0.10        # no-trade band as a fraction of the base size
    risk_factor: float = 0.001
    portfolio_value_usd: float = 300_000.0
    point_value: float = 5.0
    max_contracts: int = 100
    risk_multiplier: float = 1.0     # portfolio-level overlay (IDM × vol-target); 1.0 = off


class EwmacTrendStrategy:
    def __init__(self, broker: Broker, cfg: EwmacTrendConfig | None = None):
        self.broker = broker
        self.cfg = cfg or EwmacTrendConfig()

        self._slowest = max(slow for _, slow, _ in self.cfg.speeds)
        self._spans = sorted({s for pair in self.cfg.speeds for s in pair[:2]})
        self._alpha = {s: 2.0 / (s + 1) for s in self._spans}
        self.emas: dict[int, float | None] = {s: None for s in self._spans}

        keep = max(self._slowest * 3, self.cfg.std_window + 5, 300)
        self.closes: deque[float] = deque(maxlen=keep)

        # Ledger of the held position (signed), for trade records + roll rebasing.
        self.entry_price: float | None = None
        self.entry_qty: int = 0
        self.pending_order_id: int | None = None
        self.last_session_date: date | None = None
        self.trades: list[TradeRecord] = []
        broker.set_on_fill(self._on_fill)

    # ---- persistence ----

    def to_state_dict(self) -> dict:
        """Path-dependent state only. EMAs, closes, std and session cursors are
        rebuilt by replay+catch-up, so they are deliberately not persisted."""
        return {
            "entry_price": self.entry_price,
            "entry_qty": self.entry_qty,
            "pending_order_id": self.pending_order_id,
            "trades": [t.to_dict() for t in self.trades],
        }

    def apply_state_dict(self, d: dict) -> None:
        self.entry_price = d["entry_price"]
        self.entry_qty = d["entry_qty"]
        self.pending_order_id = d["pending_order_id"]
        self.trades = [TradeRecord.from_dict(t) for t in d["trades"]]

    # ---- roll ----

    def rebase_prices(self, basis: float) -> None:
        """Shift every price-level field by `basis` (new − old contract price) so
        the EWMACs and the held-position entry stay continuous across a futures
        roll. The std of daily changes is diff-based and shift-invariant."""
        if not basis:
            return
        if self.closes:
            self.closes = deque(
                (c + basis for c in self.closes), maxlen=self.closes.maxlen
            )
        for s in self._spans:
            if self.emas[s] is not None:
                self.emas[s] += basis
        if self.entry_price is not None:
            self.entry_price += basis

    # ---- main bar handler ----

    def on_bar(self, bar: Bar) -> None:
        self.last_session_date = bar.ts.date()
        self.closes.append(bar.close)
        self._update_emas(bar.close)

        required = max(self._slowest, self.cfg.std_window) + 1
        if len(self.closes) < required:
            return

        sigma = self._std_of_changes()
        if sigma is None or sigma <= 0:
            return

        # Don't stack orders: wait for the in-flight rebalance to fill.
        if self.pending_order_id is not None:
            return

        forecast = self._combined_forecast(sigma)
        base = (self.cfg.portfolio_value_usd * self.cfg.risk_factor
                * self.cfg.risk_multiplier) / (sigma * self.cfg.point_value)
        target = int(round((forecast / 10.0) * base))
        target = max(-self.cfg.max_contracts, min(self.cfg.max_contracts, target))

        current = self.broker.position().qty
        delta = target - current
        threshold = max(1, int(round(self.cfg.buffer_frac * base)))
        if abs(delta) < threshold:
            return

        side = Side.LONG if delta > 0 else Side.SHORT
        self.pending_order_id = self.broker.place_order(
            side, abs(delta), OrderType.MARKET, 0.0
        )

    # ---- helpers ----

    def _update_emas(self, close: float) -> None:
        for s in self._spans:
            if self.emas[s] is None:
                self.emas[s] = close
            else:
                a = self._alpha[s]
                self.emas[s] = a * close + (1 - a) * self.emas[s]

    def _std_of_changes(self) -> float | None:
        if len(self.closes) < self.cfg.std_window + 1:
            return None
        closes = list(self.closes)[-(self.cfg.std_window + 1):]
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        n = len(diffs)
        mean = sum(diffs) / n
        var = sum((d - mean) ** 2 for d in diffs) / n  # population std (ddof=0)
        return var ** 0.5

    def _combined_forecast(self, sigma: float) -> float:
        cap = self.cfg.forecast_cap
        scaled = []
        for fast, slow, scalar in self.cfg.speeds:
            raw = (self.emas[fast] - self.emas[slow]) / sigma
            scaled.append(max(-cap, min(cap, raw * scalar)))
        combined = (sum(scaled) / len(scaled)) * self.cfg.fdm
        return max(-cap, min(cap, combined))

    def _on_fill(self, fill: Fill) -> None:
        if fill.order_id != self.pending_order_id:
            return
        signed = fill.qty if fill.side is Side.LONG else -fill.qty
        prev = self.entry_qty
        new = prev + signed

        # Reducing or flipping realizes P&L on the closed quantity.
        if prev != 0 and (prev > 0) != (signed > 0):
            closing = min(abs(signed), abs(prev))
            direction = 1 if prev > 0 else -1
            pnl = ((fill.price - (self.entry_price or fill.price))
                   * direction * closing * self.cfg.point_value)
            self.trades.append(TradeRecord(
                session_date=self.last_session_date or fill.ts.date(),
                side=Side.LONG if prev > 0 else Side.SHORT,
                qty=closing,
                entry_price=self.entry_price or fill.price,
                exit_price=fill.price,
                pnl_usd=pnl,
            ))

        # Update the running average entry (mirrors the broker's position math).
        if new == 0:
            self.entry_price = None
        elif prev == 0 or (prev > 0) == (signed > 0):
            base_px = self.entry_price if self.entry_price is not None else fill.price
            self.entry_price = (base_px * abs(prev) + fill.price * abs(signed)) / abs(new)
        elif (prev > 0) != (new > 0):
            self.entry_price = fill.price  # flipped sides — reopen at fill price

        self.entry_qty = new
        self.pending_order_id = None
