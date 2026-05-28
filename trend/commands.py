"""Command file IPC between the SwiftUI menubar app and the Python loop.

The menubar app writes a single JSON file (`~/.trend/command.json`) per
command — an atomic temp-file rename so the loop reader never sees a partial
write. The loop polls during its idle wait, reads-and-deletes the file, and
acts on the command.

Fire-and-forget: results show up in the next `status.json` heartbeat (paused
flag, positions, etc.). If you need explicit ack-style responses in the
future, add a sibling `command_response.json` with the same id.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_command_path() -> Path:
    return Path.home() / ".trend" / "command.json"


def read_and_delete_command(path: str | os.PathLike) -> dict[str, Any] | None:
    """Read the command file and delete it atomically. Returns None if no
    command is pending, the file is malformed, or it disappeared mid-read.

    Reads and deletes in the same call so a slow command-processing step
    can't be triggered twice by the same file.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text()
    except (OSError, FileNotFoundError):
        return None
    # Best-effort unlink — race with a fresh write is harmless (the new
    # command will fire next poll).
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
