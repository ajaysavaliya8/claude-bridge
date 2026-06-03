"""Unit tests for the MCP registration helper (the per-call timeout is critical:
a wrong value silently truncates blocking ask_peer in production)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("register_mcp", ROOT / "scripts" / "register_mcp.py")
register_mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_mcp)


def test_build_invocation_timeout_and_argv():
    cmd, timeout_ms = register_mcp.build_invocation(
        peer_self="web", default_target="api", broker_url="http://127.0.0.1:8765",
        python_bin="C:/py/python.exe", ask_timeout=300, name="bridge", scope="user",
    )
    assert timeout_ms == 360000  # (300 + 60) * 1000
    assert cmd[1:4] == ["mcp", "add-json", "bridge"]
    assert cmd[-2:] == ["--scope", "user"]
    config = json.loads(cmd[4])
    assert config["timeout"] == 360000
    assert config["command"] == "C:/py/python.exe"
    assert config["args"][0].endswith("bridge_mcp.py")
    assert config["env"]["PEER_SELF"] == "web"
    assert config["env"]["DEFAULT_TARGET"] == "api"


def test_build_invocation_omits_empty_default_target():
    cmd, timeout_ms = register_mcp.build_invocation(
        peer_self="payments", default_target="", broker_url="x",
        python_bin="python", ask_timeout=100, name="bridge", scope="local",
    )
    assert timeout_ms == 160000
    config = json.loads(cmd[4])
    assert "DEFAULT_TARGET" not in config["env"]


def test_ask_timeout_tolerant_of_bad_values(monkeypatch):
    monkeypatch.setenv("ASK_TIMEOUT_SECONDS", "")
    assert register_mcp._ask_timeout_seconds() == 300
    monkeypatch.setenv("ASK_TIMEOUT_SECONDS", "not-a-number")
    assert register_mcp._ask_timeout_seconds() == 300
    monkeypatch.setenv("ASK_TIMEOUT_SECONDS", "120")
    assert register_mcp._ask_timeout_seconds() == 120


def test_dotenv_strips_surrounding_quotes(tmp_path, monkeypatch):
    # A quoted value must not reach the config with literal surrounding quotes.
    monkeypatch.setattr(register_mcp, "BRIDGE_DIR", tmp_path)
    (tmp_path / ".env").write_text('DEFAULT_TARGET="abc123"\nBROKER_URL=http://x\n', encoding="utf-8")
    monkeypatch.delenv("DEFAULT_TARGET", raising=False)
    monkeypatch.delenv("BROKER_URL", raising=False)
    register_mcp._load_dotenv()
    import os

    assert os.environ["DEFAULT_TARGET"] == "abc123"  # surrounding quotes stripped
    assert os.environ["BROKER_URL"] == "http://x"
