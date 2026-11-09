"""PactPay's execution-backed deliverable evaluator.

    from app.evaluation import evaluate, openai_compatible_reviewer
    report = evaluate(criteria, "deliverable.py", source, reviewer=openai_compatible_reviewer())

The submission runs in a sandboxed worker process. The agreed tests run in a separate runner process that signs
each verdict. An optional AI reviewer can only hold money back, and only with verbatim citations. The report is
evidence plus a recommended score: a person releases money.
"""

from .engine import ENGINE_VERSION, MAX_SUBMISSION_BYTES, evaluate
from .reviewer import Reviewer, detect_injection, gemini_reviewer, openai_compatible_reviewer, reviewer_from_env

__all__ = [
    "ENGINE_VERSION",
    "MAX_SUBMISSION_BYTES",
    "Reviewer",
    "detect_injection",
    "evaluate",
    "gemini_reviewer",
    "openai_compatible_reviewer",
    "reviewer_from_env",
]
