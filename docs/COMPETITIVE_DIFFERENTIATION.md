# How PactPay differs from similar PayPal AI Hackathon projects

*As of 2026-11-09. The other projects were read from their public Devpost pages and shallow clones of their public
repositories; none of their code was run. "Verified in repo" means the code was found, not that it was executed.
The hackathon gallery was not yet published, so other entries may exist.*

## The concept is not new

AI-assisted milestone payments, authorization holds released on verified work, and AI-to-payout pipelines all
exist in several entries and in commercial products (marketplace escrow, milestone-billing tools). PactPay does
not claim to have invented any of them. It claims a specific, testable combination of safety properties.

## What comparable entries show

| Project | Claims | Verified in repo | Gaps visible in the repo |
|---|---|---|---|
| MilestonePay | Gemini plans milestones; AUTHORIZE hold; AI verifier; client releases full or partial; void/reauthorize; invoicing | Orders authorize, capture, void and reauthorize; invoicing; webhook verification; PayPal-Request-Id | Falls back to a mock when credentials are missing; no tests; no Payouts split |
| PayPlan | Spec frozen and hashed; tests before AI; signed reports; MCP server lets an agent release funds; "51/51 labelled cases" | Authorize, capture, void and reauthorize; webhook verification; vitest suites (judge, eval, mcp, rules) | Single payee, no Payouts; an AI agent can release money within a mandate |
| ProofPay | AUTHORIZE escrow; the AI auto-captures when MUST criteria pass and the score is at least 80 | Orders authorize, capture and void; PayPal-Request-Id; 1 test file | The AI releases money; no webhooks or Payouts |
| Handshake | AI agents negotiate; Playwright reviewer; the AI reviewer releases; capture plus Payouts; Agent Toolkit invoices | Agent Toolkit, server SDK, payouts with `sender_batch_id`, webhook verification | The AI releases money; no tests found |
| Tributary | Plain-English revenue share compiled into integer-cent waterfalls; batch payouts; clawback; property tests | `sender_batch_id`, payouts-item cancel, verify-webhook-signature; fast-check and chaos tests | No authorization hold or acceptance step |

## PactPay's distinct position (each item is checkable in this repository)

1. **The AI can hold money back but can never release it.**
   * ProofPay and Handshake let the AI capture or release; PayPlan lets an agent release within a mandate.
   * In PactPay, the AI can turn a passing test into "needs human review", never a failure into a pass, and only a
     person's decision triggers a capture.
   * Code: `app/evaluation/engine.py`, `Service.release` / `resolve`. Tests: `tests/test_evaluation.py`,
     `tests/test_security_gates.py`.
2. **AI verdicts must cite the work verbatim, and the citations are checked by code.**
   * A verdict whose quote is not in the cited lines is shown struck through and ignored.
   * Prompt-injection attempts in the deliverable put the affected criteria on hold.
   * Measured against a keyword baseline and against the raw model in `evidence/ai-eval/`.
3. **Two-party approval of one SHA-256 of the canonical terms**, including the parties and the source agreement's
   hash.
   * Any edit invalidates both approvals.
   * Release, accept and resolve re-verify the stored terms against the locked hash before capturing.
   * Tests: `tests/test_flow.py`, `tests/test_money_safety.py`.
4. **An authorization hold and a multi-payee Payouts split in one lifecycle, reconciled per recipient.**
   * No entry above combines the escrow-style hold with a Payouts split.
   * Integer basis points throughout, largest-remainder cents, and a conservation check before any batch is sent.
   * Each item is shown with the status PayPal reports (SUCCESS, UNCLAIMED, FAILED…).
5. **Unknown outcomes are reconciled, never guessed.**
   * Deterministic `PayPal-Request-Id` and `sender_batch_id` values.
   * A lost capture answer is found by `invoice_id` in the order record.
   * A lost payout answer is identified by PayPal's verified webhook and never resent.
   * There are tests for each case in `tests/test_flow.py`.
6. **Three roles with an arbiter**, whose decision ($0 voids the hold) is recorded in the hash-chained evidence.

## Where PactPay is weaker, honestly

* **No Agent Toolkit or MCP integration.** PactPay deliberately keeps a person in the release loop.
* **No reauthorization.** PactPay does not reauthorize after the 3-day honor period. MilestonePay does.
* **No public hosted demo yet.** Several entries have one; PactPay runs locally from the repository.
* **Single-node SQLite.** See `docs/ARCHITECTURE.md` §8.
