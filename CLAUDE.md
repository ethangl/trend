# CLAUDE.md — operating notes for agents in this repo

See `README.md` for architecture. This file is the **operational runbook** + how to
behave. Read it before touching the live loop.

## Behavioral directive (read this first)

- **This trades a PAPER IBKR account** (account ids start `DU`/`DF`; `connect_ib`
  refuses non-paper). Flatten, restart, killing the loop, editing `~/.trend/*`
  while the loop is stopped — all **reversible and low-stakes**. Act decisively:
  verify state by reading the files, then do the obvious thing. Do **not**
  over-confirm, re-ask, or re-derive what's written here. The user is an expert;
  match that pace.
- When something's wrong, **gather state first** (read `status.json` + tail the
  log), state the diagnosis in one or two lines, then act or give the exact
  command. Don't narrate long branching analyses.
- Reserve caution for genuinely irreversible things (committing, pushing,
  anything touching a real-money account — which this is not).

## Live loop supervision

- Supervised by the **SwiftUI menubar app** in `macos-app/` (`ProcessSupervisor.swift`),
  which launches `scripts/run_live_loop.py`. Buttons: **Start**, **Start (flatten)**
  [adds `--flatten-account`], **Stop**, **Restart**.
- `--flatten-account` is **not a default** — only the "Start (flatten)" button adds
  it, and it flattens the whole account on startup. Use plain **Start/Restart** for
  steady operation; use flatten only for a deliberate clean slate (pair with a
  cleared `state.json`, and do it when CME is open so the close fills).
- Ticks fire weekdays **18:15 ET**. CME is closed weekends (Fri 17:00 → Sun 18:00 ET);
  orders placed while closed rest until reopen.

## Control & state files (`~/.trend/`)

- `status.json` — health heartbeat (read-only). Fields: `status`, `ib_connected`,
  `expected_positions` vs `ibkr_positions`, `paused`, `risk_multiplier`, `last_tick`.
  **First thing to check.**
- `command.json` — control the running loop by writing `{"command":"pause|resume|
  flatten|restart"}` atomically (temp + rename). The heartbeat consumes it within
  ~2s **only when the loop is in its stable tick-loop** (not mid-restart).
- `state.json` — persisted positions + strategy lifecycle + risk-overlay controller.
  The loop **re-saves it on exit (SIGTERM)**, so only edit it while the loop is
  **stopped**, or the edit is overwritten. Cold start = no file present.
- Logs: `logs/loop-supervised.log` (full), `logs/daily.jsonl` (per-tick records).

## Reconcile / HALT

- Each tick reconciles cell positions against IBKR. Mismatch beyond
  `--halt-threshold` (default 0) → **HALT**. The loop **skips the tick under HALT**
  (no rolls, no orders) and keeps skipping until resolved — safe, but stuck.
- "New cell" (market added that isn't in saved state) is **force-flat** on resume
  (it can't hold a real position yet). This is why adding a market to a running
  system no longer injects a phantom position (the MET −24 incident).

## Risk overlay (vol-target)

- `trend/risk_overlay.py` (`RiskOverlayController`) + `Runner.set_risk_multiplier`.
  Wired into the loop: `--vol-target` (default 0.10), warms up ~40 ticks at
  multiplier 1.0 then scales sizing toward the target vol. Controller state
  persists in `state.json`; current multiplier shows in `status.json`.
- True risk is ~13% vol / ~19% DD mark-to-market (≈2× the README's lumpy-P&L
  figures). See memory `project_true_mtm_risk`.

## Dev

- Tests: `.venv/bin/pytest` (pure Python, no IB). Keep them green before/after changes.
- Backtests: `scripts/all_strategies_backtest.py`, `clenow_oos.py`,
  `risk_overlay_backtest.py`, `ewmac_vs_coretrend.py`, `equity_trend_blend.py`.
- Strategy decisions already made (don't re-litigate without new evidence):
  EWMAC rejected as a Core Trend replacement; structural long-equity blend rejected
  (this account is a diversifier vs an equity-heavy net worth). See memory files
  `project_ewmac_evaluation`, `user_ibkr_trader`.
