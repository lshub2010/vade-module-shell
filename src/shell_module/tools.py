"""Shell tools — one subprocess, no shell, a confined working directory.

The module exists for its four boundaries, not for `exec`:

1. **cwd** — resolved with `Path.resolve()` and then proven to be inside one of
   the roots from `SHELL_ROOTS` (default `SHELL_DATA_DIR`), compared with
   `Path.is_relative_to` (never a string prefix, so `/data/ws-evil` cannot pass
   for `/data/ws`). No roots configured means every call is refused: the module
   fails closed.
2. **no shell** — the command string is `shlex.split` and handed to
   `create_subprocess_exec`-style `Popen(argv)`. `;`, `|`, `&&`, redirects,
   `$(...)` and backticks are *refused* rather than silently passed as literal
   arguments: a model that writes a pipeline must learn it is not supported,
   not get `ls: cannot access '|'`.
3. **time** — `timeout_s` in `[1, 120]`; on expiry the whole process group is
   SIGKILLed and the call returns `timed_out=true` instead of raising. A
   grandchild that outlives its parent dies with the group.
4. **size** — stdout and stderr share one 48 KiB result budget (24 KiB each
   before the JSON fit; `MAX_RESULT_BYTES`), because a v1 frame carries 65519
   bytes and a result that does not fit is a fatal call. The pipes are still
   *drained to EOF* and the surplus discarded; if we stopped reading, the child
   would block on a full pipe and `wait()` would hang forever.

The child does not inherit the module's environment: a command must not be able
to read `MODULE_KEY_PATH` / `CORE_PUBLIC_KEY` with `env`. It gets a small,
explicit set (PATH, HOME, TMPDIR, LANG/LC_ALL, TERM) and `HOME`/`TMPDIR` point
at the resolved cwd, so `~` and `mktemp` land inside the sandbox.

What this is *not*: a kernel sandbox. cwd only chooses where the command
starts; the command can still `cd ..` or open any file its uid can read. Real
isolation comes from the container (uid 10001, no writable layer outside
`/data`, see Dockerfile).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "tool",
    "ToolFailure",
    "MAX_RESULT_BYTES",
    "MAX_STREAM_BYTES",
    "MAX_COMMAND_BYTES",
    "FRAME_PLAINTEXT_BYTES",
    "MIN_TIMEOUT_S",
    "MAX_TIMEOUT_S",
    "configured_roots",
    "configured_allowlist",
    "resolve_cwd",
    "split_command",
    "shell_exec",
    "shell_which",
    "all_tools",
]

#: Budget for one *tool result*, measured on the JSON the SDK actually sends
#: (`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`). A v1 frame
#: carries at most 65519 plaintext bytes and v1 has no fragmentation, so the
#: budget leaves ~16 KiB for the JSON-RPC envelope, the duration/error fields
#: and the Noise tag. stdout and stderr share this one budget: a result that
#: looks fine locally but cannot be framed is a fatal call, not a big answer.
MAX_RESULT_BYTES = 48 * 1024

#: Per-stream share, before the JSON fit: a fixed half each. Fixed on purpose —
#: a split decided by whichever pipe the scheduler drained first would make the
#: same command return different bytes on different runs.
MAX_STREAM_BYTES = MAX_RESULT_BYTES // 2

#: One plaintext frame of the v1 transport is 65519 bytes and v1 has no
#: fragmentation. Kept as a documented constant next to `MAX_RESULT_BYTES` so
#: the reason for the 48 KiB budget is visible to readers instead of being
#: rediscovered in production.
FRAME_PLAINTEXT_BYTES = 65_519

#: One argv string above this cannot be executed on Linux anyway (a single
#: argument is capped at 128 KiB by the kernel); refusing early is honest.
MAX_COMMAND_BYTES = 32 * 1024

MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 120

#: Read size per pipe. Larger than the cap so a chatty command costs few
#: syscalls while the surplus is being discarded.
_READ_CHUNK = 64 * 1024

#: Characters a POSIX shell would interpret. Refused when unquoted; a literal
#: `|` inside single quotes is a normal argument and stays legal.
_METACHARACTERS = ";&|<>`\n\r"

#: Environment the child is allowed to see. Everything else — the module key,
#: the core URL, `SHELL_*` — stays in this process.
_ENV_PASSTHROUGH = ("LANG", "LC_ALL", "TZ")


class ToolFailure(Exception):
    """A structured failure raised by a pure tool.

    Deliberately NOT `vade_sdk.ToolError`: importing the SDK here would break
    the "write and test tools before the SDK" rule. `server.py` translates this
    into a ToolError with the same code, so nothing is lost.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def tool(
    name: str,
    description: str,
    *,
    risk: str = "low",
    timeout_ms: int = 30_000,
    idempotent: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach manifest metadata to a function (no SDK, no validation logic)."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.tool_meta = {  # type: ignore[attr-defined]
            "name": name,
            "description": description,
            "risk": risk,
            "timeout_ms": timeout_ms,
            "idempotent": idempotent,
        }
        return fn

    return decorate


