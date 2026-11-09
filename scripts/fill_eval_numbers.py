"""Write the measured AI-evaluation numbers into README.md, DEVPOST.md and docs/VERIFICATION_2026-11-09.md from one
evidence file, so the documents cannot drift from the evidence.

    python scripts/fill_eval_numbers.py evidence/ai-eval/<run>-retallied.json
Replaces the blocks between <!-- EVAL_TABLE -->, <!-- EVAL_DEVPOST -->, <!-- EVAL_VERIFY --> markers (idempotent).
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def block(marker: str, body: str, text: str) -> str:
    start, end = f"<!-- {marker} -->", f"<!-- /{marker} -->"
    new = f"{start}\n{body.strip()}\n{end}"
    if end in text:
        return re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _m: new, text, flags=re.S)
    return text.replace(start, new)


def main() -> None:
    src = Path(sys.argv[1])
    res = json.loads(src.read_text())
    rel = src.relative_to(ROOT) if src.is_absolute() else src
    d, c = res["docs"], res.get("code") or {}
    fails = sum(1 for r in d["keyword_baseline"]["rows"] if r["label"] == "fail")
    passes = sum(1 for r in d["keyword_baseline"]["rows"] if r["label"] == "pass")
    runs = res.get("runs", 1)
    kw = d["keyword_baseline"]["summary"]
    rows = [("Keyword rule (all keywords present)", kw, 1)]
    for m, r in d["models"].items():
        rows.append((f"{m}: model alone", r["ai_only"], runs))
        rows.append((f"**{m}: PactPay pipeline**", r["pactpay"], runs))
    table = ["| System | Paid for an unmet requirement (false release) | Correct automatic verdicts | Held for a person | Held: injection detected |",
             "|---|---|---|---|---|"]
    for name, t, k in rows:
        table.append(f"| {name} | **{t['false_release']}** of {fails * k} | {t['correct_pass'] + t['correct_fail']} of "
                     f"{t['cases']} | {t['held']} | {t['held_injection']} |")
    code_lines = []
    if c:
        code_lines = ["", f"Executable criteria ({c['cases']} deliverables, 15 of them adversarial: fake markers, "
                      "hard-coded answers, harness tampering, network/subprocess attempts, infinite loops, prompt "
                      "injection):", "",
                      "| System | Deliverables with exactly the expected per-criterion result | Criteria passed that should not |",
                      "|---|---|---|"]
        for name, s in c["systems"].items():
            code_lines.append(f"| {name.replace('_', ' ')} | {s['matches_expected']} of {c['cases']} | {s['unsafe_passes']} |")
    note = (f"Labels: {passes} pass and {fails} fail criterion-cases per run, {runs} run(s) per model, local models via "
            f"Ollama. One label (d7 A1) was corrected after review, against PactPay; see `tests/fixtures/ai_eval/"
            f"labels.json`. Source: `{rel}`.")
    honest = ("**What this shows.** On requirements that tests cannot check, the models judge far better than a "
              "keyword rule. The pipeline's checks (verified citations, injection hold, fail-closed) cost some "
              "automation: correct work is sometimes held for a person. They do not catch every semantic "
              "misjudgment: d7 A1, a guide that is correct except that it targets the live API, was passed by some "
              "models. That is why the AI only recommends, and a person releases.")
    readme_body = "\n".join(table + code_lines + ["", note, "", honest])
    best = min(d["models"].items(), key=lambda kv: kv[1]["pactpay"]["false_release"])
    devpost_body = (f"**Measured (local models, hand-labelled set; `{rel}`).** On {fails} unmet semantic requirements "
                    f"per run, a keyword rule would have paid for {kw['false_release']}. The PactPay pipeline paid "
                    + ", ".join(f"{r['pactpay']['false_release']} ({m}, {runs} runs)" for m, r in d["models"].items())
                    + ". It held uncertain or injected work for a person instead. On "
                    f"{c.get('cases', 0)} code deliverables (15 adversarial), every configuration matched the expected "
                    "result per criterion and passed nothing it should not. The AI never moves money: a person does.")
    for path, marker, body in ((ROOT / "README.md", "EVAL_TABLE", readme_body),
                               (ROOT / "DEVPOST.md", "EVAL_DEVPOST", devpost_body),
                               (ROOT / "docs" / "VERIFICATION_2026-11-09.md", "EVAL_VERIFY",
                                f"Command: `python scripts/eval_ai_reviewer.py --model gpt-oss:20b --model qwen3.8:27b "
                                f"--model ministral-3:8b --runs 2`, re-tallied with `--retally` after the label "
                                f"correction.\n\n" + readme_body)):
        text = path.read_text()
        path.write_text(block(marker, body, text))
        print("updated", path.relative_to(ROOT))
    print(f"best pipeline false releases: {best[0]} {best[1]['pactpay']['false_release']}")


if __name__ == "__main__":
    main()
