import json
from pathlib import Path

from trend.commands import default_command_path, read_and_delete_command


def test_read_and_delete_returns_none_when_file_missing(tmp_path: Path):
    assert read_and_delete_command(tmp_path / "nope.json") is None


def test_read_and_delete_parses_and_unlinks(tmp_path: Path):
    target = tmp_path / "command.json"
    target.write_text(json.dumps({"id": "abc", "command": "pause"}))
    cmd = read_and_delete_command(target)
    assert cmd == {"id": "abc", "command": "pause"}
    assert not target.exists()  # deleted


def test_read_and_delete_returns_none_on_malformed_json(tmp_path: Path):
    target = tmp_path / "command.json"
    target.write_text("{ not json")
    assert read_and_delete_command(target) is None
    # Still deleted so a bad payload can't loop forever.
    assert not target.exists()


def test_default_command_path_in_home_dot_trend():
    p = default_command_path()
    assert p.name == "command.json"
    assert p.parent.name == ".trend"
    assert p.parent.parent == Path.home()