# --------------------------------------------------------------------- arguments


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ToolFailure("INVALID_ARGUMENT", f"{field} must be a string, got {type(value).__name__}")
    if "\x00" in value:
        raise ToolFailure("INVALID_ARGUMENT", f"{field} must not contain NUL bytes")
    return value


def _require_int(value: Any, field: str) -> int:
    # bool is an int subclass; `timeout_s=True` must not become 1 second.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolFailure("INVALID_ARGUMENT", f"{field} must be an integer, got {type(value).__name__}")
    return value


# ------------------------------------------------------------------------ roots


def _split_env_list(raw: str) -> list[str]:
    """Split on `os.pathsep` (':' on POSIX) and drop blanks."""
    return [chunk.strip() for chunk in raw.split(os.pathsep) if chunk.strip()]


def configured_roots() -> tuple[Path, ...]:
    """Roots, resolved, from `SHELL_ROOTS` (default `SHELL_DATA_DIR`), per call.

    Read per call on purpose: an operator can move the boundary without a
    restart, and a test can move it without re-importing the module.
    """
    raw = os.environ.get("SHELL_ROOTS")
    if raw is None:
        raw = os.environ.get("SHELL_DATA_DIR", "")
    roots: list[Path] = []
    for chunk in _split_env_list(raw):
        try:
            resolved = Path(chunk).resolve(strict=False)
        except OSError:  # an unresolvable root cannot widen the boundary
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def configured_allowlist() -> tuple[str, ...]:
    """`SHELL_ALLOWLIST` as bare command names; empty means "no list".

    Entries are reduced to their basename too (`/usr/bin/curl` means `curl`):
    matching happens on `argv[0]`'s basename, so an entry that keeps a directory
    would never match and would silently deny a command the operator meant to
    allow.
    """
    raw = os.environ.get("SHELL_ALLOWLIST", "")
    names: list[str] = []
    for chunk in _split_env_list(raw):
        name = os.path.basename(chunk) or chunk
        if name not in names:
            names.append(name)
    return tuple(names)


def _check_allowed(argv0: str) -> None:
    """Enforce `SHELL_ALLOWLIST` against the basename of `argv[0]`.

    Basename, not the literal string: `/usr/local/bin/python3` must be judged as
    `python3`, otherwise an absolute path would slip past the list. An empty
    list means "no restriction here" — approval is core's job, this knob is the
    operator's.
    """
    allowed = configured_allowlist()
    if not allowed:
        return
    name = os.path.basename(argv0)
    if name not in allowed:
        raise ToolFailure(
            "PERMISSION_DENIED",
            f"command {name!r} is not in SHELL_ALLOWLIST (allowed: {', '.join(allowed)})",
        )


