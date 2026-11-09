"""The untrusted side of a PactPay evaluation: holds the submission and answers RPCs about it (stdlib only).

Inlined after ``_proto.py`` into ``python -I -B -c`` and run inside the sandbox. The worker never receives the
tests, the HMAC key or the verdict channel. It only answers ``getattr`` / ``call`` / ``new`` / ``value`` requests
with a value or an exception. Whatever it answers, the runner (a separate process) does every assertion itself.
"""

try:  # in-process import (unit tests); when inlined, the _proto names are already defined
    from ._proto import *  # noqa: F401,F403
    from ._proto import apply_limits, decode, encode, md_sections, read_msg, take_stdio, write_msg
except ImportError:
    pass

import os
import re
import sys

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,120}$")
_BLOCKED_IMPORTS = frozenset({"ctypes", "_ctypes", "cffi", "_testcapi", "_xxsubinterpreters"})
_DENIED_EVENTS = frozenset({
    # network
    "socket.__new__", "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.gethostbyaddr",
    # processes and signals
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
    "os.kill", "os.killpg", "signal.pthread_kill", "os.startfile", "pty.spawn",
    # filesystem changes
    "os.remove", "os.rmdir", "os.rename", "os.mkdir", "os.truncate", "os.chmod", "os.chown", "os.link",
    "os.symlink", "os.utime", "os.chdir", "os.chroot", "os.mknod", "os.mkfifo", "shutil.rmtree", "shutil.move",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.chown", "shutil.make_archive",
    # native code
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata", "ctypes.call_function",
})


def _install_guard(read_roots):
    """A runtime audit hook: no network, no processes, no writes, no reads outside the Python install.

    It is defence in depth (bwrap already isolates network and filesystem). In the subprocess fallback it is the
    main line of defence, and it cannot be removed from Python code (the native-code modules are refused).
    """
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    # `import subprocess` keeps working (it is common and harmless); starting a process does not: Popen is refused
    # by the "subprocess.Popen" audit event and the low-level fork_exec is replaced before any submission code runs.
    try:
        import subprocess  # noqa: F401
        import _posixsubprocess

        def _denied(*args, **kwargs):
            raise PermissionError("PactPay sandbox: starting processes is not allowed")

        _posixsubprocess.fork_exec = _denied
    except ImportError:
        pass
    roots = tuple(sorted({os.path.normpath(r) for r in read_roots if r}))
    cwd = os.getcwd()

    def allowed_path(path):
        try:
            p = os.path.normpath(os.path.join(cwd, os.fsdecode(path)))
        except Exception:
            return False
        return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
                raise PermissionError("PactPay sandbox: writing files is not allowed")
            if isinstance(flags, int) and flags & write_flags:
                raise PermissionError("PactPay sandbox: writing files is not allowed")
            if isinstance(path, int):
                return
            if not allowed_path(path):
                raise PermissionError("PactPay sandbox: reading %r is not allowed" % (path,))
        elif event in ("os.listdir", "os.scandir"):
            path = args[0] if args else "."
            if path is None or isinstance(path, int) or not allowed_path(path):
                raise PermissionError("PactPay sandbox: listing directories is not allowed")
        elif event == "import":
            name = str(args[0] if args else "")
            if name.split(".")[0] in _BLOCKED_IMPORTS:
                raise ImportError("PactPay sandbox: importing %s is not allowed" % name)
        elif event in _DENIED_EVENTS:
            raise PermissionError("PactPay sandbox: %s is not allowed" % event)

    sys.addaudithook(hook)


class Doc:
    """A non-Python submission as the tests see it: ``text``, ``lines`` and ``sections`` (heading -> body)."""

    def __init__(self, text):
        self.text = text
        self.lines = text.splitlines()
        self.sections = md_sections(text)


class Recorder:
    """A callable the tests can hand to the submission (e.g. as a webhook handler) and inspect afterwards."""

    def __init__(self, return_value):
        self._return = encode(return_value)
        self._calls = []

    def __call__(self, *args, **kwargs):
        self._calls.append({"$dict": [["args", encode(list(args))], ["kwargs", encode(dict(kwargs))]]})
        return decode(self._return)

    def __repr__(self):
        return "<recorder>"

    def encoded_calls(self):
        return list(self._calls)


