"""``evaluate()``: execution-backed, fail-closed evaluation of one deliverable against its criteria.

Scoring rules:
  * a ``tests`` criterion is PASS only when every one of its tests passed in the sandbox;
  * a configured reviewer that answered validly and says ``fail`` with at least one verified citation sends a
    test-passing criterion to NEEDS_HUMAN_REVIEW (the AI can hold money back, never release it). This hold also
    applies when prompt injection is suspected, because holding is the fail-closed direction;
  * an ``ai_review`` criterion is PASS only when the reviewer says ``pass`` with a verified citation and no injection
    was detected; FAIL when it says ``fail`` with a verified citation and no injection; otherwise NEEDS_HUMAN_REVIEW;
  * a ``manual`` criterion is always NEEDS_HUMAN_REVIEW;
  * ``harness_tamper_suspected`` (or a scan that could not finish) sends every criterion to NEEDS_HUMAN_REVIEW;
  * only PASS criteria earn their weight. ``score_pct`` is a recommendation: a person releases money.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from . import reviewer as reviewer_mod
from . import sandbox as sandbox_mod
from .reviewer import Reviewer

ENGINE_VERSION = "pactpay-eval/2.0.0"
MAX_SUBMISSION_BYTES = 4 * 1024 * 1024
METHODS = ("tests", "ai_review", "manual")

PASS = "PASS"
FAIL = "FAIL"
REVIEW = "NEEDS_HUMAN_REVIEW"


@dataclass
class _Criterion:
    id: str
    title: str
    description: str
    weight_pct: float
    method: str
    tests_source: str | None
    test_names: list[str]
    tests_error: str | None


def _criteria(raw: list[dict[str, Any]]) -> list[_Criterion]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("criteria must be a non-empty list")
    out: list[_Criterion] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("criterion %d is not an object" % idx)
        cid = str(item.get("id") or "").strip()
        if not cid or len(cid) > 64 or cid in seen:
            raise ValueError("criterion %d needs a unique id of at most 64 characters" % idx)
        seen.add(cid)
        method = item.get("method")
        if method not in METHODS:
            raise ValueError("criterion %s: method must be one of %s" % (cid, ", ".join(METHODS)))
        try:
            weight = float(item.get("weight_pct"))
        except (TypeError, ValueError):
            raise ValueError("criterion %s: weight_pct must be a number" % cid) from None
        if not (0.0 <= weight <= 100.0):
            raise ValueError("criterion %s: weight_pct must be between 0 and 100" % cid)
        source = item.get("tests")
        names: list[str] = []
        error = None
        if method == "tests":
            if not isinstance(source, str) or not source.strip():
                raise ValueError("criterion %s: method 'tests' needs a 'tests' source" % cid)
            try:
                names = sandbox_mod.list_test_names(source)
            except SyntaxError as exc:
                error = "the criterion's test code does not parse (line %s: %s)" % (exc.lineno, exc.msg)
            if not names and error is None:
                error = "the criterion's test code defines no test_* function"
        out.append(_Criterion(cid, str(item.get("title") or cid), str(item.get("description") or ""), weight,
                              method, source if isinstance(source, str) else None, names, error))
    total = sum(c.weight_pct for c in out)
    if abs(total - 100.0) > 0.01:
        raise ValueError("criterion weights must sum to 100 (got %.2f)" % total)
    return out


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def evaluate(criteria: list[dict], filename: str, content: str, *, reviewer: Reviewer | None = None,
             timeout_s: float = 30.0) -> dict:
    """Evaluate ``content`` (named ``filename``) against ``criteria``. Returns a JSON-serialisable report."""
    started = time.monotonic()
    crits = _criteria(criteria)
    if not isinstance(content, str):
        raise TypeError("content must be a str")
    raw = content.encode("utf-8", "surrogatepass")
    lines = content.splitlines()
    is_python = str(filename or "").lower().endswith(".py")
    safe_name = reviewer_mod.safe_label(filename)
    box = sandbox_mod.detect()
    flags: list[str] = []

    def flag(name: str) -> None:
        if name not in flags:
            flags.append(name)

    empty = not content.strip()
    too_large = len(raw) > MAX_SUBMISSION_BYTES
    if empty:
        flag("empty_submission")
    if too_large:
        flag("too_large")

    injection = reviewer_mod.detect_injection(content) if not too_large else []
    if injection:
        flag("prompt_injection_suspected")

    scan: dict[str, Any] | None = None
    if is_python and not too_large and not empty:
        scan = sandbox_mod.run_scan(content, box, timeout=min(sandbox_mod.SCAN_TIMEOUT_S, max(1.0, timeout_s)))
        if not scan.get("ok"):
            flag("scan_incomplete")
        else:
            for name in scan.get("flags") or []:
                flag(str(name))

    suites = [{"criterion_id": c.id, "source": c.tests_source, "tests": c.test_names}
              for c in crits if c.method == "tests" and c.tests_error is None]
    run: sandbox_mod.SuiteRun | None = None
    if suites and not too_large:
        remaining = max(1.0, timeout_s - (time.monotonic() - started))
        run = sandbox_mod.run_tests(box, mode="python" if is_python else "doc", filename=safe_name,
                                    content=content, suites=suites, timeout_s=remaining)
        for name in run.flags:
            flag(name)
        if run.harness_error:
            flag("harness_error")
    if any(c.tests_error for c in crits) or (run is not None and run.suite_errors):
        flag("criterion_tests_error")

    tamper = "harness_tamper_suspected" in flags or "scan_incomplete" in flags

    # ---- deterministic part ----------------------------------------------------------------------------------
    det: dict[str, dict[str, Any]] = {}
    for c in crits:
        tests: list[dict[str, Any]] = []
        status, evidence = REVIEW, ""
        if c.method == "tests":
            if too_large:
                evidence = "The submission exceeds the %d MiB limit and was not executed." % (MAX_SUBMISSION_BYTES >> 20)
            elif c.tests_error:
                evidence = "Cannot be tested: %s." % c.tests_error
            else:
                assert run is not None
                suite_error = run.suite_errors.get(c.id)
                for name in c.test_names:
                    result = run.results.get((c.id, name))
                    if result is None:
                        if suite_error:
                            reason = "not run: the criterion's test code failed to load (%s)" % suite_error
                        elif run.timed_out:
                            reason = "not completed: the evaluation hit its %.0f s time limit" % timeout_s
                        else:
                            reason = "not completed: %s" % (run.harness_error or "the test runner stopped")
                        result = {"name": name, "passed": False, "detail": _clip(reason, 400), "duration_ms": 0.0}
                    tests.append(result)
                passed = sum(1 for t in tests if t["passed"])
                failed = [t for t in tests if not t["passed"]]
                incomplete = any(t["detail"].startswith("not completed") for t in failed)
                if suite_error:
                    status = REVIEW
                    evidence = "The criterion's test code failed to load (%s); a person must check it." % suite_error
                elif incomplete and run.harness_error and not run.timed_out:
                    status = REVIEW
                    evidence = "The test harness failed (%s); a person must check it." % run.harness_error
                elif not failed:
                    status = PASS
                    evidence = "All %d sandbox test(s) passed." % len(tests)
                else:
                    status = FAIL
                    first = failed[0]
                    evidence = "%d of %d sandbox test(s) failed; first: %s - %s" % (
                        len(failed), len(tests), first["name"], first["detail"])
                if tamper and status != REVIEW:
                    status = REVIEW
                    evidence = _tamper_evidence(scan, flags) + " Tests: %d of %d passed." % (passed, len(tests))
        elif c.method == "manual":
            evidence = "Manual criterion: a person must check it."
        else:  # ai_review, settled after the reviewer answers
            evidence = "Needs the AI reviewer's verified evidence or a person."
            if tamper:
                evidence = _tamper_evidence(scan, flags)
        det[c.id] = {"status": status, "tests": tests, "evidence": evidence}

    # ---- the AI reviewer -------------------------------------------------------------------------------------
    review: dict[str, Any] | None = None
    reviewer_report: dict[str, Any] = {"model": None, "status": "not_configured", "detail": "no AI reviewer configured",
                                       "latency_ms": None}
    if reviewer is not None:
        reviewer_report["model"] = str(reviewer.model)
        if empty or too_large:
            reviewer_report.update(status="unavailable",
                                   detail="not called: the submission is %s" % ("empty" if empty else "too large"))
        else:
            summary = [{"id": c.id, "method": c.method, "status": det[c.id]["status"],
                        "tests_passed": sum(1 for t in det[c.id]["tests"] if t["passed"]),
                        "tests_total": len(det[c.id]["tests"]),
                        "failing_tests": [t["name"] for t in det[c.id]["tests"] if not t["passed"]][:5]}
                       for c in crits]
            review = reviewer_mod.run_review(
                reviewer,
                criteria=[{"id": c.id, "title": c.title, "description": c.description, "method": c.method}
                          for c in crits],
                test_summary=summary, flags=list(flags), filename=safe_name, lines=lines)
            reviewer_report.update(status=review["status"], detail=review["detail"], latency_ms=review["latency_ms"])
            if review["meta"].get("truncated"):
                flag("reviewer_input_truncated")
    reviewer_ok = review is not None and review["status"] == "ok"
    advisory = bool(injection)

    # ---- combine ---------------------------------------------------------------------------------------------
    out_criteria: list[dict[str, Any]] = []
    for c in crits:
        d = det[c.id]
        status, evidence = d["status"], d["evidence"]
        ai_view = None
        verdict = review["verdicts"].get(c.id) if reviewer_ok and review else None
        if verdict is not None:
            verified = [x for x in verdict["citations"] if x["verified"]]
            valid = bool(verified)
            accepted = valid and not advisory
            where = ", ".join("lines %s-%s" % (x["line_start"], x["line_end"]) for x in verified[:3])
            if not valid:
                reason = "no verified citation: recorded, not acted on"
            elif advisory:
                reason = "prompt injection suspected: the AI verdict is advisory only"
            else:
                reason = "verified citation (%s)" % where
            if c.method == "tests":
                if status == PASS and verdict["verdict"] == "fail" and valid:
                    status = REVIEW
                    evidence = ("All %d sandbox test(s) passed, but the AI reviewer reported a verified problem at %s; "
                                "held for a person." % (len(d["tests"]), where))
                    reason += "; holds this criterion for human review (the AI can hold money back, never release it)"
                elif verdict["verdict"] == "pass" and status != PASS:
                    reason += "; the tests decide: an AI pass cannot override them"
            elif c.method == "ai_review" and not tamper:
                if accepted and verdict["verdict"] == "pass":
                    status = PASS
                    evidence = "The AI reviewer found the criterion met, citing %s (quotes verified)." % where
                elif accepted and verdict["verdict"] == "fail":
                    status = FAIL
                    evidence = "The AI reviewer found the criterion not met, citing %s (quotes verified)." % where
                elif advisory:
                    evidence = "Prompt injection is suspected, so the AI verdict is advisory; a person must check it."
                else:
                    evidence = "The AI reviewer gave no usable verified verdict ('%s'); a person must check it." % (
                        verdict["verdict"])
            elif c.method == "manual":
                reason += "; manual criteria are always checked by a person"
            ai_view = {"verdict": verdict["verdict"], "explanation": verdict["explanation"],
                       "citations": verdict["citations"], "accepted": accepted, "reason": reason}
        elif c.method == "ai_review" and not tamper:
            if reviewer is None:
                evidence = "No AI reviewer is configured; a person must check it."
            elif review is not None and review["status"] != "ok":
                evidence = "The AI reviewer was %s (%s); a person must check it." % (
                    review["status"].replace("_", " "), review["detail"])
            else:
                evidence = "The AI reviewer gave no verdict for this criterion; a person must check it."
        tests = d["tests"]
        earned = round(c.weight_pct, 2) if status == PASS else 0.0
        out_criteria.append({
            "id": c.id,
            "title": c.title,
            "weight_pct": round(c.weight_pct, 2),
            "method": c.method,
            "status": status,
            "tests": tests,
            "tests_passed": sum(1 for t in tests if t["passed"]),
            "tests_total": len(tests),
            "ai": ai_view,
            "earned_pct": earned,
            "evidence": _clip(evidence, 400),
        })

    report: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "submission_sha256": hashlib.sha256(raw).hexdigest(),
        "filename": safe_name,
        "line_count": len(lines),
        "size_bytes": len(raw),
        "sandbox": box.describe(),
        "criteria": out_criteria,
        "score_pct": round(sum(c["earned_pct"] for c in out_criteria if c["status"] == PASS), 2),
        "flags": flags,
        "reviewer": reviewer_report,
        "static_findings": (scan or {}).get("findings", [])[:30] if scan and scan.get("ok") else [],
        "injection_evidence": injection[:10],
        "run": {
            "test_seed": run.seed if run else None,
            "rpc_calls": run.rpc_count if run else 0,
            "worker_starts": run.worker_spawns if run else 0,
            "rejected_lines": run.rejected_lines if run else 0,
            "import_error": run.import_error if run else None,
            "harness_error": run.harness_error if run else None,
            "scan_error": scan.get("error") if scan and not scan.get("ok") else None,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        },
        "release_policy": "recommendation only: a person approves every capture or payout",
    }
    return report


def _tamper_evidence(scan: dict[str, Any] | None, flags: list[str]) -> str:
    if "scan_incomplete" in flags:
        return "Held for a person: the static scan of the submission could not finish (%s)." % (
            (scan or {}).get("error") or "unknown error")
    finding = next((f for f in (scan or {}).get("findings", []) if f.get("flag") == "harness_tamper_suspected"), None)
    if finding:
        return "Held for a person: the static scan found harness-tampering code (%s, line %s)." % (
            finding.get("what"), finding.get("line"))
    return "Held for a person: harness tampering is suspected."
