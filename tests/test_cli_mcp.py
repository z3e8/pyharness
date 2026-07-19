"""The pyharness-mcp CLI: editing the .mcp.json the session mounts."""

import json

import pytest

from pyharness.cli.mcp import main


def _run(monkeypatch, tmp_path, *argv):
    monkeypatch.setenv("PYHARNESS_MCP_CONFIG", str(tmp_path / "mcp.json"))
    monkeypatch.setattr("sys.argv", ["pyharness-mcp", *argv])
    main()
    return tmp_path / "mcp.json"


def test_add_local_server(monkeypatch, tmp_path, capsys):
    path = _run(
        monkeypatch,
        tmp_path,
        "add",
        "weather",
        "--command",
        "npx",
        "--arg=-y",
        "--arg",
        "weather-mcp",
        "--summary",
        "Weather lookups.",
        "--keyword",
        "forecast",
        "--category",
        "data",
    )
    spec = json.loads(path.read_text())["mcpServers"]["weather"]
    assert spec == {
        "command": "npx",
        "args": ["-y", "weather-mcp"],
        "summary": "Weather lookups.",
        "keywords": ["forecast"],
        "category": "data",
    }
    assert "added 'weather'" in capsys.readouterr().out


def test_add_remote_server_with_secret_ref(monkeypatch, tmp_path):
    path = _run(
        monkeypatch,
        tmp_path,
        "add",
        "gh",
        "--url",
        "https://mcp.example/mcp",
        "--header",
        "Authorization=secret:gh_token",
    )
    spec = json.loads(path.read_text())["mcpServers"]["gh"]
    assert spec["headers"] == {"Authorization": "secret:gh_token"}


def test_add_refuses_cleartext_credentials(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="cleartext"):
        _run(
            monkeypatch,
            tmp_path,
            "add",
            "gh",
            "--url",
            "https://x/mcp",
            "--header",
            "Authorization=Bearer abc",
        )
    assert not (tmp_path / "mcp.json").exists()


def test_add_refuses_duplicates_and_ambiguous_transport(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, "add", "one", "--command", "x")
    with pytest.raises(SystemExit, match="already exists"):
        _run(monkeypatch, tmp_path, "add", "one", "--command", "y")
    with pytest.raises(SystemExit, match="exactly one"):
        _run(monkeypatch, tmp_path, "add", "two")


def test_list_and_rm(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, "add", "one", "--command", "x", "--summary", "First.")
    _run(monkeypatch, tmp_path, "list")
    out = capsys.readouterr().out
    assert "one: x" in out and "First." in out
    path = _run(monkeypatch, tmp_path, "rm", "one")
    assert json.loads(path.read_text())["mcpServers"] == {}
    with pytest.raises(SystemExit, match="no server named"):
        _run(monkeypatch, tmp_path, "rm", "one")
