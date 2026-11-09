"""Wire format shared by PactPay's sandboxed evaluation processes (standard library only).

This source is inlined into the sandboxed worker, runner and scanner processes (``python -I -B -c <code>``), so it
must import nothing outside the standard library and must not use ``from __future__`` imports.

Values cross process boundaries as tagged JSON, never pickle:

* ``None``, ``bool``, ``int``, ``str`` and finite ``float`` travel as JSON;
* ``{"$float": "nan"|"inf"|"-inf"}``, ``{"$bytes": <base64>}``, ``{"$tuple": [...]}``, ``{"$set": [...]}``,
  ``{"$frozenset": [...]}``, ``{"$dict": [[key, value], ...]}`` (any hashable key), ``{"$ref": <id>}`` (a container
  that lives in the worker), ``{"$callable": <name>}`` (an attribute of the submission that can be called);
* anything else becomes ``{"$repr": <repr, truncated>, "$type": <type name>}`` - data only, no behaviour.

Decoding only ever builds plain built-in values, :class:`Ref`, :class:`RemoteObject` and :class:`RemoteCallable`, so
an object with a rigged ``__eq__`` (or any other behaviour) never reaches the process that does the assertions.
"""

import base64
import json
import math
import os
import re
import sys

MAX_DEPTH = 48
MAX_NODES = 250_000
MAX_REPR = 600
MAX_LINE = 48 * 1024 * 1024


class WireError(ValueError):
    """A value or a message that does not fit the wire format."""


class Ref:
    """A handle to a mutable container that lives in the worker process (see ``sub._new`` / ``sub._value``)."""

    __slots__ = ("id",)

    def __init__(self, ref_id):
        self.id = int(ref_id)

    def __repr__(self):
        return "<remote ref %d>" % self.id

    def __eq__(self, other):
        return isinstance(other, Ref) and other.id == self.id

    def __hash__(self):
        return hash(("pactpay-ref", self.id))


class RemoteObject:
    """A value the submission produced that has no data encoding: only its type name and repr text travel."""

    __slots__ = ("type_name", "text")

    def __init__(self, type_name, text):
        self.type_name = str(type_name)[:80]
        self.text = str(text)[:MAX_REPR]

    def __repr__(self):
        return "<remote %s: %s>" % (self.type_name, self.text)


class RemoteCallable:
    """Marker for a callable attribute of the submission (the runner wraps it in an RPC function)."""

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = str(name)

    def __repr__(self):
        return "<remote callable %s>" % self.name


def _safe_repr(obj):
    try:
        text = repr(obj)
    except BaseException as exc:  # a rigged __repr__ is the submission's problem, not the wire's
        text = "<unrepresentable %s: %s>" % (type(obj).__name__, type(exc).__name__)
    if not isinstance(text, str):
        text = "<unrepresentable %s>" % type(obj).__name__
    return str.__str__(text)[:MAX_REPR]


def encode(obj):
    """Python value -> JSON-able tagged structure."""
    return _enc(obj, 0, [MAX_NODES])


def _enc(obj, depth, budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise WireError("value has too many elements")
    if depth > MAX_DEPTH:
        raise WireError("value is nested too deeply")
    if obj is None or obj is True or obj is False:
        return obj
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, int):
        value = int.__int__(obj)
        if value.bit_length() > 4000:
            raise WireError("integer too large")
        return value
    if isinstance(obj, float):
        value = float.__float__(obj)
        if math.isfinite(value):
            return value
        return {"$float": "nan" if value != value else ("inf" if value > 0 else "-inf")}
    if isinstance(obj, str):
        return str.__str__(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"$bytes": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, Ref):
        return {"$ref": obj.id}
    if isinstance(obj, RemoteCallable):
        return {"$callable": obj.name}
    if isinstance(obj, tuple):
        return {"$tuple": [_enc(x, depth + 1, budget) for x in tuple.__iter__(obj)]}
    if isinstance(obj, list):
        return [_enc(x, depth + 1, budget) for x in list.__iter__(obj)]
    if isinstance(obj, frozenset):
        return {"$frozenset": [_enc(x, depth + 1, budget) for x in frozenset.__iter__(obj)]}
    if isinstance(obj, set):
        return {"$set": [_enc(x, depth + 1, budget) for x in set.__iter__(obj)]}
    if isinstance(obj, dict):
        return {"$dict": [[_enc(k, depth + 1, budget), _enc(v, depth + 1, budget)] for k, v in dict.items(obj)]}
    return {"$repr": _safe_repr(obj), "$type": type(obj).__name__[:80]}


def decode(data, resolve_ref=None):
    """Tagged structure -> Python value. ``resolve_ref`` maps a ref id to a local object (worker side)."""
    return _dec(data, 0, [MAX_NODES], resolve_ref)