def resolve_cwd(cwd: Any) -> tuple[Path, Path, str]:
    """Resolve `cwd` and prove it is an existing directory inside a root.

    Returns `(resolved, matched_root, root_relative_path)`, where the last
    element is "." for the root itself. Relative values are taken relative to
    the **first** configured root — the only deterministic reading when several
    roots exist, and what a model means by `"subdir"`.

    Membership is checked *before* existence, so a non-existent outside path
    answers `PERMISSION_DENIED`, not `NOT_FOUND`: the error must not become an
    oracle for what exists outside the roots.
    """
    text = _require_str(cwd, "cwd")
    roots = configured_roots()
    if not roots:
        raise ToolFailure(
            "PERMISSION_DENIED",
            "no shell roots are configured (set SHELL_ROOTS or SHELL_DATA_DIR)",
        )

    candidate = Path(text) if text else roots[0]
    if not candidate.is_absolute():
        candidate = roots[0] / candidate

    try:
        real = candidate.resolve(strict=False)
    except OSError as exc:
        raise ToolFailure(
            "INVALID_ARGUMENT", f"cwd cannot be resolved: {exc.strerror or 'invalid path'}"
        ) from None

    for root in roots:
        if real == root or real.is_relative_to(root):
            if not real.exists():
                raise ToolFailure("NOT_FOUND", f"cwd {_relative(real, root)!r} does not exist")
            if not real.is_dir():
                raise ToolFailure(
                    "INVALID_ARGUMENT", f"cwd {_relative(real, root)!r} is not a directory"
                )
            return real, root, _relative(real, root)

    # No path in the message: the caller's spelling may be absolute, and the
    # roots themselves are host detail.
    raise ToolFailure("PERMISSION_DENIED", "cwd escapes the configured roots")


def _relative(path: Path, root: Path) -> str:
    return "." if path == root else path.relative_to(root).as_posix()


# ---------------------------------------------------------------- command syntax


def _reject_shell_syntax(command: str) -> None:
    """Refuse anything a POSIX shell would interpret, quote-aware.

    A tiny scanner rather than a blacklist on the raw string: `grep 'a|b'` is a
    legal single command whose argument happens to contain a pipe, and it must
    keep working. Inside single quotes everything is literal. Inside double
    quotes `$` and a backtick are still refused because a shell *would* have
    expanded them, and we do not — passing `$HOME` through as four characters
    would be the confusing outcome.
    """
    quote: str | None = None
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < length and command[index + 1] in '"\\$`':
                index += 2
                continue
            if char == '"':
                quote = None
                index += 1
                continue
            if char in "$`":
                raise _syntax_error(char)
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char in _METACHARACTERS or char == "$":
            raise _syntax_error(char)
        index += 1


def _syntax_error(char: str) -> ToolFailure:
    shown = {";": "';'", "|": "'|'", "&": "'&'", ">": "'>'", "<": "'<'", "\n": "a newline", "\r": "a newline"}.get(
        char, f"{char!r}"
    )
    return ToolFailure(
        "INVALID_ARGUMENT",
        f"command must be a single command without shell syntax (found {shown}); "
        "this module does not run a shell — pass one command and its arguments, "
        "and quote literal metacharacters",
    )


def split_command(command: Any) -> list[str]:
    """Turn the command string into `argv`, refusing shell syntax.

    Quoting is honoured by `shlex` (POSIX rules), so `sh -c` is *not* needed for
    an argument with spaces — and is not available either, because `sh` as
    `argv[0]` would reintroduce every metacharacter we just refused. That is a
    known limit, documented in the README: the way to run a compound action is
    a script file inside the roots.
    """
    text = _require_str(command, "command")
    if not text.strip():
        raise ToolFailure("INVALID_ARGUMENT", "command must not be empty")
    payload = text.encode("utf-8")
    if len(payload) > MAX_COMMAND_BYTES:
        raise ToolFailure(
            "PAYLOAD_TOO_LARGE",
            f"command is {len(payload)} bytes; the limit is {MAX_COMMAND_BYTES}",
        )
    _reject_shell_syntax(text)
    try:
        argv = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ToolFailure("INVALID_ARGUMENT", f"command is not parseable: {exc}") from None
    if not argv:
        raise ToolFailure("INVALID_ARGUMENT", "command must not be empty")
    return argv


