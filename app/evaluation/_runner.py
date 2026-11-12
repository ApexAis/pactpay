"""The trusted side of a PactPay evaluation: runs the agreed tests and signs each verdict (stdlib only).

Inlined after ``_proto.py`` into ``python -I -B -c`` and run in its own sandbox. It receives the HMAC key, the test
sources and the random seed on stdin from the parent, never from the worker. The ``sub`` object the tests receive is
a proxy: every attribute read and call becomes an RPC that the parent relays to the worker, and every answer is
decoded into plain data before a test sees it.
"""

try:  # in-process import (unit tests); when inlined, the _proto names are already defined
    from ._proto import *  # noqa: F401,F403
    from ._proto import Ref, RemoteCallable, RemoteObject, apply_limits, decode, encode, md_sections, read_msg
    from ._proto import take_stdio, write_msg
except ImportError:
    pass

import hashlib
import hmac
import json
import linecache
import random
import time
import traceback


class RemoteError(Exception):
    """The submission raised (or could not be reached) while answering a call."""

    def __init__(self, type_name, message):
        super().__init__("%s: %s" % (type_name, message) if message else type_name)
        self.type_name = type_name
        self.remote_message = message


class RemoteTimeout(RemoteError):
    """The submission did not answer a call within the per-call time limit."""


class _Channel:
    def __init__(self, proto_in, proto_out):
        self.proto_in = proto_in
        self.proto_out = proto_out
        self.next_id = 0

    def rpc(self, op, **fields):
        self.next_id += 1
        msg = {"t": "rpc", "id": self.next_id, "op": op}
        msg.update(fields)
        write_msg(self.proto_out, msg)
        resp = read_msg(self.proto_in)
        if not isinstance(resp, dict) or resp.get("t") != "resp" or resp.get("id") != self.next_id:
            raise RuntimeError("YAVQEN runner: protocol error on RPC %d" % self.next_id)
        if resp.get("ok") is True:
            return decode(resp.get("value"))
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        type_name = str(err.get("type") or "Error")[:80]
        cls = RemoteTimeout if type_name == "Timeout" else RemoteError
        raise cls(type_name, str(err.get("msg") or "")[:500])


class RemoteFunction:
    def __init__(self, channel, name):
        self._channel = channel
        self._name = name

    def __call__(self, *args, **kwargs):
        return self._channel.rpc("call", name=self._name, args=encode(list(args)), kwargs=encode(dict(kwargs)))

    def __repr__(self):
        return "<submission function %s>" % self._name


class Submission:
    """``sub`` in the tests. ``sub.name`` reads an attribute (callables come back as RPC functions);
    ``sub._new("set"|"dict"|"list"|"recorder", initial)`` makes a container in the worker that can be passed by
    reference; ``sub._value(ref)`` reads it back; ``sub._attr(name)`` reads a name that starts with ``_``."""

    def __init__(self, channel):
        object.__setattr__(self, "_channel", channel)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._attr(name)

    def __setattr__(self, name, value):
        raise AttributeError("the submission proxy is read-only")

    def _attr(self, name):
        try:
            value = self._channel.rpc("getattr", name=name)
        except RemoteError as exc:
            if exc.type_name == "AttributeError":
                raise AttributeError("the submission has no attribute %r" % name) from None
            raise
        if isinstance(value, RemoteCallable):
            return RemoteFunction(self._channel, name)
        return value

    def _call(self, name, *args, **kwargs):
        return RemoteFunction(self._channel, name)(*args, **kwargs)

    def _new(self, kind, initial=None):
        return self._channel.rpc("new", kind=kind, init=encode(initial))

    def _value(self, ref):
        if not isinstance(ref, Ref):
            raise TypeError("_value() takes a ref returned by _new()")
        return self._channel.rpc("value", ref=ref.id)

    def _has(self, name):
        try:
            self._attr(name)
        except AttributeError:
            return False
        return True

    def __repr__(self):
        return "<submission proxy>"


class _Verdicts:
    """Every line on the verdict channel carries a sequence number and an HMAC-SHA256 over it."""

    def __init__(self, proto_out, key):
        self.proto_out = proto_out
        self.key = key
        self.seq = 0

    def send(self, kind, body):
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        mac = hmac.new(self.key, ("%d|%s|%s" % (self.seq, kind, text)).encode("ascii"), hashlib.sha256).hexdigest()
        write_msg(self.proto_out, {"t": kind, "seq": self.seq, "body": text, "mac": mac})
        self.seq += 1


def _clip(text, limit=360):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _failure_detail(exc, test_file):
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == test_file]
    where = ""
    if frames:
        frame = frames[-1]
        where = " (line %d: %s)" % (frame.lineno, (frame.line or "").strip())
    if isinstance(exc, RemoteTimeout):
        return _clip("the submission did not answer in time: %s%s" % (exc.remote_message or exc.type_name, where))
    if isinstance(exc, RemoteError):
        return _clip("the submission raised %s%s" % (exc, where))
    if isinstance(exc, AssertionError):
        message = str(exc)
        return _clip(("assertion failed: %s" % message if message else "assertion failed") + where)
    return _clip("%s: %s%s" % (type(exc).__name__, exc, where))


def main(config):
    apply_limits(config["limits"])
    proto_in, proto_out = take_stdio()
    init = read_msg(proto_in)
    verdicts = _Verdicts(proto_out, bytes.fromhex(init["key"]))
    random.seed(int(init["seed"]))
    sub = Submission(_Channel(proto_in, proto_out))
    count = 0
    for suite in init["suites"]:
        cid = str(suite["criterion_id"])
        test_file = "<tests %s>" % cid
        source = str(suite["source"])
        linecache.cache[test_file] = (len(source), None, source.splitlines(True), test_file)
        namespace = {
            "__name__": "pactpay_tests_%s" % cid,
            "Ref": Ref,
            "RemoteError": RemoteError,
            "RemoteTimeout": RemoteTimeout,
            "RemoteObject": RemoteObject,
            "md_sections": md_sections,
        }
        try:
            exec(compile(source, test_file, "exec", dont_inherit=True), namespace)
        except BaseException as exc:
            verdicts.send("suite_error", {"criterion_id": cid, "detail": _clip("%s: %s" % (type(exc).__name__, exc))})
            continue
        for name in suite["tests"]:
            fn = namespace.get(name)
            started = time.perf_counter()
            if not callable(fn):
                passed, detail = False, "test %s is not defined" % name
            else:
                try:
                    fn(sub)
                    passed, detail = True, "passed"
                except Exception as exc:
                    passed, detail = False, _failure_detail(exc, test_file)
            duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
            verdicts.send("result", {"criterion_id": cid, "name": name, "passed": passed, "detail": detail,
                                     "duration_ms": duration_ms})
            count += 1
    verdicts.send("done", {"count": count})
