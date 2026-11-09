"""Static AST pre-scan of a Python submission (standard library only).

``scan_python(source)`` never executes the submission. PactPay runs it in a sandboxed, memory-limited process
(parsing a few MB of hostile Python can take gigabytes), but it is a pure function and can be called in-process.

Flags:
  * ``harness_tamper_suspected`` - code that reaches for the interpreter or the process itself (``sys.modules``,
    ``os._exit``, ``sys.exit``, ``builtins``, ``gc``, ``inspect``, ``ctypes``, ``sys._getframe``, ``signal``,
    ``atexit``, ``/proc``, dynamic code via ``exec``/``eval``/``compile``/``__import__``/``importlib`` ...). The
    engine sends every criterion to human review when it is set.
  * ``policy:network``, ``policy:subprocess``, ``policy:filesystem_write`` - informational; the sandbox blocks them.
  * ``syntax_error`` - the source does not parse.
"""

import ast

TAMPER = "harness_tamper_suspected"
NETWORK = "policy:network"
SUBPROCESS = "policy:subprocess"
FS_WRITE = "policy:filesystem_write"

_TAMPER_MODULES = {"builtins", "gc", "inspect", "ctypes", "_ctypes", "signal", "atexit", "importlib", "faulthandler",
                   "cffi", "_thread", "marshal", "pickle", "dill"}
_NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ftplib", "smtplib",
                    "poplib", "imaplib", "telnetlib", "xmlrpc", "websocket", "websockets", "socketserver", "paramiko",
                    "pycurl", "grpc", "asyncssh", "nntplib"}
_SUBPROCESS_MODULES = {"subprocess", "pty", "multiprocessing", "_posixsubprocess", "concurrent.futures.process"}
_FS_MODULES = {"shutil", "tempfile"}

_TAMPER_ATTRS = {("sys", "modules"), ("os", "_exit"), ("sys", "exit"), ("sys", "_getframe"), ("sys", "settrace"),
                 ("sys", "setprofile"), ("sys", "addaudithook"), ("sys", "_current_frames"), ("os", "abort"),
                 ("os", "kill"), ("os", "killpg"), ("sys", "breakpointhook"), ("sys", "excepthook"),
                 ("sys", "meta_path"), ("sys", "path_hooks")}
_SUBPROCESS_ATTRS = {("os", n) for n in ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
                                          "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                                          "spawnvpe", "execl", "execle", "execlp", "execlpe", "execv", "execve",
                                          "execvp", "execvpe", "startfile")}
_NETWORK_ATTRS = {("asyncio", "open_connection"), ("asyncio", "start_server"), ("asyncio", "open_unix_connection")}
_FS_ATTRS = {("os", n) for n in ("remove", "unlink", "rename", "renames", "replace", "mkdir", "makedirs", "rmdir",
                                 "removedirs", "truncate", "ftruncate", "chmod", "chown", "lchown", "link", "symlink",
                                 "utime", "mkfifo", "mknod", "write", "open")}
_FS_METHODS = {"write_text", "write_bytes", "touch", "unlink", "rmdir", "symlink_to", "hardlink_to", "rmtree"}
_TAMPER_NAMES = {"__builtins__", "__import__", "exec", "eval", "compile", "breakpoint", "__loader__", "__spec__"}
_TAMPER_CALLS = {"exit", "quit"}
_TAMPER_STRINGS = {"_exit", "_getframe", "__builtins__", "__import__", "__subclasses__", "__globals__", "__code__",
                   "f_back", "f_globals", "gi_frame", "tb_frame", "__closure__"}
_TAMPER_DUNDER_ATTRS = {"__subclasses__", "__globals__", "__code__", "__closure__", "__builtins__", "f_back",
                        "f_globals", "f_locals", "gi_frame", "tb_frame", "__base__", "__bases__", "__mro__"}


def _root(module_name):
    return str(module_name or "").split(".")[0]


