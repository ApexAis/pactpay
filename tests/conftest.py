"""Test setup: an isolated database per test, the offline PayPal simulator, and no model or network access."""

from __future__ import annotations

import os
import tempfile

for _name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "GEMINI_API_KEY", "OPENAI_API_KEY",
              "OPENAI_BASE_URL", "AI_MODEL"):
    os.environ.pop(_name, None)
os.environ["PACTPAY_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="pactpay-import-"), "import.db")
os.environ["PAYPAL_MODE"] = "sandbox"

from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.flow import Service  # noqa: E402
from app.main import create_app  # noqa: E402
from app.paypal_sim import SimulatedPayPal  # noqa: E402


def stub_evaluate(criteria: list[dict[str, Any]], filename: str, content: str, *, reviewer: Any = None,
                  **_: Any) -> dict[str, Any]:
    """A stand-in evaluator for lifecycle tests: FINAL passes every criterion, DRAFT all but the last, else none."""
    passed = len(criteria) if "FINAL" in content else (len(criteria) - 1 if "DRAFT" in content else 0)
    results = [{"id": c["id"], "title": c["title"], "weight_pct": c["weight_pct"], "method": c["method"],
                "status": "PASS" if i < passed else "FAIL", "tests": [], "tests_passed": 0, "tests_total": 0,
                "ai": None, "earned_pct": c["weight_pct"] if i < passed else 0.0, "evidence": "stub"}
               for i, c in enumerate(criteria)]
    return {"criteria": results, "score_pct": round(sum(r["earned_pct"] for r in results), 2), "flags": [],
            "reviewer": {"status": "not_configured"}, "engine_version": "stub", "sandbox": {"kind": "none"}}


@dataclass
class Env:
    client: TestClient
    sim: SimulatedPayPal
    service: Service
    settings: Settings

    def h(self, token: str | None, key: str | None = None) -> dict[str, str]:
        out = {"Authorization": f"Bearer {token}"} if token else {}
        if key:
            out["Idempotency-Key"] = key
        return out


def make_env(tmp_path: Any, *, evaluate: Any = stub_evaluate, sim: SimulatedPayPal | None = None,
             db: str = "pactpay.db") -> Env:
    settings = Settings(db_path=str(tmp_path / db), demo_mode=True, public_url="http://testserver")
    sim = sim or SimulatedPayPal()
    app = create_app(settings, transport=sim, evaluate=evaluate, reviewer=None, drafter=(None, None), recover=False)
    return Env(TestClient(app), sim, app.state.service, settings)


@pytest.fixture
def env(tmp_path: Any) -> Env:
    return make_env(tmp_path)
