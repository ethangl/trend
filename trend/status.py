"""Atomic JSON status writer for live-loop introspection.

`run_live_loop` calls `write_status(...)` periodically. A small native
(SwiftUI) menubar app reads the resulting file to display positions,
reconcile state, and the next-tick countdown.

The write is atomic (temp file + rename) so the reader never sees a half-
written document.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def default_status_path() -> Path:
    return Path.home() / ".trend" / "status.json"


def write_status(path: str | os.PathLike, state: dict[str, Any]) -> None:
    """Atomically write `state` to JSON at `path`.

    Creates parent directories as needed. Uses os.replace so a concurrent
    reader either sees the previous complete file or the new complete file —
    never a partial write.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, default=str, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
