"""Process isolation and the RPC relay for PactPay evaluations (parent side).

Three kinds of sandboxed process, each in its own ``bwrap`` (or a plain subprocess when bwrap is unusable):

* the **scanner** parses the submission (``prescan.scan_python``) under a memory limit;
* the **worker** holds the untrusted submission and answers RPCs about it;
* the **runner** executes the agreed test code and signs every verdict with an HMAC key it got on stdin.

The parent relays line-delimited JSON between runner and worker. It accepts verdicts only from the runner's channel,
and only with a valid sequence number and HMAC. The worker never sees the tests, the key or the verdict channel.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _proto

HERE = Path(__file__).resolve().parent

CALL_TIMEOUT_S = 5.0
READY_TIMEOUT_S = 8.0
SCAN_TIMEOUT_S = 10.0
MAX_WORKER_RESTARTS = 4
MAX_STRAY_LINES = 1000
LIMITS = {"cpu_s": 10, "as_bytes": 1 << 30, "fsize_bytes": 1 << 20, "nofile": 64}
TMPFS_BYTES = 16 << 20

_EOF = object()
_TIMEOUT = object()
_BAD = object()

_ROLE_FILES = {"worker": "_worker.py", "runner": "_runner.py", "scan": "prescan.py"}
_code_cache: dict[str, str] = {}
_probe_lock = threading.Lock()
_probe_result: dict[str, Any] = {}

# Tests may set this to force the subprocess fallback.
FORCE_SUBPROCESS = False


@dataclass(frozen=True)
class Sandbox:
    kind: str  # "bwrap" | "subprocess"
    python: str
    bwrap: str | None = None
    binds: tuple[str, ...] = ()

    @property
    def network(self) -> str:
        return "blocked" if self.kind == "bwrap" else "not_blocked"

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "network": self.network,
            "limits": {
                "cpu_s": LIMITS["cpu_s"],
                "address_space_mb": LIMITS["as_bytes"] >> 20,
                "file_size_mb": LIMITS["fsize_bytes"] >> 20,
                "open_files": LIMITS["nofile"],
                "call_timeout_s": CALL_TIMEOUT_S,
                "import_timeout_s": READY_TIMEOUT_S,
                "tmpfs_mb": TMPFS_BYTES >> 20 if self.kind == "bwrap" else None,
            },
            "isolation": (
                "bwrap --unshare-all (no network, own pid/ipc/uts/user namespaces), read-only /usr and Python, "
                "tmpfs work dir, empty environment, audit hook"
                if self.kind == "bwrap"
                else "python -I, empty environment, temp cwd, own session, rlimits, audit hook (no namespace isolation)"
            ),
        }


def _python_executable() -> str:
    return os.path.realpath(sys.executable)


def _ro_binds(python: str) -> tuple[str, ...]:
    binds: list[str] = []
    for candidate in (sys.base_prefix, sys.base_exec_prefix, str(Path(python).parent.parent)):
        path = os.path.realpath(candidate)
        if path.startswith("/usr/") or path == "/usr" or not os.path.isdir(path):
            continue
        if not any(path == b or path.startswith(b + "/") for b in binds):
            binds.append(path)
    return tuple(binds)


def _bwrap_argv(sandbox: Sandbox) -> list[str]:
    argv = [sandbox.bwrap or "bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
            "--new-session", "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        path = "/" + name
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            argv += ["--ro-bind", path, path]
    for bind in sandbox.binds:
        argv += ["--ro-bind", bind, bind]
    argv += ["--dev", "/dev", "--size", str(TMPFS_BYTES), "--tmpfs", "/tmp", "--size", str(TMPFS_BYTES),
             "--tmpfs", "/work", "--chdir", "/work", "--clearenv", "--setenv", "LANG", "C.UTF-8", "--"]
    return argv


def _probe_bwrap(bwrap: str, python: str, binds: tuple[str, ...]) -> bool:
    candidate = Sandbox("bwrap", python, bwrap, binds)
    code = ("import socket\ntry:\n    socket.create_connection(('192.0.2.1', 9), timeout=1)\n    print('NET')\n"
            "except OSError:\n    print('OK')\n")
    try:
        out = subprocess.run(_bwrap_argv(candidate) + [python, "-I", "-B", "-c", code], capture_output=True,
                             timeout=20, env={}, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == b"OK"


def detect() -> Sandbox:
    """The best sandbox this host supports (bwrap if it runs and blocks the network, else plain subprocesses)."""
    python = _python_executable()
    if FORCE_SUBPROCESS:
        return Sandbox("subprocess", python)
    with _probe_lock:
        key = python
        if key not in _probe_result:
            bwrap = shutil.which("bwrap") or ("/usr/bin/bwrap" if os.path.exists("/usr/bin/bwrap") else None)
            binds = _ro_binds(python)
            ok = bool(bwrap) and _probe_bwrap(bwrap, python, binds)  # type: ignore[arg-type]
            _probe_result[key] = Sandbox("bwrap", python, bwrap, binds) if ok else Sandbox("subprocess", python)
        return _probe_result[key]


def _role_code(role: str, config: dict[str, Any]) -> str:
    if role not in _code_cache:
        proto = (HERE / "_proto.py").read_text(encoding="utf-8")
        body = (HERE / _ROLE_FILES[role]).read_text(encoding="utf-8")
        _code_cache[role] = proto + "\n\n" + body + "\n"
    return _code_cache[role] + "\nmain(%r)\n" % (config,)


class _Proc:
    """One sandboxed process with a line reader on stdout and a bounded drain on stderr."""

    def __init__(self, sandbox: Sandbox, role: str, read_roots: list[str]):
        config = {"limits": dict(LIMITS), "read_roots": read_roots}
        cmd = [sandbox.python, "-I", "-B", "-c", _role_code(role, config)]
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        if sandbox.kind == "bwrap":
            argv, cwd = _bwrap_argv(sandbox) + cmd, None
        else:
            self._tmp = tempfile.TemporaryDirectory(prefix="pactpay-eval-")
            argv, cwd = cmd, self._tmp.name
        self.role = role
        self.popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={}, cwd=cwd, start_new_session=True, close_fds=True)
        self._lines: queue.Queue[Any] = queue.Queue()
        self._stderr_tail = bytearray()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        stream = self.popen.stdout
        assert stream is not None
        try:
            while True:
                line = stream.readline(_proto.MAX_LINE + 1)
                if not line:
                    self._lines.put(_EOF)
                    return
                if len(line) > _proto.MAX_LINE or not line.endswith(b"\n"):
                    self._lines.put(_BAD)
                    return
                try:
                    self._lines.put(json.loads(line))
                except (ValueError, RecursionError):
                    self._lines.put(_BAD)
        except (OSError, ValueError):
            self._lines.put(_EOF)

    def _pump_stderr(self) -> None:
        stream = self.popen.stderr
        assert stream is not None
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    return
                self._stderr_tail += chunk
                if len(self._stderr_tail) > 8192:
                    del self._stderr_tail[:-8192]
        except (OSError, ValueError):
            return

    def stderr_tail(self, limit: int = 600) -> str:
        return bytes(self._stderr_tail[-limit:]).decode("utf-8", "replace")

    def send(self, msg: dict[str, Any], timeout: float) -> bool:
        try:
            data = _proto.dumps_line(msg)
        except (TypeError, ValueError):
            return False
        result = {"ok": False}
        done = threading.Event()

        def write() -> None:
            try:
                assert self.popen.stdin is not None
                self.popen.stdin.write(data)
                self.popen.stdin.flush()
                result["ok"] = True
            except (OSError, ValueError):
                pass
            finally:
                done.set()

        threading.Thread(target=write, daemon=True).start()
        if not done.wait(max(0.05, timeout)):
            self.kill()
            return False
        return result["ok"]

    def recv(self, timeout: float) -> Any:
        try:
            return self._lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return _TIMEOUT

    def alive(self) -> bool:
        return self.popen.poll() is None

    def kill(self) -> None:
        try:
            os.killpg(self.popen.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self.popen.kill()
        except OSError:
            pass
        try:
            self.popen.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


def _read_roots(sandbox: Sandbox) -> list[str]:
    roots = {os.path.realpath(sys.base_prefix), os.path.realpath(sys.base_exec_prefix),
             str(Path(sandbox.python).parent.parent), "/dev/null", "/dev/urandom"}
    return sorted(roots)


# ----------------------------------------------------------------------------------------------- scanner ------
def run_scan(source: str, sandbox: Sandbox, timeout: float) -> dict[str, Any]:
    """The static pre-scan in its own sandbox. -> scan dict with "ok" (False when it could not finish)."""
    proc = _Proc(sandbox, "scan", _read_roots(sandbox))
    try:
        deadline = time.monotonic() + max(0.5, timeout)
        if not proc.send({"source": source}, timeout=max(0.5, deadline - time.monotonic())):
            return {"ok": False, "error": "the scanner did not accept the source"}
        msg = proc.recv(deadline - time.monotonic())
        if msg is _TIMEOUT:
            return {"ok": False, "error": "the scan did not finish within %.0f s" % timeout}
        if not isinstance(msg, dict):
            return {"ok": False, "error": "the scanner exited without a result (memory limit?)"}
        if not msg.get("ok"):
            return {"ok": False, "error": str(msg.get("error") or "scan failed")[:300]}
        return msg
    finally:
        proc.kill()


# -------------------------------------------------------------------------------------------- test run ------
def list_test_names(source: str) -> list[str]:
    """Top-level ``def test_*`` names in a criterion's (trusted) test source, in order. Raises SyntaxError."""
    tree = ast.parse(source)
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]


