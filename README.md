# trend

Diversified, multi-strategy futures trading on IBKR. Three end-of-day
strategies (Core Trend, Time Return, Counter Trend — Clenow's framework from
*Trading Evolved*) run in parallel across 13 micro futures markets spanning
6 sectors: equity indexes, commodities, currencies, rates, agriculture.

## Status

v0.2 — backtest is validated; live execution path is built but **not yet
smoke-tested against a real IB Gateway**. Paper trading begins once the IBKR
paper account is approved.

Validated metrics over 16y (2010–2026), 9y train / 7y test split:

|                | train (2010–2018) | test (2019–2026) |
|----------------|-------------------|------------------|
| Sharpe         | 0.88              | 0.97             |
| Annualized ret | +7.49%            | +6.58%           |
| Annualized vol | 8.53%             | 6.80%            |
| Max drawdown   | 11.5%             | 7.0%             |

## Layout

```
trend/
  trend/
    types.py            Bar, Order, Fill, Position, TradeRecord, enums
    broker.py           Broker Protocol
    sim_broker.py       backtest broker (bar-driven, OCO, modify-stop, force-close)
    ib_broker.py        live broker against ib_async (same Protocol)
    data.py             CSV bar loader, ATR helpers
    core_trend.py       Clenow Ch.15 — 50-day breakout + EMA filter + chandelier
    time_return.py      Clenow Ch.16 — monthly rebalance, 6m & 12m return rule
    counter_trend.py    Clenow Ch.17 — pullback in confirmed bull regime
    runner.py           orchestrator: cells, history replay, daily tick, report
    reconcile.py        cell vs IBKR position diff + recommendations
    roll.py             futures roll detection (warn-only)
  tests/                52 tests, pure Python
  scripts/
    fetch_databento.py        backtest data
    all_strategies_backtest.py 3×13 cell matrix backtest, full report
    clenow_oos.py             train/test split validation
    cell_diagnostic.py        rank cells, propose structural exclusions
    diagnose_m2k.py           example per-market apples-to-apples diagnostic
    run_daily.py              dry-run of the live runner
    ib_smoke_test.py          IBBroker integration smoke test (needs paper)
  data/                       daily OHLCV CSVs per market
  book/                       Clenow's "Trading Evolved" reference
```

## Quickstart

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev,data]'
.venv/bin/pytest                          # 52 tests
.venv/bin/python scripts/all_strategies_backtest.py   # full backtest report
.venv/bin/python scripts/clenow_oos.py                # OOS validation
.venv/bin/python scripts/run_daily.py                 # what should we hold today
```

To re-pull historical data (default backtest period 2010-06-07 → today):

```bash
export DATABENTO_API_KEY=db-...   # or put it in .env (see .env.example)
.venv/bin/python scripts/fetch_databento.py --symbol ES.n.0 --start 2010-06-07 \
    --schema ohlcv-1d --out data/es_1d.csv
# repeat for: NQ, RTY, YM, CL, GC, 6E, 6B, 6J, 6A, ZN, ZC, ZS
```

## How it works

Each (strategy × market) combination is a **cell**. Each cell has its own
broker and strategy instance. On startup the runner replays historical daily
bars through every cell to bring its internal state (EMAs, std, position,
deques) up to today. The daily tick then feeds each cell its new bar.

Position-sizing is volatility parity per Clenow:

```
contracts = floor(portfolio_value × 0.001 / (std × point_value))
```

Sizing on micro contract point-values (live trading uses IBKR micros where
available, full-size where not — see `MARKETS` in
`scripts/all_strategies_backtest.py`).

Four (strategy × market) cells are structurally excluded based on documented
failure modes — see `scripts/cell_diagnostic.py`.

## Path to live

1. ✅ Backtest + OOS validation
2. ✅ `IBBroker` against `ib_async`
3. ⬜ Smoke test against paper account (needs paper approval)
4. ⬜ IB historical-bar fetch (pull yesterday's bar each day)
5. ⬜ Daily scheduling (cron or python loop)
6. ⬜ Paper trade ~4–6 weeks
7. ⬜ Go live with conservative sizing

## Risk budget

0.10% daily impact per position. With up to 39 active cells, naive daily
portfolio vol ~4% (much less after diversification — realized ~2%). Max
historical drawdown ~11.5%. See `trend/runner.py` for sizing defaults.
