"""How reliable are AI-drafted acceptance tests? Draft the demo chat's terms N times with a real model, then run each
draft's tests (in the evaluation sandbox) against the correct deliverable, the flawed draft and two fakes.

A draft is *sound* when its terms validate, it accepts every criterion of the correct deliverable, rejects the
flawed draft's missing criterion, and earns the marker-only fake and the hard-coded fake nothing. Writes
evidence/ai-eval/drafter-<model>-<UTC time>.json and .md. Needs an OpenAI-compatible endpoint (e.g. Ollama).

    python scripts/eval_ai_drafter.py --base-url http://localhost:11434/v1 --model gpt-oss:20b --runs 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import terms as T  # noqa: E402
from app.config import Settings  # noqa: E402
from app.evaluation import evaluate  # noqa: E402
from app.llm import completer  # noqa: E402
from app.presets import get_preset  # noqa: E402

MARKER_FAKE = (ROOT / "tests" / "fixtures" / "adversarial" / "marker_only_fake.py")
HARDCODED_FAKE = (ROOT / "tests" / "fixtures" / "adversarial" / "hardcoded_answers.py")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--model", default="gpt-oss:20b")
    ap.add_argument("--api-key", default="ollama")
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    settings = Settings(openai_api_key=args.api_key, openai_base_url=args.base_url, ai_model=args.model)
    complete, label = completer(settings, timeout_s=900)
    preset = get_preset("paypal_webhook_sprint")
    subjects = {"correct": (preset["fixed_submission"]["filename"], preset["fixed_submission"]["content"]),
                "flawed (no idempotency handler)": (preset["flawed_submission"]["filename"],
                                                    preset["flawed_submission"]["content"])}
    for name, path in (("marker-only fake", MARKER_FAKE), ("hard-coded fake", HARDCODED_FAKE)):
        if path.exists():
            subjects[name] = (path.name, path.read_text())
    runs = []
    for i in range(args.runs):
        t0 = time.time()
        drafted, by, notes = T.draft(preset["chat_text"], amount_cents=None, title=None, preset_id=None,
                                     settings=settings, complete=complete, model_label=label)
        seconds = round(time.time() - t0, 1)
        try:
            T.validate(drafted)
            problems = []
        except T.TermsError as exc:
            problems = exc.problems
        results = {}
        if not problems:
            for name, (fname, content) in subjects.items():
                r = evaluate(drafted["criteria"], fname, content)
                results[name] = {"score_pct": r["score_pct"],
                                 "criteria": {c["id"]: c["status"] for c in r["criteria"]},
                                 "failed_tests": [f"{c['id']}:{t['name']}: {str(t.get('detail'))[:160]}"
                                                  for c in r["criteria"] for t in c.get("tests") or []
                                                  if not t.get("passed")][:6]}
        tested = [c["id"] for c in drafted["criteria"] if c.get("method") == "tests"]
        correct = results.get("correct", {}).get("criteria", {})
        sound = bool(results) and bool(tested) and all(correct.get(c) == "PASS" for c in tested) and all(
            results[n]["score_pct"] == 0 for n in results if "fake" in n)
        flawed = results.get("flawed (no idempotency handler)", {}).get("score_pct")
        runs.append({"run": i + 1, "seconds": seconds, "drafted_by": by, "notes": notes, "problems": problems,
                     "criteria": [{"id": c["id"], "method": c["method"], "weight_pct": c["weight_pct"],
                                   "description": c.get("description"), "tests": c.get("tests")}
                                  for c in drafted["criteria"]],
                     "payees": drafted["payees"], "amount_cents": drafted["amount_cents"],
                     "results": results, "sound": sound, "flawed_score_pct": flawed})
        print(f"run {i + 1}: {seconds}s valid={not problems} sound={sound} "
              + " ".join(f"{n}={v['score_pct']}" for n, v in results.items()), flush=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "evidence" / "ai-eval" / f"drafter-{args.model.replace(':', '_')}-{stamp}"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {"model": label, "runs": args.runs, "valid_terms": sum(not r["problems"] for r in runs),
               "sound_drafts": sum(r["sound"] for r in runs), "at": stamp}
    out.with_suffix(".json").write_text(json.dumps({"summary": summary, "runs": runs}, indent=1) + "\n")
    lines = [f"# AI-drafted acceptance tests: {label}", "",
             f"{summary['sound_drafts']} of {args.runs} drafts were sound; {summary['valid_terms']} of {args.runs} "
             "drafts produced valid terms.", "",
             "| run | s | valid | " + " | ".join(subjects) + " | sound |", "|---|---|---|" + "---|" * len(subjects) + "---|"]
    for r in runs:
        cells = [str(r["results"].get(n, {}).get("score_pct", "—")) for n in subjects]
        lines.append(f"| {r['run']} | {r['seconds']} | {'yes' if not r['problems'] else 'no'} | "
                     + " | ".join(cells) + f" | {'yes' if r['sound'] else 'no'} |")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
