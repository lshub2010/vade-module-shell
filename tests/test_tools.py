"""shell tool tests — stdlib + pytest only, NO SDK import (module self-sufficiency).

Two things are tested here on purpose and they are not the same thing:

* the tool contract (`exec` result shape, `which`, non-zero exits, timeouts);
* the four boundaries — cwd, no-shell, time, size — plus the optional
  allowlist. Those are the module's reason to exist, so they are asserted with
  the exact code, not just "raises".

No test needs network. `sys.executable` is the command under test: it is the one
binary guaranteed to exist in the environment running the suite.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shell_module import tools  # noqa: E402

PY = sys.executable
PY_NAME = os.path.basename(PY)


class Sandbox:
    def __init__(self, root: Path, outside: Path) -> None:
        self.root = root
        self.outside = outside

    def script(self, name: str, body: str) -> str:
        """Write a throwaway python script into the root; return its command."""
        (self.root / name).write_text(body, encoding="utf-8")
        return f"{PY} {name}"


@pytest.fixture()
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setenv("SHELL_ROOTS", str(root))
    monkeypatch.delenv("SHELL_DATA_DIR", raising=False)
    monkeypatch.delenv("SHELL_ALLOWLIST", raising=False)
    return Sandbox(root, outside)


def _failure(code: str, fn, *args, **kwargs) -> tools.ToolFailure:
    with pytest.raises(tools.ToolFailure) as excinfo:
        fn(*args, **kwargs)
    assert excinfo.value.code == code, f"expected {code}, got {excinfo.value.code}: {excinfo.value.message}"
    return excinfo.value


# ------------------------------------------------------------------ self-sufficiency


def test_no_sdk_import_in_tools():
    """AST check, not a substring scan: a docstring may *mention* the SDK."""
    tree = ast.parse(Path(tools.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name == "vade_sdk" or name.startswith("vade_sdk.")]
    assert not [name for name in imported if name == "vade" or name.startswith("vade.")]


def test_documented_limits():
    assert tools.MAX_OUTPUT_BYTES == 64 * 1024
    assert tools.MAX_COMMAND_BYTES == 32 * 1024
    assert (tools.MIN_TIMEOUT_S, tools.MAX_TIMEOUT_S) == (1, 120)


def test_every_tool_is_decorated_and_unique():
    names = [fn.tool_meta["name"] for fn in tools.all_tools()]
    assert names == ["shell_exec", "shell_which"]
    assert len(names) == len(set(names))
    for fn in tools.all_tools():
        assert fn.__name__ == fn.tool_meta["name"]


def test_tool_names_are_legal_for_the_sdk():
    """Same pattern as `vade_sdk.spec.TOOL_NAME_RE`; dots are NOT allowed."""
    pattern = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
    for fn in tools.all_tools():
        assert pattern.match(fn.tool_meta["name"]), fn.tool_meta["name"]


def test_risk_and_idempotency_are_declared_per_tool():
    meta = {fn.tool_meta["name"]: fn.tool_meta for fn in tools.all_tools()}
    assert meta["shell_exec"]["risk"] == "high"
    assert meta["shell_exec"]["idempotent"] is False
    # The manifest timeout must outlive the tool's own maximum (120 s), or the
    # SDK would kill a legal call before the tool's own timeout reports.
    assert meta["shell_exec"]["timeout_ms"] > tools.MAX_TIMEOUT_S * 1000
    assert meta["shell_which"]["risk"] == "low"
    assert meta["shell_which"]["idempotent"] is True


# --------------------------------------------------------------------- configuration


def test_no_roots_means_every_call_is_denied(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SHELL_ROOTS", raising=False)
    monkeypatch.delenv("SHELL_DATA_DIR", raising=False)
    _failure("PERMISSION_DENIED", tools.shell_exec, "true")
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd="")


def test_root_defaults_to_shell_data_dir(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SHELL_ROOTS")
    monkeypatch.setenv("SHELL_DATA_DIR", str(box.root))
    assert tools.shell_exec(f"{PY} -c 'print(1)'")["stdout"] == "1\n"


def test_roots_are_read_on_every_call(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_ROOTS", str(box.outside))
    assert tools.shell_exec("true", cwd=str(box.outside))["exit_code"] == 0

    monkeypatch.setenv("SHELL_ROOTS", str(box.root))
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd=str(box.outside))


def test_multiple_roots_are_colon_separated(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_ROOTS", os.pathsep.join([str(box.root), str(box.outside)]))
    assert tools.shell_exec("true", cwd=str(box.outside))["exit_code"] == 0
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd="/")


def test_relative_cwd_uses_the_first_root(box: Sandbox):
    (box.root / "sub").mkdir()
    (box.root / "sub" / "write.py").write_text("open('made.txt', 'w').write('x')\n", encoding="utf-8")
    assert tools.shell_exec(f"{PY} write.py", cwd="sub")["exit_code"] == 0
    assert (box.root / "sub" / "made.txt").read_text(encoding="utf-8") == "x"


def test_empty_cwd_and_dot_both_mean_the_first_root(box: Sandbox):
    assert tools.shell_exec("true", cwd="")["exit_code"] == 0
    assert tools.shell_exec("true", cwd=".")["exit_code"] == 0
    assert tools.shell_exec("true", cwd="./")["exit_code"] == 0


def test_cwd_must_exist_and_be_a_directory(box: Sandbox):
    _failure("NOT_FOUND", tools.shell_exec, "true", cwd="missing")
    (box.root / "a.txt").write_text("x", encoding="utf-8")
    _failure("INVALID_ARGUMENT", tools.shell_exec, "true", cwd="a.txt")


def test_root_itself_may_be_a_symlink(box: Sandbox, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    alias = tmp_path / "alias"
    alias.symlink_to(box.root, target_is_directory=True)
    monkeypatch.setenv("SHELL_ROOTS", str(alias))
    assert tools.shell_exec("true", cwd=".")["exit_code"] == 0


def test_parent_traversal_is_denied(box: Sandbox):
    (box.outside / "secret.txt").write_text("secret", encoding="utf-8")
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd="..")
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd="../outside")
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd="sub/../../outside")


def test_absolute_path_outside_the_roots_is_denied(box: Sandbox):
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd=str(box.outside))
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd="/etc")


def test_sibling_with_the_same_prefix_is_denied(box: Sandbox, tmp_path: Path):
    """`/x/root-evil` must not pass for `/x/root`: no string prefix comparisons."""
    evil = tmp_path / "root-evil"
    evil.mkdir()
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd=str(evil))


def test_symlink_inside_the_root_pointing_outside_is_denied(box: Sandbox):
    (box.root / "escape").symlink_to(box.outside, target_is_directory=True)
    _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd="escape")


def test_cwd_rejects_wrong_types(box: Sandbox):
    _failure("INVALID_ARGUMENT", tools.shell_exec, "true", cwd=None)
    _failure("INVALID_ARGUMENT", tools.shell_exec, "true", cwd=123)
    _failure("INVALID_ARGUMENT", tools.shell_exec, "true", cwd="a\x00b")


# ------------------------------------------------------------------ command syntax


@pytest.mark.parametrize(
    "command",
    [
        "true; echo pwned",
        "true | cat",
        "true && echo pwned",
        "true & echo pwned",
        "echo hi > out.txt",
        "cat < /etc/passwd",
        "echo $(id)",
        "echo `id`",
        "echo $HOME",
        "echo hi\nrm -rf /",
        "echo hi\r\nid",
    ],
)
def test_shell_syntax_is_refused(box: Sandbox, command: str):
    """Refused, not silently passed as literal arguments: a model must learn it."""
    _failure("INVALID_ARGUMENT", tools.shell_exec, command)
    assert not any(box.root.iterdir())


def test_quoted_metacharacter_is_a_normal_argument(box: Sandbox):
    command = f"{PY} -c 'print(\"a|b;c\")'"
    result = tools.shell_exec(command)
    assert result["stdout"] == "a|b;c\n"
    assert result["exit_code"] == 0


def test_dollar_inside_double_quotes_is_refused(box: Sandbox):
    """A shell would have expanded it; we do not, so accepting it would lie."""
    _failure("INVALID_ARGUMENT", tools.shell_exec, f'{PY} -c "print(\'$HOME\')"')


def test_empty_and_unbalanced_commands_are_refused(box: Sandbox):
    _failure("INVALID_ARGUMENT", tools.shell_exec, "")
    _failure("INVALID_ARGUMENT", tools.shell_exec, "   ")
    _failure("INVALID_ARGUMENT", tools.shell_exec, "echo 'unterminated")
    _failure("INVALID_ARGUMENT", tools.shell_exec, 123)
    _failure("INVALID_ARGUMENT", tools.shell_exec, "true\x00")


def test_oversized_command_is_refused(box: Sandbox):
    _failure("PAYLOAD_TOO_LARGE", tools.shell_exec, "echo " + "x" * tools.MAX_COMMAND_BYTES)


def test_no_globbing_and_no_expansion(box: Sandbox):
    """There is no shell, so `*` is a literal — the honest behaviour, documented."""
    (box.root / "a.txt").write_text("x", encoding="utf-8")
    (box.root / "b.txt").write_text("x", encoding="utf-8")
    command = f"{PY} -c 'import sys; print(sys.argv[1])' '*'"
    assert tools.shell_exec(command)["stdout"] == "*\n"


# ------------------------------------------------------------------------ execution


def test_stdout_and_exit_code(box: Sandbox):
    result = tools.shell_exec(f"{PY} -c 'print(\"hello 世界\")'")
    assert result == {
        "exit_code": 0,
        "stdout": "hello 世界\n",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
    }


def test_nonzero_exit_is_a_result_not_an_error(box: Sandbox):
    result = tools.shell_exec(f"{PY} -c 'import sys; sys.exit(3)'")
    assert result["exit_code"] == 3
    assert result["timed_out"] is False
    assert result["stdout"] == ""


def test_stderr_is_captured_separately(box: Sandbox):
    result = tools.shell_exec(f"{PY} -c 'import sys; print(\"out\"); print(\"err\", file=sys.stderr)'")
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"
    assert result["exit_code"] == 0


def test_child_actually_runs_in_the_resolved_cwd(box: Sandbox):
    command = box.script("here.py", "import os\nprint(os.getcwd())\n")
    result = tools.shell_exec(command)
    assert Path(result["stdout"].strip()) == box.root.resolve()


def test_missing_command_is_not_found(box: Sandbox):
    _failure("NOT_FOUND", tools.shell_exec, "definitely-not-a-command-xyz")


def test_timeout_bounds_are_enforced(box: Sandbox):
    for bad in (0, -1, tools.MAX_TIMEOUT_S + 1, True, "5", 1.5, None):
        _failure("INVALID_ARGUMENT", tools.shell_exec, "true", bad)


def test_timeout_kills_the_process_group_and_returns(box: Sandbox):
    command = box.script("sleepy.py", "import time\ntime.sleep(60)\n")
    started = time.monotonic()
    result = tools.shell_exec(command, timeout_s=1)
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert result["exit_code"] == -9  # SIGKILL
    assert elapsed < 15, f"timeout took {elapsed:.1f}s"


def test_timeout_also_reaps_a_grandchild(box: Sandbox):
    """The child spawns a grandchild, then both sleep: the group kill gets both.

    If the grandchild survived, it would keep the stdout pipe open and the
    bounded thread join is what keeps this call from hanging — either way the
    call must return quickly with `timed_out=true`.
    """
    command = box.script(
        "family.py",
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n",
    )
    started = time.monotonic()
    result = tools.shell_exec(command, timeout_s=1)
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert elapsed < 15, f"timeout took {elapsed:.1f}s with a grandchild alive"


def test_partial_output_before_a_timeout_is_returned(box: Sandbox):
    command = box.script(
        "chatty.py",
        "import sys, time\nprint('before', flush=True)\ntime.sleep(60)\n",
    )
    result = tools.shell_exec(command, timeout_s=1)
    assert result["timed_out"] is True
    assert result["stdout"] == "before\n"


# ---------------------------------------------------------------------------- size


def test_each_stream_is_capped_at_64_kib(box: Sandbox):
    command = box.script(
        "flood.py",
        "import sys\n"
        "sys.stdout.write('o' * 200_000)\n"
        "sys.stderr.write('e' * 200_000)\n",
    )
    result = tools.shell_exec(command)
    assert result["exit_code"] == 0
    assert len(result["stdout"]) == tools.MAX_OUTPUT_BYTES
    assert len(result["stderr"]) == tools.MAX_OUTPUT_BYTES
    assert result["truncated"] is True


def test_not_truncated_at_exactly_the_cap(box: Sandbox):
    command = box.script("exact.py", "import sys\nsys.stdout.write('x' * 65536)\n")
    result = tools.shell_exec(command)
    assert len(result["stdout"]) == tools.MAX_OUTPUT_BYTES
    assert result["truncated"] is False


def test_a_flooding_command_still_finishes(box: Sandbox):
    """Truncation must not block the child: the surplus is drained and dropped."""
    command = box.script("huge.py", "import sys\nsys.stdout.write('x' * 4_000_000)\n")
    started = time.monotonic()
    result = tools.shell_exec(command, timeout_s=20)
    elapsed = time.monotonic() - started
    assert result["exit_code"] == 0
    assert result["truncated"] is True
    assert len(result["stdout"]) == tools.MAX_OUTPUT_BYTES
    assert elapsed < 15, f"draining took {elapsed:.1f}s"


def test_binary_output_is_replaced_not_fatal(box: Sandbox):
    command = box.script("binary.py", "import sys\nsys.stdout.buffer.write(b'a\\xff\\xfe\\x00b')\n")
    result = tools.shell_exec(command)
    assert result["exit_code"] == 0
    assert result["stdout"].startswith("a")
    assert result["stdout"].endswith("b")
    assert "\ufffd" in result["stdout"]


# --------------------------------------------------------------------- environment


def test_child_does_not_inherit_the_module_environment(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    """`env` in a command must not print the module key or the core URL."""
    monkeypatch.setenv("SHELL_TEST_SECRET", "leak-me")
    monkeypatch.setenv("MODULE_KEY_PATH", "/data/module.key")
    monkeypatch.setenv("CORE_PUBLIC_KEY", "00" * 32)
    command = box.script(
        "envdump.py",
        "import os\n"
        "for key in ('SHELL_TEST_SECRET', 'MODULE_KEY_PATH', 'CORE_PUBLIC_KEY'):\n"
        "    print(key, os.environ.get(key, 'absent'))\n",
    )
    result = tools.shell_exec(command)
    assert result["stdout"] == (
        "SHELL_TEST_SECRET absent\nMODULE_KEY_PATH absent\nCORE_PUBLIC_KEY absent\n"
    )


def test_child_home_and_tmpdir_point_at_the_cwd(box: Sandbox):
    (box.root / "sub").mkdir()
    command = f"{PY} -c 'import os;print(os.environ[\"HOME\"]);print(os.environ[\"TMPDIR\"])'"
    result = tools.shell_exec(command, cwd="sub")
    lines = result["stdout"].splitlines()
    assert lines == [str((box.root / "sub").resolve())] * 2


def test_path_is_present_and_never_contains_the_cwd(box: Sandbox):
    """`.` in PATH would let a file in the sandbox shadow a real command."""
    command = box.script("path.py", "import os\nprint(os.environ['PATH'])\n")
    entries = tools.shell_exec(command)["stdout"].strip().split(os.pathsep)
    assert entries
    assert "" not in entries
    assert "." not in entries


# ----------------------------------------------------------------------- allowlist


def test_allowlist_denies_commands_outside_the_list(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_ALLOWLIST", PY_NAME)
    assert tools.shell_exec(f"{PY} -c 'print(1)'")["stdout"] == "1\n"
    failure = _failure("PERMISSION_DENIED", tools.shell_exec, "true")
    assert "SHELL_ALLOWLIST" in failure.message


def test_allowlist_matches_the_basename_of_an_absolute_path(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_ALLOWLIST", "echo")
    assert tools.shell_exec("/bin/echo hi")["stdout"] == "hi\n"
    _failure("PERMISSION_DENIED", tools.shell_exec, PY)


def test_empty_allowlist_means_no_restriction(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_ALLOWLIST", "")
    assert tools.shell_exec("true")["exit_code"] == 0


def test_allowlist_is_not_a_path_list(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    """`SHELL_ALLOWLIST=/usr/bin/python3` is a name, not a path; basename wins."""
    monkeypatch.setenv("SHELL_ALLOWLIST", f"/usr/bin/{PY_NAME}")
    assert tools.shell_exec(f"{PY} -c 'print(2)'")["stdout"] == "2\n"


# --------------------------------------------------------------------------- which


def test_which_resolves_an_executable():
    found = tools.shell_which(PY_NAME)
    assert found["path"] is not None
    assert Path(found["path"]).is_absolute()
    assert tools.shell_which("definitely-not-a-command-xyz") == {"path": None}


def test_which_rejects_paths_and_empty_names(box: Sandbox):
    _failure("INVALID_ARGUMENT", tools.shell_which, "/etc/passwd")
    _failure("INVALID_ARGUMENT", tools.shell_which, "a/b")
    _failure("INVALID_ARGUMENT", tools.shell_which, "")
    _failure("INVALID_ARGUMENT", tools.shell_which, 123)


def test_which_respects_the_allowlist(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_ALLOWLIST", PY_NAME)
    assert tools.shell_which(PY_NAME)["path"] is not None
    _failure("PERMISSION_DENIED", tools.shell_which, "echo")


# ------------------------------------------------------------------ error messages


def test_errors_never_leak_absolute_paths(box: Sandbox):
    (box.root / "a.txt").write_text("x", encoding="utf-8")
    failure = _failure("PERMISSION_DENIED", tools.shell_exec, "true", cwd=str(box.outside))
    assert str(box.outside) not in failure.message
    assert str(box.root) not in failure.message

    missing = _failure("NOT_FOUND", tools.shell_exec, "true", cwd="nested/missing")
    assert str(box.root) not in missing.message
    assert "nested/missing" in missing.message

    not_dir = _failure("INVALID_ARGUMENT", tools.shell_exec, "true", cwd="a.txt")
    assert str(box.root) not in not_dir.message


def test_error_messages_do_not_contain_the_environment(box: Sandbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_ALLOWLIST", "echo")
    failure = _failure("PERMISSION_DENIED", tools.shell_exec, PY)
    assert "MODULE_KEY" not in failure.message
    assert "PATH=" not in failure.message
