"""How complete are AI-drafted policies? Draft one agreement N times with a real model through PactPay's draft()
(grounding checks, completeness check, one validator-in-the-loop repair) and record what came out.

    python scripts/eval_drafter_completeness.py --runs 6 [--chat scripts/video/custom_agreement.txt]
Writes evidence/ai-eval/drafter-completeness-<model>-<UTC>.json and prints a summary.
"""
import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import terms as T  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm import completer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--chat", default=str(ROOT / "scripts" / "video" / "custom_agreement.txt"))
    a = ap.parse_args()
    s = get_settings()
    comp, label = completer(s)
    if comp is None:
        print("configure OPENAI_* or GEMINI_API_KEY", file=sys.stderr)
        return 2
    chat = Path(a.chat).read_text()
    listed = [ln for ln in chat.splitlines() if re.match(r"^\s*(\d+[.)]|[-*•])\s+\S", ln)]
    calls = {"n": 0}

    def counted(system, user):
        calls["n"] += 1
        return comp(system, user)

    runs = []
    for i in range(a.runs):
        calls["n"] = 0
        t0 = time.time()
        out, by, notes = T.draft(chat, amount_cents=None, title=None, preset_id=None, settings=s, complete=counted,
                                 model_label=label)
        crit = out["criteria"]
        missing = T.missing_requirements(chat, crit)
        try:
            T.validate(out)
            valid = True
        except T.TermsError:
            valid = False
        runs.append({"run": i, "seconds": round(time.time() - t0, 1), "model_calls": calls["n"],
                     "criteria": [{"id": c["id"], "method": c["method"], "quote": c.get("source_quote")} for c in crit],
                     "listed_requirements_covered": len(listed) - len(missing), "listed_requirements": len(listed),
                     "executable_tests": sum(1 for c in crit if c["method"] == "tests"),
                     "amount_cents": out["amount_cents"], "payees": [(p["paypal_email"], p["share_pct"]) for p in out["payees"]],
                     "valid_terms": valid, "notes": notes})
        r = runs[-1]
        print(f"run {i}: covered {r['listed_requirements_covered']}/{len(listed)} listed, tests {r['executable_tests']}, "
              f"calls {r['model_calls']}, valid {valid}, {r['seconds']}s")
    summary = {"model": label, "runs": a.runs,
               "all_listed_covered": sum(r["listed_requirements_covered"] == r["listed_requirements"] for r in runs),
               "repair_rounds_used": sum(r["model_calls"] > 1 for r in runs),
               "valid_terms": sum(r["valid_terms"] for r in runs),
               "omissions_reported_when_present": all(
                   r["listed_requirements_covered"] == r["listed_requirements"] or any("no criterion" in n for n in r["notes"])
                   for r in runs)}
    print(json.dumps(summary, indent=1))
    at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = ROOT / "evidence" / "ai-eval" / f"drafter-completeness-{re.sub(r'[^a-z0-9.]+', '-', label.lower())}-{at}.json"
    dest.write_text(json.dumps({"at": at, "chat": chat, "summary": summary, "runs": runs}, indent=1))
    print("wrote", dest.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