def _describe(exc):
    try:
        message = str(exc)
    except BaseException:
        message = "<unprintable>"
    if isinstance(exc, SyntaxError):
        message = "line %s: %s" % (exc.lineno, exc.msg)
    return {"type": type(exc).__name__[:80], "msg": str.__str__(message)[:500] if isinstance(message, str) else ""}


def _load_module(source, filename):
    import linecache
    import types

    path = "/submission/" + filename
    code = compile(source, path, "exec", dont_inherit=True)
    module = types.ModuleType("submission")
    module.__file__ = path
    sys.modules["submission"] = module
    linecache.cache[path] = (len(source), None, source.splitlines(True), path)
    exec(code, module.__dict__)
    return module


def _name(req):
    name = req.get("name")
    if not isinstance(name, str) or not _NAME.match(name) or (name.startswith("__") and name.endswith("__")):
        raise AttributeError("attribute name not allowed: %r" % (name,))
    return name


def _handle(req, target, refs, counter):
    op = req.get("op")
    resolve = refs.__getitem__

    def resolve_ref(ref_id):
        try:
            return resolve(ref_id)
        except KeyError:
            raise LookupError("unknown or stale ref %d (the submission process was restarted?)" % ref_id) from None

    if op == "getattr":
        name = _name(req)
        value = getattr(target, name)
        if callable(value):
            return {"$callable": name}
        return encode(value)
    if op == "call":
        name = _name(req)
        fn = getattr(target, name)
        if not callable(fn):
            raise TypeError("%s is not callable" % name)
        args = decode(req.get("args", []), resolve_ref)
        kwargs = decode(req.get("kwargs", {"$dict": []}), resolve_ref)
        if not isinstance(args, list) or not isinstance(kwargs, dict) or not all(isinstance(k, str) for k in kwargs):
            raise TypeError("bad call arguments")
        return encode(fn(*args, **kwargs))
    if op == "new":
        kind = req.get("kind")
        initial = decode(req.get("init"), resolve_ref)
        if kind == "set":
            obj = set(initial or ())
        elif kind == "dict":
            obj = dict(initial or {})
        elif kind == "list":
            obj = list(initial or ())
        elif kind == "recorder":
            obj = Recorder(initial)
        else:
            raise ValueError("unknown container kind %r" % (kind,))
        counter[0] += 1
        refs[counter[0]] = obj
        return {"$ref": counter[0]}
    if op == "value":
        obj = resolve_ref(req.get("ref"))
        if isinstance(obj, Recorder):
            return obj.encoded_calls()
        return encode(obj)
    raise ValueError("unknown op %r" % (op,))


def main(config):
    apply_limits(config["limits"])
    proto_in, proto_out = take_stdio()
    try:
        init = read_msg(proto_in)
    except Exception:
        os._exit(3)
    nonce = init.get("nonce")
    refs = {}
    counter = [int(init.get("ref_base") or 0)]
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", str(init.get("filename") or "submission"))[:120]
    content = init.get("content") or ""
    _install_guard(config.get("read_roots") or [sys.base_prefix, sys.prefix, "/dev/null", "/dev/urandom"])
    target = None
    error = None
    if init.get("mode") == "python":
        try:
            target = _load_module(content, filename)
        except BaseException as exc:  # SystemExit, MemoryError, RecursionError ... at import time
            error = _describe(exc)
    else:
        target = Doc(content)
    write_msg(proto_out, {"t": "ready", "nonce": nonce, "ok": error is None, "error": error})
    while True:
        try:
            req = read_msg(proto_in)
        except BaseException:
            break
        resp = {"t": "resp", "id": req.get("id"), "nonce": req.get("nonce")}
        try:
            resp["value"] = _handle(req, target, refs, counter)
            resp["ok"] = True
        except BaseException as exc:
            resp.pop("value", None)
            resp["ok"] = False
            resp["error"] = _describe(exc)
        try:
            write_msg(proto_out, resp)
        except (TypeError, ValueError) as exc:
            write_msg(proto_out, {"t": "resp", "id": req.get("id"), "nonce": req.get("nonce"), "ok": False,
                                  "error": {"type": "WireError", "msg": "unencodable result: %s" % exc}})
    os._exit(0)
