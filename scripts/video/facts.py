"""Build facts.json for tts.py from evidence files and the running server, so narration only states measured facts.

    python scripts/video/facts.py --base http://127.0.0.1:18480 --eval evidence/ai-eval/reviewer-....json --out build/video/facts.json
"""
import argparse
import json
from pathlib import Path

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--eval", required=True)
    ap.add_argument("--model", default="gpt-oss:20b")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    health = httpx.get(a.base.rstrip("/") + "/api/health", timeout=10).json()
    live = health["paypal"]["mode"] == "live_sandbox"
    ev = json.loads(Path(a.eval).read_text())
    kw = ev["docs"]["keyword_baseline"]["summary"]
    pp = ev["docs"]["models"][a.model]["pactpay"]
    fails = sum(1 for r in ev["docs"]["keyword_baseline"]["rows"] if r["label"] == "fail")
    facts = {
        "LIVE_OR_SIM": "live PayPal Sandbox" if live else "offline PayPal simulator",
        "FUND_NOTE": ("Here the buyer approved in PayPal's real Sandbox checkout." if live else
                      "In this recording, PayPal is the labelled offline simulator."),
        "PAYOUT_NOTE": ("These come straight from PayPal's Payouts API." if live else "Here, from the simulator."),
        "EVAL_CAPTION": (f"Measured: keyword rule paid for {kw['false_release']} of {fails} unmet requirements · "
                         f"PactPay + {a.model}: {pp['false_release']}"),
        "EVAL_NARRATION": (f"On our labelled test set, a keyword rule paid for {kw['false_release']} unmet "
                           f"requirements. PactPay, with the model in this demo, paid for {'none' if pp['false_release'] == 0 else pp['false_release']}."),
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(facts, indent=1))
    print(json.dumps(facts, indent=1))


if __name__ == "__main__":
    main()