# ------------------------------------------------------------------- environment


def _child_path() -> str:
    """The PATH the child will search — and the one `shell_which` reports on."""
    return os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"


def _child_env(cwd: Path) -> dict[str, str]:
    """A minimal, explicit environment: nothing of ours leaks into the child.

    `HOME` and `TMPDIR` are the resolved cwd, so `~/.cache` and `mktemp` write
    inside the sandbox instead of into the module's home directory.
    """
    env = {
        "PATH": _child_path(),
        "HOME": str(cwd),
        "TMPDIR": str(cwd),
        "TERM": "dumb",
        "LANG": os.environ.get("LANG") or "C.UTF-8",
    }
    for name in _ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


# ---------------------------------------------------------------------- capture


class _Capture:
    """Keep the first `limit` bytes of a stream, count the rest, never block."""

    __slots__ = ("limit", "buffer", "total")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.limit - len(self.buffer)
        if room > 0:
            self.buffer += chunk[:room]

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def text(self) -> str:
        # errors="replace": a command may print binary or cut a UTF-8 character
        # at the cap; that is not a reason to fail the call.
        return bytes(self.buffer).decode("utf-8", "replace")


def _pump(stream: Any, sink: _Capture) -> None:
    """Drain one pipe to EOF, feeding the (bounded) sink.

    EOF matters more than the cap: the surplus is read and dropped so the child
    never sees a full pipe. Errors are swallowed because the only thing left to
    report is "the pipe died", which `exit_code` already tells.
    """
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            sink.feed(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:  # pragma: no cover - close of a broken pipe
            pass


def _json_bytes(value: Any) -> int:
    """Size of `value` as the SDK serialises it: compact, non-ASCII kept."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fit_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    cut: bool,
) -> dict[str, Any]:
    """Assemble the result, trimming text until the whole JSON fits the budget.

    The per-stream caps bound *raw* bytes; escaping is what can still push a
    result past the frame (`\\n` costs two bytes, a replaced byte three). A pair
    that fits is returned untouched; otherwise each stream is held to its own
    half of the frame — measured with the envelope, so the two halves plus the
    skeleton can never add up to more than `MAX_RESULT_BYTES`. A stream already
    inside its half is kept whole: slack is not spent on the other side, which
    is what keeps the split independent of who wrote more.
    """
    total_out, total_err = len(stdout), len(stderr)

    def build(out: str, err: str) -> dict[str, Any]:
        return {
            "exit_code": exit_code,
            "stdout": out,
            "stderr": err,
            # `cut` is the capture verdict (bytes past the per-stream cap were
            # dropped); the length checks catch a trim made here.
            "truncated": cut or len(out) < total_out or len(err) < total_err,
            "timed_out": timed_out,
        }

    def longest_prefix(text: str, fits: Callable[[str], bool]) -> str:
        """Longest prefix of `text` satisfying `fits` (monotone in length)."""
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if fits(text[:middle]):
                low = middle
            else:
                high = middle - 1
        return text[:low]

    if _json_bytes(build(stdout, stderr)) <= MAX_RESULT_BYTES:
        return build(stdout, stderr)

    # Over budget: give each stream its half, skeleton included. Trimming one
    # stream to fit around the other would empty it when both are escape-heavy.
    half = MAX_RESULT_BYTES // 2
    stdout = longest_prefix(stdout, lambda candidate: _json_bytes(build(candidate, "")) <= half)
    stderr = longest_prefix(stderr, lambda candidate: _json_bytes(build("", candidate)) <= half)
    return build(stdout, stderr)


# ------------------------------------------------------------------------ spawn


def _spawn(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Start the child in its own session so the whole group can be killed."""
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }
    if os.name == "posix":
        # New session => new process group => killpg reaches grandchildren.
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs)  # noqa: S603 - argv, never shell=True
    except FileNotFoundError:
        raise ToolFailure("NOT_FOUND", f"command not found: {argv[0]}") from None
    except PermissionError:
        raise ToolFailure("PERMISSION_DENIED", f"command is not executable: {argv[0]}") from None
    except OSError as exc:
        # `strerror` only: the cwd may be a host path and must not be echoed.
        raise ToolFailure("INTERNAL", f"cannot start command: {exc.strerror or type(exc).__name__}") from None


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's process group; fall back to the child alone."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