def _dec(data, depth, budget, resolve_ref):
    budget[0] -= 1
    if budget[0] < 0:
        raise WireError("value has too many elements")
    if depth > MAX_DEPTH:
        raise WireError("value is nested too deeply")
    if data is None or isinstance(data, (bool, int, str)):
        return data
    if isinstance(data, float):
        return data
    if isinstance(data, list):
        return [_dec(x, depth + 1, budget, resolve_ref) for x in data]
    if not isinstance(data, dict) or not data:
        raise WireError("unexpected JSON value")
    keys = set(data)
    try:
        if keys == {"$bytes"}:
            return base64.b64decode(data["$bytes"], validate=True)
        if keys == {"$float"}:
            if data["$float"] not in ("nan", "inf", "-inf"):
                raise WireError("bad float tag")
            return float(data["$float"])
        if keys == {"$tuple"}:
            return tuple(_dec(x, depth + 1, budget, resolve_ref) for x in _as_list(data["$tuple"]))
        if keys == {"$set"}:
            return set(_dec(x, depth + 1, budget, resolve_ref) for x in _as_list(data["$set"]))
        if keys == {"$frozenset"}:
            return frozenset(_dec(x, depth + 1, budget, resolve_ref) for x in _as_list(data["$frozenset"]))
        if keys == {"$dict"}:
            out = {}
            for pair in _as_list(data["$dict"]):
                if not isinstance(pair, list) or len(pair) != 2:
                    raise WireError("bad dict pair")
                out[_dec(pair[0], depth + 1, budget, resolve_ref)] = _dec(pair[1], depth + 1, budget, resolve_ref)
            return out
        if keys == {"$ref"}:
            ref_id = data["$ref"]
            if not isinstance(ref_id, int) or isinstance(ref_id, bool):
                raise WireError("bad ref")
            return resolve_ref(ref_id) if resolve_ref is not None else Ref(ref_id)
        if keys == {"$callable"}:
            return RemoteCallable(_as_str(data["$callable"]))
        if keys == {"$repr", "$type"}:
            return RemoteObject(_as_str(data["$type"]), _as_str(data["$repr"]))
    except TypeError as exc:  # an unhashable set member or dict key
        raise WireError("bad value: %s" % exc) from None
    raise WireError("unknown tagged value")


def _as_list(value):
    if not isinstance(value, list):
        raise WireError("expected a list")
    return value


def _as_str(value):
    if not isinstance(value, str):
        raise WireError("expected a string")
    return value


def dumps_line(msg):
    """One protocol message as one ASCII line (newlines and non-ASCII are escaped, NaN is refused)."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"


def write_msg(stream, msg):
    stream.write(dumps_line(msg))
    stream.flush()


def read_msg(stream, limit=MAX_LINE):
    line = stream.readline(limit + 1)
    if not line:
        raise EOFError("stream closed")
    if len(line) > limit or not line.endswith(b"\n"):
        raise WireError("line too long or truncated")
    return json.loads(line)


_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*(?:#+[ \t]*)?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def md_sections(text):
    """Markdown heading text -> the body below it, up to the next heading of any level.

    Headings inside fenced code blocks are ignored; a repeated heading gets a " (2)", " (3)" ... suffix.
    """
    sections = {}
    current = None
    body = []
    fence = None

    def flush():
        if current is None:
            return
        key = current
        n = 2
        while key in sections:
            key = "%s (%d)" % (current, n)
            n += 1
        sections[key] = "\n".join(body).strip()

    for line in str(text).splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
        elif fence is None:
            heading = _HEADING.match(line)
            if heading and heading.group(2).strip():
                flush()
                current = heading.group(2).strip()
                body = []
                continue
        if current is not None:
            body.append(line)
    flush()
    return sections


def apply_limits(limits):
    """Lower this process's resource limits (soft and hard, so the sandboxed code cannot raise them again)."""
    try:
        import resource
    except ImportError:  # not POSIX: nothing to apply
        return
    wanted = [
        (resource.RLIMIT_CPU, int(limits["cpu_s"]), int(limits["cpu_s"]) + 1),
        (resource.RLIMIT_AS, int(limits["as_bytes"]), int(limits["as_bytes"])),
        (resource.RLIMIT_FSIZE, int(limits["fsize_bytes"]), int(limits["fsize_bytes"])),
        (resource.RLIMIT_NOFILE, int(limits["nofile"]), int(limits["nofile"])),
        (resource.RLIMIT_CORE, 0, 0),
    ]
    for res, soft, hard in wanted:
        try:
            _, cur_hard = resource.getrlimit(res)
            if cur_hard != resource.RLIM_INFINITY:
                hard = min(hard, cur_hard)
                soft = min(soft, hard)
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):
            pass


def take_stdio():
    """Keep private copies of stdin/stdout for the protocol; fd 0 becomes /dev/null and fd 1 goes to stderr.

    Whatever the sandboxed code prints therefore lands on stderr, which the parent drains and ignores.
    """
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "wb")
    null_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(null_fd, 0)
    os.dup2(2, 1)
    os.close(null_fd)
    try:
        sys.stdout.flush()
    except Exception:
        pass
    return proto_in, proto_out