def scan_python(source):
    """-> {"syntax_ok": bool, "error": str|None, "flags": [..], "findings": [{"flag","line","what"}, ...]}"""
    try:
        tree = ast.parse(source, filename="submission.py")
    except SyntaxError as exc:
        return {"syntax_ok": False, "error": "line %s: %s" % (exc.lineno, exc.msg), "flags": ["syntax_error"],
                "findings": [{"flag": "syntax_error", "line": exc.lineno or 0, "what": str(exc.msg)[:200]}]}
    except (ValueError, RecursionError, MemoryError) as exc:
        return {"syntax_ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "flags": ["syntax_error"], "findings": [{"flag": "syntax_error", "line": 0, "what": type(exc).__name__}]}

    findings = []

    def hit(flag, node, what):
        if len(findings) < 200:
            findings.append({"flag": flag, "line": getattr(node, "lineno", 0) or 0, "what": what[:200]})

    def module_flag(name, node):
        root = _root(name)
        full = str(name or "")
        if root in _TAMPER_MODULES or full in _TAMPER_MODULES:
            hit(TAMPER, node, "imports %s" % full)
        if root in _NETWORK_MODULES:
            hit(NETWORK, node, "imports %s" % full)
        if root in _SUBPROCESS_MODULES or full in _SUBPROCESS_MODULES:
            hit(SUBPROCESS, node, "imports %s" % full)
        if root in _FS_MODULES:
            hit(FS_WRITE, node, "imports %s" % full)

    aliases = {}  # local name -> module path ("o" -> "os")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_flag(alias.name, node)
                aliases[alias.asname or _root(alias.name)] = alias.name if alias.asname else _root(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            module_flag(base, node)
            for alias in node.names:
                pair = (_root(base), alias.name)
                full = "%s.%s" % (base, alias.name) if base else alias.name
                if alias.name == "*" and base in ("os", "sys"):
                    hit(TAMPER, node, "from %s import *" % base)
                if pair in _TAMPER_ATTRS:
                    hit(TAMPER, node, "imports %s" % full)
                if pair in _SUBPROCESS_ATTRS:
                    hit(SUBPROCESS, node, "imports %s" % full)
                if pair in _FS_ATTRS:
                    hit(FS_WRITE, node, "imports %s" % full)
                if pair in _NETWORK_ATTRS:
                    hit(NETWORK, node, "imports %s" % full)
                module_flag(full, node)
                aliases[alias.asname or alias.name] = full

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                base = _root(aliases.get(node.value.id, node.value.id))
                pair = (base, node.attr)
                dotted = "%s.%s" % (base, node.attr)
                if pair in _TAMPER_ATTRS:
                    hit(TAMPER, node, "uses %s" % dotted)
                if pair in _SUBPROCESS_ATTRS:
                    hit(SUBPROCESS, node, "uses %s" % dotted)
                if pair in _FS_ATTRS:
                    hit(FS_WRITE, node, "uses %s" % dotted)
                if pair in _NETWORK_ATTRS:
                    hit(NETWORK, node, "uses %s" % dotted)
                if base in ("shutil",):
                    hit(FS_WRITE, node, "uses %s" % dotted)
            if node.attr in _TAMPER_DUNDER_ATTRS:
                hit(TAMPER, node, "reaches %s" % node.attr)
            if node.attr in _FS_METHODS:
                hit(FS_WRITE, node, "calls .%s()" % node.attr)
        elif isinstance(node, ast.Name):
            if node.id in _TAMPER_NAMES:
                hit(TAMPER, node, "uses %s" % node.id)
        elif isinstance(node, ast.Call):
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else None
            if fname in _TAMPER_CALLS:
                hit(TAMPER, node, "calls %s()" % fname)
            if fname in ("getattr", "setattr", "delattr", "hasattr") and len(node.args) >= 2:
                obj, target = node.args[0], node.args[1]
                if (not isinstance(target, ast.Constant) and isinstance(obj, ast.Name)
                        and (obj.id in aliases or obj.id in ("os", "sys", "builtins"))):
                    hit(TAMPER, node, "%s() on module %s with a computed attribute name" % (fname, obj.id))
            is_attr_open = isinstance(func, ast.Attribute) and func.attr == "open"
            if fname == "open" or is_attr_open:
                mode = node.args[1] if len(node.args) >= 2 else None
                if is_attr_open and isinstance(func.value, ast.Call):  # Path(...).open(mode)
                    mode = node.args[0] if node.args else None
                for kw in node.keywords:
                    if kw.arg == "mode":
                        mode = kw.value
                if mode is not None:
                    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                        if any(ch in mode.value for ch in "wax+"):
                            hit(FS_WRITE, node, "open(..., %r)" % mode.value)
                    elif fname == "open":
                        hit(FS_WRITE, node, "open() with a computed mode")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
            if "/proc" in text:
                hit(TAMPER, node, "mentions /proc")
            stripped = text.strip()
            if stripped in _TAMPER_STRINGS:
                hit(TAMPER, node, "string %r" % stripped[:40])

    flags = []
    for finding in findings:
        if finding["flag"] not in flags:
            flags.append(finding["flag"])
    return {"syntax_ok": True, "error": None, "flags": flags, "findings": findings}


def main(config):
    """Sandboxed entry point: one JSON line {"source": ...} on stdin -> one JSON line with the scan on stdout."""
    apply_limits(config["limits"])  # noqa: F821 - defined by the inlined _proto source
    proto_in, proto_out = take_stdio()  # noqa: F821
    request = read_msg(proto_in)  # noqa: F821
    try:
        result = scan_python(str(request.get("source") or ""))
        result["ok"] = True
    except BaseException as exc:
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    write_msg(proto_out, result)  # noqa: F821

