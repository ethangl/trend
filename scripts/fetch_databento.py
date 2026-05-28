#!/usr/bin/env python3
"""Fetch 1-min OHLCV from Databento for the backtester.

Examples
--------
    # Default: 5y of MES front-month (open-interest roll), 2020 → today
    DATABENTO_API_KEY=db-... python scripts/fetch_databento.py

    # Specific symbol/dates and output path
    DATABENTO_API_KEY=db-... python scripts/fetch_databento.py \\
        --symbol ES.n.0 --start 2015-01-01 --end 2026-01-01 \\
        --out data/es_1min.csv

Notes
-----
* Continuous symbol formats: `MES.n.0` (open-interest roll, recommended),
  `MES.c.0` (calendar roll), `MES.v.0` (volume roll).
* MES history starts May 6 2019; for longer history use ES (same price series).
* Dataset `GLBX.MDP3` is CME Globex.
* The script previews cost via `metadata.get_cost` and asks before downloading
  unless `--yes` is passed.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Tiny .env loader: KEY=value per line, # comments, optional quotes.
    Does not overwrite existing env vars."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--symbol", default="MES.n.0",
                   help="Continuous symbol (default: MES.n.0)")
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--dataset", default="GLBX.MDP3")
    p.add_argument("--schema", default="ohlcv-1m")
    p.add_argument("--out", default="data/mes_1min.csv")
    p.add_argument("--yes", action="store_true",
                   help="Skip cost confirmation prompt")
    p.add_argument("--env", default=".env",
                   help="Path to .env file (default: .env in cwd)")
    args = p.parse_args()

    load_dotenv(Path(args.env))

    try:
        import databento as db
    except ImportError:
        print("Missing dependency. Install with:\n"
              "    pip install -e '.[data]'", file=sys.stderr)
        return 1

    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        print("DATABENTO_API_KEY not set. Add it to .env (see .env.example) "
              "or export it in your shell.", file=sys.stderr)
        return 1

    client = db.Historical(key=key)

    common = dict(
        dataset=args.dataset,
        schema=args.schema,
        symbols=[args.symbol],
        stype_in="continuous",
        start=args.start,
        end=args.end,
    )

    try:
        cost = client.metadata.get_cost(**common)
        print(f"Estimated cost: ${cost:.4f}  "
              f"({args.symbol}, {args.schema}, {args.start} → {args.end})")
    except Exception as e:
        print(f"(Cost preview failed: {e}. Proceeding to download estimate.)",
              file=sys.stderr)

    if not args.yes:
        ans = input("Download? [y/N] ").strip().lower()
        if ans not in {"y", "yes"}:
            print("Aborted.")
            return 0

    print("Fetching…")
    data = client.timeseries.get_range(**common)
    df = data.to_df()

    # Databento returns a DataFrame indexed by ts_event (UTC).
    df = df.reset_index()
    ts_col = "ts_event" if "ts_event" in df.columns else df.columns[0]
    keep = [ts_col, "open", "high", "low", "close", "volume"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        print(f"Unexpected columns from Databento: missing {missing}. "
              f"Got: {list(df.columns)}", file=sys.stderr)
        return 1
    out = df[keep].rename(columns={ts_col: "timestamp"})

    # Coerce timestamps to ISO-8601 UTC strings for the CSV loader.
    out["timestamp"] = out["timestamp"].dt.tz_convert("UTC").dt.strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"Wrote {len(out):,} bars to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
