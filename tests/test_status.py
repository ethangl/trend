import json
from pathlib import Path

from trend.status import default_status_path, write_status


def test_write_status_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "nested" / "dir" / "status.json"
    write_status(target, {"hello": "world"})
    assert target.exists()
    assert json.loads(target.read_text()) == {"hello": "world"}


def test_write_status_atomic_no_tmp_left_behind(tmp_path: Path):
    target = tmp_path / "status.json"
    write_status(target, {"a": 1})
    # No leftover .tmp file from atomic rename
    assert not any(p.suffix.endswith(".tmp") for p in tmp_path.iterdir())


def test_write_status_overwrites_existing(tmp_path: Path):
    target = tmp_path / "status.json"
    write_status(target, {"v": 1})
    write_status(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 2}


def test_write_status_serializes_non_json_via_default_str(tmp_path: Path):
    from datetime import datetime
    target = tmp_path / "status.json"
    write_status(target, {"now": datetime(2026, 5, 28, 12, 0, 0)})
    parsed = json.loads(target.read_text())
    assert parsed["now"] == "2026-05-28 12:00:00"


def test_default_status_path_in_home_dot_trend():
    p = default_status_path()
    assert p.name == "status.json"
    assert p.parent.name == ".trend"
    assert p.parent.parent == Path.home()