@dataclass
class SuiteRun:
    results: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    suite_errors: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    completed: bool = False
    timed_out: bool = False
    harness_error: str | None = None
    import_error: str | None = None
    worker_spawns: int = 0
    rpc_count: int = 0
    rejected_lines: int = 0
    seed: int = 0

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)


def _err(rpc_id: Any, type_name: str, message: str) -> dict[str, Any]:
    return {"t": "resp", "id": rpc_id, "ok": False, "error": {"type": type_name, "msg": message[:500]}}


class _Worker:
    """Spawns the worker lazily, restarts it after a crash, and refuses calls that already timed out."""

    def __init__(self, sandbox: Sandbox, mode: str, filename: str, content: str, run: SuiteRun, deadline: float):
        self.sandbox = sandbox
        self.mode = mode
        self.filename = filename
        self.content = content
        self.run = run
        self.deadline = deadline
        self.proc: _Proc | None = None
        self.generation = 0
        self.fatal: str | None = None
        self.poisoned: set[tuple[str, str]] = set()

    def _spawn(self) -> None:
        self.generation += 1
        self.run.worker_spawns += 1
        nonce = secrets.token_hex(12)
        proc = _Proc(self.sandbox, "worker", _read_roots(self.sandbox))
        ready_deadline = min(self.deadline, time.monotonic() + READY_TIMEOUT_S)
        init = {"nonce": nonce, "mode": self.mode, "filename": self.filename, "content": self.content,
                "ref_base": self.generation * 1_000_000_000}
        if not proc.send(init, timeout=max(0.5, ready_deadline - time.monotonic())):
            proc.kill()
            self.fatal = "the submission process did not start"
            self.run.flag("worker_crashed")
            return
        while True:
            msg = proc.recv(ready_deadline - time.monotonic())
            if msg is _TIMEOUT:
                proc.kill()
                self.fatal = "the submission did not finish loading within %.0f s" % READY_TIMEOUT_S
                self.run.import_error = self.fatal
                self.run.flag("timeout")
                return
            if msg is _EOF or msg is _BAD:
                proc.kill()
                self.fatal = "the submission process exited while loading"
                self.run.import_error = self.fatal
                self.run.flag("worker_crashed")
                return
            if isinstance(msg, dict) and msg.get("t") == "ready" and msg.get("nonce") == nonce:
                break
            self.run.rejected_lines += 1
        if msg.get("ok") is not True:
            proc.kill()
            error = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            detail = "%s: %s" % (str(error.get("type") or "Error")[:80], str(error.get("msg") or "")[:300])
            self.fatal = "the submission failed to load (%s)" % detail
            self.run.import_error = detail
            self.run.flag("import_error")
            return
        self.proc = proc

    def handle(self, rpc: dict[str, Any]) -> dict[str, Any]:
        rpc_id = rpc.get("id")
        op = rpc.get("op")
        if op not in ("getattr", "call", "new", "value"):
            return _err(rpc_id, "ProtocolError", "unknown op")
        key = (str(op), str(rpc.get("name") or rpc.get("kind") or ""))
        if key in self.poisoned:
            return _err(rpc_id, "Timeout", "an earlier %s of %s timed out" % key)
        if self.fatal:
            return _err(rpc_id, "SubmissionUnavailable", self.fatal)
        if self.proc is None or not self.proc.alive():
            if self.proc is not None:
                self.proc.kill()
                self.proc = None
            if self.generation > MAX_WORKER_RESTARTS:
                self.fatal = "the submission process crashed %d times" % self.generation
                return _err(rpc_id, "SubmissionUnavailable", self.fatal)
            self._spawn()
            if self.fatal or self.proc is None:
                return _err(rpc_id, "SubmissionUnavailable", self.fatal or "the submission did not start")
        proc = self.proc
        nonce = secrets.token_hex(12)
        request = {"id": rpc_id, "nonce": nonce, "op": op}
        for name in ("name", "args", "kwargs", "kind", "init", "ref"):
            if name in rpc:
                request[name] = rpc[name]
        call_deadline = min(self.deadline, time.monotonic() + CALL_TIMEOUT_S)
        self.run.rpc_count += 1
        if not proc.send(request, timeout=max(0.05, call_deadline - time.monotonic())):
            self._lost(proc, "worker_crashed")
            return _err(rpc_id, "WorkerCrashed", "the submission process stopped reading")
        stray = 0
        while True:
            msg = proc.recv(call_deadline - time.monotonic())
            if msg is _TIMEOUT:
                self._lost(proc, "timeout")
                self.poisoned.add(key)
                return _err(rpc_id, "Timeout", "%s %s did not answer within %.1f s" % (op, key[1], CALL_TIMEOUT_S))
            if msg is _EOF or msg is _BAD:
                self._lost(proc, "worker_crashed")
                return _err(rpc_id, "WorkerCrashed", "the submission process exited during %s %s" % key)
            if (isinstance(msg, dict) and msg.get("t") == "resp" and msg.get("id") == rpc_id
                    and msg.get("nonce") == nonce):
                if msg.get("ok") is True and "value" in msg:
                    return {"t": "resp", "id": rpc_id, "ok": True, "value": msg["value"]}
                error = msg.get("error") if isinstance(msg.get("error"), dict) else {}
                type_name = str(error.get("type") or "Error")[:80]
                if type_name == "Timeout":  # only the parent decides what a timeout is
                    type_name = "SubmissionTimeoutError"
                return _err(rpc_id, type_name, str(error.get("msg") or ""))
            stray += 1
            self.run.rejected_lines += 1
            if stray > MAX_STRAY_LINES:
                self._lost(proc, "worker_protocol_abuse")
                return _err(rpc_id, "WorkerCrashed", "the submission process flooded its channel")

    def _lost(self, proc: _Proc, flag: str) -> None:
        self.run.flag(flag)
        proc.kill()
        if self.proc is proc:
            self.proc = None

    def close(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc = None


def run_tests(sandbox: Sandbox, *, mode: str, filename: str, content: str, suites: list[dict[str, Any]],
              timeout_s: float) -> SuiteRun:
    """Run every suite ({"criterion_id", "source", "tests": [names]}) against the submission."""
    run = SuiteRun()
    deadline = time.monotonic() + max(1.0, timeout_s)
    key = secrets.token_bytes(32)
    seed = secrets.randbits(63)
    runner = _Proc(sandbox, "runner", _read_roots(sandbox))
    worker = _Worker(sandbox, mode, filename, content, run, deadline)
    expected_seq = 0
    try:
        if not runner.send({"key": key.hex(), "seed": seed, "suites": suites}, timeout=10.0):
            run.harness_error = "the test runner did not start: " + runner.stderr_tail()
            return run
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                run.timed_out = True
                run.flag("timeout")
                break
            msg = runner.recv(remaining)
            if msg is _TIMEOUT:
                run.timed_out = True
                run.flag("timeout")
                break
            if msg is _EOF or msg is _BAD or not isinstance(msg, dict):
                run.harness_error = "the test runner exited early: %s" % runner.stderr_tail(400)
                break
            kind = msg.get("t")
            if kind == "rpc":
                response = worker.handle(msg)
                if not runner.send(response, timeout=max(0.5, deadline - time.monotonic())):
                    run.harness_error = "the test runner stopped reading"
                    break
                continue
            if kind not in ("result", "suite_error", "done"):
                run.harness_error = "unexpected message on the runner channel"
                break
            body_text = msg.get("body")
            mac = msg.get("mac")
            if (not isinstance(body_text, str) or not isinstance(mac, str) or msg.get("seq") != expected_seq
                    or not verify_line(key, expected_seq, kind, body_text, mac)):
                run.rejected_lines += 1
                run.harness_error = "an unauthenticated verdict line was rejected"
                run.flag("verdict_forgery_rejected")
                break
            expected_seq += 1
            body = json.loads(body_text)
            if kind == "done":
                run.completed = True
                break
            if kind == "suite_error":
                run.suite_errors[str(body.get("criterion_id"))] = str(body.get("detail") or "")[:400]
                continue
            run.results[(str(body.get("criterion_id")), str(body.get("name")))] = {
                "name": str(body.get("name")),
                "passed": body.get("passed") is True,
                "detail": str(body.get("detail") or "")[:400],
                "duration_ms": float(body.get("duration_ms") or 0.0),
            }
    finally:
        runner.kill()
        worker.close()
        run.seed = seed
    return run


def sign_line(key: bytes, seq: int, kind: str, body_text: str) -> str:
    return hmac.new(key, ("%d|%s|%s" % (seq, kind, body_text)).encode("ascii"), hashlib.sha256).hexdigest()


def verify_line(key: bytes, seq: int, kind: str, body_text: str, mac: str) -> bool:
    try:
        expected = sign_line(key, seq, kind, body_text)
    except (UnicodeEncodeError, ValueError):
        return False
    return hmac.compare_digest(expected, mac)
