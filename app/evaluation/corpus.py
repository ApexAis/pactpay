"""Loads the adversarial corpus (``tests/fixtures/adversarial/MANIFEST.json``) and the demo presets as cases.

Used by ``tests/test_evaluation.py`` and ``scripts/eval_ai_reviewer.py``; the engine itself never imports it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .preset_tests import preset_criteria

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "adversarial"
PAD_BYTES = 3 * 1024 * 1024


def _fixed_webhook() -> str:
    from app.presets import FIXED_SUBMISSION_CODE

    return FIXED_SUBMISSION_CODE


def _generate(kind: str) -> str:
    if kind == "fixed_webhook_padded_3mb":
        filler = "# padding: operational notes that change nothing about the behaviour of this module.\n"
        return _fixed_webhook() + "\n" + filler * (PAD_BYTES // len(filler) + 1)
    raise ValueError("unknown generated case %r" % kind)


def corpus_cases(fixtures: Path = FIXTURES) -> list[dict[str, Any]]:
    """-> [{"id", "filename", "content", "criteria", "expected_score", "expected_status", "expected_flags"}]"""
    manifest = json.loads((fixtures / "MANIFEST.json").read_text(encoding="utf-8"))
    cases = []
    for case in manifest["cases"]:
        if "generate" in case:
            content = _generate(case["generate"])
            filename = case["filename"]
        else:
            content = (fixtures / case["file"]).read_text(encoding="utf-8")
            filename = case["file"]
            if case.get("base") == "fixed_webhook":
                content = _fixed_webhook() + content
        cases.append({
            "id": case["id"],
            "filename": filename,
            "content": content,
            "criteria": preset_criteria(case["preset"]),
            "expected_score": float(case["expected_score"]),
            "expected_status": dict(case["expected_status"]),
            "expected_flags": list(case.get("expected_flags") or []),
        })
    return cases


def preset_cases() -> list[dict[str, Any]]:
    from app.presets import DEMO_PRESETS

    expected = {"paypal_webhook_sprint": ({"C3": "FAIL"}, 75.0), "sdk_migration_docs": ({"C4": "FAIL"}, 75.0)}
    cases = []
    for preset_id, preset in DEMO_PRESETS.items():
        if preset_id not in expected:
            continue
        failing, flawed_score = expected[preset_id]
        for which, score in (("flawed_submission", flawed_score), ("fixed_submission", 100.0)):
            sub = preset[which]
            status = {c["id"]: "PASS" for c in preset_criteria(preset_id)}
            if which == "flawed_submission":
                status.update(failing)
            cases.append({
                "id": "%s:%s" % (preset_id, which.split("_")[0]),
                "filename": sub["filename"],
                "content": sub["content"],
                "criteria": preset_criteria(preset_id),
                "expected_score": score,
                "expected_status": status,
                "expected_flags": [],
            })
    return cases
