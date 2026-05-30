"""Cross-restart state persistence for the live loop.

Every cold start replays CSV history (warming derivable state: EMAs, std,
rolling deques) and would otherwise force-flat, losing the real position
tracking. Persistence makes restarts idempotent: load the saved path-dependent
state (broker bookkeeping + strategy lifecycle), skip the force-flat, then
reconcile against IB.

Only path-dependent state is persisted; anything reconstructable by replay
(EMAs, deques, last_session_date, monthly cursors) is deliberately left out.
See Runner.snapshot_cells / Runner.apply_persisted_state.

The write is atomic (temp + os.replace), same pattern as trend/status.py, so a
crash mid-write never leaves a half-read file.

Recovery policy (deliberately fail-loud rather than fail-flat):
  - missing file          -> load_state returns None  (genuine cold start)
  - corrupt JSON          -> raises StatePersistenceError (operator investigates)
  - schema_version bump   -> raises StatePersistenceError (no silent migration)
A silent fall-back to force-flat could close real positions and forfeit P&L,
so we refuse instead.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCHEMA_VERSION = 1


class StatePersistenceError(Exception):
    """Saved state exists but can't be trusted (corrupt or wrong schema)."""


def default_state_path() -> Path:
    return Path.home() / ".trend" / "state.json"


def save_state(path: str | os.PathLike, runner, args) -> None:
    """Atomically write the runner's path-dependent state to `path`."""
    global_state = {
        "paused": bool(getattr(args, "_paused", False)),
        "skip": sorted(getattr(args, "_skip", set())),
    }
    overlay = getattr(args, "_overlay", None)
    if overlay is not None:
        # Persist the vol-target controller's trailing-vol estimate so it
        # resumes instead of cold-starting at multiplier 1.0 on every restart.
        global_state["risk_overlay"] = overlay.to_dict()

    state = {
        "schema_version": SCHEMA_VERSION,
        "saved_at": datetime.now(ET).isoformat(),
        "global": global_state,
        "cells": runner.snapshot_cells(),
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def load_state(path: str | os.PathLike) -> dict[str, Any] | None:
    """Return the saved state dict, or None if no file exists.

    Raises StatePersistenceError if the file exists but is corrupt or carries a
    different schema_version — the caller should refuse to start rather than
    silently force-flat.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text()
    except OSError as e:
        raise StatePersistenceError(f"could not read {p}: {e}") from e
    try:
        state = json.loads(text)
    except json.JSONDecodeError as e:
        raise StatePersistenceError(f"corrupt state file {p}: {e}") from e
    version = state.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StatePersistenceError(
            f"state schema mismatch in {p}: file is v{version}, "
            f"code expects v{SCHEMA_VERSION}"
        )
    return state