# ------------------------------------------------------------------------ tools


@tool(
    "shell_exec",
    "在受限工作目录中执行单个命令（不经过 shell）：返回 exit_code/stdout/stderr；"
    "超时 kill 整个进程组并返回 timed_out=true；stdout+stderr 合计最多 48 KiB（含 JSON 信封）",
    risk="high",
    timeout_ms=130_000,
    idempotent=False,
)
def shell_exec(command: str, timeout_s: int = 30, cwd: str = ".") -> dict[str, Any]:
    """Run one command. A non-zero exit is a result, not an error.

    `exit_code` is the child's status; on timeout it is `-9` (SIGKILL) and
    `timed_out` is true. `truncated` is true when either stream exceeded its
    `MAX_STREAM_BYTES` share, or when the two together still did not fit
    `MAX_RESULT_BYTES` once escaped as JSON — the command still ran to
    completion, only the copy we keep is short.
    """
    timeout = _require_int(timeout_s, "timeout_s")
    if not (MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S):
        raise ToolFailure(
            "INVALID_ARGUMENT",
            f"timeout_s must be within [{MIN_TIMEOUT_S}, {MAX_TIMEOUT_S}], got {timeout}",
        )

    argv = split_command(command)
    _check_allowed(argv[0])
    real, _root, _relative_cwd = resolve_cwd(cwd)

    proc = _spawn(argv, real, _child_env(real))
    out = _Capture(MAX_STREAM_BYTES)
    err = _Capture(MAX_STREAM_BYTES)
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True),
    ]
    for thread in pumps:
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL was ignored
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # Bounded join: a grandchild that escaped the group could hold a pipe open
    # forever, and "the output stopped" must not turn into "the call hangs".
    for thread in pumps:
        thread.join(timeout=5)

    exit_code = proc.returncode if proc.returncode is not None else -1
    return _fit_result(
        exit_code,
        out.text(),
        err.text(),
        timed_out=timed_out,
        cut=out.truncated or err.truncated,
    )


@tool(
    "shell_which",
    "在 PATH 中解析命令的绝对路径；未找到时 path 为 null（只接受裸命令名，不接受路径）",
    risk="low",
    timeout_ms=10_000,
    idempotent=True,
)
def shell_which(name: str) -> dict[str, Any]:
    """Where would `shell_exec(name, ...)` find `name`?

    `path` is null when nothing executable matches, which is a fact about the
    host, not a failure of the call. A name containing a path separator is
    refused: `which("/etc/passwd")` would otherwise become a file-existence
    oracle outside the roots.
    """
    text = _require_str(name, "name")
    if not text.strip():
        raise ToolFailure("INVALID_ARGUMENT", "name must not be empty")
    if "/" in text or "\\" in text:
        raise ToolFailure(
            "INVALID_ARGUMENT", "name must be a bare command name; path separators are not accepted"
        )
    _check_allowed(text)
    found = shutil.which(text, path=_child_path())
    return {"path": found}


def all_tools() -> tuple[Callable[..., Any], ...]:
    """Every decorated tool, in declaration order (manifest order is stable)."""
    return (shell_exec, shell_which)
