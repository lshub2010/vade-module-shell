"""Adapter tests: manifest identity, schema generation, error mapping, CLI.

Skipped entirely when the SDK is not installed, so `pytest tests` still runs on
a bare Python (that is the module's own gate). The SDK is looked up in the
usual places: `VADE_SDK_PATH`, a vendored `sdk/python`, and the sibling Vade
checkout used during development.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT / "src"))


def _sdk_candidates() -> list[Path]:
    candidates: list[Path] = []
    if os.environ.get("VADE_SDK_PATH"):
        candidates.append(Path(os.environ["VADE_SDK_PATH"]))
    # vendored (repo layout `modules/shell`), then the local dev checkout
    candidates.append(MODULE_ROOT / "sdk" / "python")
    candidates.append(MODULE_ROOT.parent / "Vade" / "sdk" / "python")
    candidates.append(MODULE_ROOT.parents[1] / "sdk" / "python")
    return candidates


SDK_DIR = next((path for path in _sdk_candidates() if (path / "vade_sdk").is_dir()), None)
if SDK_DIR is not None:
    sys.path.insert(0, str(SDK_DIR))

pytest.importorskip("vade_sdk", reason="vade-sdk is required for the adapter tests")

from shell_module import server, tools  # noqa: E402

PY = sys.executable
PY_NAME = os.path.basename(PY)


@pytest.fixture()
def app():
    return server.build_app()


def test_module_identity_is_stable():
    assert server.MODULE_ID == "com.example.shell"
    assert server.MODULE_VERSION == "1.0.0"


def test_committed_manifest_matches_print_manifest(app):
    committed = json.loads((MODULE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert app.manifest() == committed


def test_check_is_clean(app):
    assert app.check() == []


def test_schema_comes_from_the_tool_signature(app):
    executing = app.tool("shell_exec").input_schema
    assert executing["required"] == ["command"]
    assert executing["properties"]["command"] == {"type": "string"}
    assert executing["properties"]["timeout_s"] == {"type": "integer", "default": 30}
    assert executing["properties"]["cwd"] == {"type": "string", "default": "."}
    assert executing["additionalProperties"] is False

    which = app.tool("shell_which").input_schema
    assert which["required"] == ["name"]
    assert which["properties"]["name"] == {"type": "string"}
    assert which["additionalProperties"] is False


def test_tool_metadata_comes_from_the_decorator(app):
    for fn in tools.all_tools():
        meta = fn.tool_meta  # type: ignore[attr-defined]
        spec = app.tool(meta["name"])
        assert spec.description == meta["description"]
        assert spec.risk == meta["risk"]
        assert spec.timeout_ms == meta["timeout_ms"]
        assert spec.idempotent is meta["idempotent"]


def test_exec_is_the_only_high_risk_tool(app):
    assert app.tool("shell_exec").risk == "high"
    assert app.tool("shell_exec").idempotent is False
    assert app.tool("shell_which").risk == "low"
    assert [name for name in app.tool_names if app.tool(name).risk == "high"] == ["shell_exec"]


async def test_structured_tool_error_keeps_its_code(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ROOTS", str(tmp_path))
    monkeypatch.delenv("SHELL_DATA_DIR", raising=False)
    envelope = await app.call_tool("shell_exec", {"command": "true", "cwd": "../../etc"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PERMISSION_DENIED"
    assert envelope["error"]["retriable"] is False
    assert str(tmp_path) not in envelope["error"]["message"]


async def test_exec_through_the_adapter(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ROOTS", str(tmp_path))
    monkeypatch.delenv("SHELL_DATA_DIR", raising=False)
    envelope = await app.call_tool("shell_exec", {"command": f"{PY} -c 'print(\"hi\")'", "cwd": "."})
    assert envelope["ok"] is True, envelope
    assert envelope["result"] == {
        "exit_code": 0,
        "stdout": "hi\n",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
    }


async def test_timeout_is_a_result_not_a_tool_error(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ROOTS", str(tmp_path))
    envelope = await app.call_tool(
        "shell_exec", {"command": f"{PY} -c 'import time; time.sleep(60)'", "timeout_s": 1}
    )
    assert envelope["ok"] is True, envelope
    assert envelope["result"]["timed_out"] is True
    assert envelope["result"]["exit_code"] == -9


async def test_shell_syntax_is_rejected_by_the_adapter(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ROOTS", str(tmp_path))
    envelope = await app.call_tool("shell_exec", {"command": "echo hi | cat"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"


async def test_which_through_the_adapter(app):
    envelope = await app.call_tool("shell_which", {"name": PY_NAME})
    assert envelope["ok"] is True, envelope
    assert Path(envelope["result"]["path"]).is_absolute()


async def test_unknown_tool_is_not_found(app):
    assert (await app.call_tool("shell_run", {"cmd": "id"}))["error"]["code"] == "NOT_FOUND"


async def test_wrong_type_is_rejected_by_the_sdk(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ROOTS", str(tmp_path))
    envelope = await app.call_tool("shell_exec", {"command": "true", "timeout_s": "30"})
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"


async def test_unknown_argument_is_rejected(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ROOTS", str(tmp_path))
    envelope = await app.call_tool("shell_exec", {"command": "true", "shell": True})
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"


async def test_missing_argument_is_rejected(app):
    assert (await app.call_tool("shell_exec", {"cwd": "."}))["error"]["code"] == "INVALID_ARGUMENT"


def _cli_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(MODULE_ROOT / "src")]
    if SDK_DIR is not None:
        parts.append(str(SDK_DIR))
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.pop("SHELL_ROOTS", None)
    env.pop("SHELL_DATA_DIR", None)
    env.pop("SHELL_ALLOWLIST", None)
    env.update(extra)
    return env


def test_standalone_entrypoint_output(tmp_path):
    """The real CLI, in a fresh interpreter (no event loop from pytest)."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shell_module",
            "--standalone",
            "shell_exec",
            "--args",
            json.dumps({"command": f"{PY} -c 'print(\"hi\")'"}),
        ],
        cwd=MODULE_ROOT,
        env=_cli_env(SHELL_ROOTS=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["stdout"] == "hi\n"
    assert "tool ok" in completed.stderr


def test_standalone_denies_an_escape(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shell_module",
            "--standalone",
            "shell_exec",
            "--args",
            '{"command":"true","cwd":"../../etc"}',
        ],
        cwd=MODULE_ROOT,
        env=_cli_env(SHELL_ROOTS=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert "PERMISSION_DENIED" in completed.stderr
    assert str(tmp_path) not in completed.stderr


def test_print_manifest_is_byte_identical_to_the_committed_file():
    completed = subprocess.run(
        [sys.executable, "-m", "shell_module", "--print-manifest"],
        cwd=MODULE_ROOT,
        env=_cli_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (MODULE_ROOT / "manifest.json").read_text(encoding="utf-8")
