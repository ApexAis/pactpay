# PactPay's design choices for AI-assisted milestone payments

*This note describes the choices PactPay makes and where each one can be checked in this repository. It does not
assess other projects or products.*

## The concept is not new

AI-assisted milestone payments, authorization holds released on verified work, and AI-to-payout pipelines exist in
other projects and in commercial milestone-billing tools. PactPay does not claim to have invented any of them. It aims
for a specific, testable combination of payment-safety and AI-containment properties.

## Design choices

| Question | Options a design can take | PactPay's choice | Cost of that choice | Where to check |
|---|---|---|---|---|
| Who triggers the capture? | An AI score above a threshold; an agent within a mandate; a person | Only a person: the client's release, the freelancer's acceptance of a proposal, or the arbiter's decision | Slower than automatic release; someone has to act | `app/flow.py` (`release`, `resolve`); `tests/test_security_gates.py` |
| What can the AI change? | Verdicts that drive payment directly; advisory verdicts | A verdict counts only with a citation that code verifies. The AI can move a passing test to `NEEDS_HUMAN_REVIEW`, never a failure to a pass | More work is held for a person | `app/evaluation/engine.py`, `app/evaluation/reviewer.py`; `tests/test_evaluation.py` |
| How are the terms fixed? | Editable terms; a hashed specification | Both parties approve one SHA-256 of the canonical terms, including the parties and the source chat's hash. Any edit invalidates both approvals; release re-verifies the stored terms | Every change needs both approvals again | `app/terms.py`, `app/flow.py`; `tests/test_flow.py`, `tests/test_money_safety.py` |
| How is the money held? | Capture up front; an authorization hold | Orders v2 `intent: AUTHORIZE`, accepted only after reading PayPal's order record | PayPal honours an authorization for 3 days (valid up to 29); PactPay does not reauthorize yet | `app/flow.py`, `app/paypal_client.py` |
| How is it paid out? | One payee; manual transfers; a Payouts batch | One Payouts batch splits PayPal's net (after its fee) in integer cents, with a conservation check; each item's status is shown as PayPal reports it | A recipient without an account shows `UNCLAIMED` until claimed | `app/terms.py` (`split_cents`), `app/flow.py` (`_payout`); `tests/test_money_safety.py` |
| What if PayPal's answer is lost? | Retry; ask a person | Deterministic `PayPal-Request-Id`, `invoice_id` and `sender_batch_id`; reconcile reads PayPal's record before any retry | Some states wait for reconcile or for a person | `app/flow.py`; `tests/test_flow.py` |

## Properties that can be checked here

1. **The AI can hold money back but can never release it.**
   * The AI can turn a passing test into `NEEDS_HUMAN_REVIEW`, never a failure into a pass, and only a person's
     decision triggers a capture.
   * Code: `app/evaluation/engine.py`, `Service.release` / `resolve`. Tests: `tests/test_evaluation.py`,
     `tests/test_security_gates.py`.
2. **AI verdicts must cite the work verbatim, and the citations are checked by code.**
   * A verdict whose quote is not in the cited lines is shown struck through and ignored.
   * Prompt-injection attempts in the deliverable put the affected criteria on hold.
   * Measured against a keyword baseline and against the raw model in `evidence/ai-eval/`.
3. **Two-party approval of one SHA-256 of the canonical terms**, including the parties and the source agreement's hash.
   * Any edit invalidates both approvals.
   * Release, accept and resolve re-verify the stored terms against the locked hash before capturing.
   * Tests: `tests/test_flow.py`, `tests/test_money_safety.py`.
4. **An authorization hold and a multi-payee Payouts split in one lifecycle, reconciled per recipient.**
   * Integer basis points throughout, largest-remainder cents on PayPal's net (`seller_receivable_breakdown`), and a
     conservation check before any batch is sent.
   * Each item is shown with the status PayPal reports (`SUCCESS`, `UNCLAIMED`, `FAILED`…).
5. **Unknown outcomes are reconciled, never guessed.**
   * Deterministic `PayPal-Request-Id` and `sender_batch_id` values.
   * A lost capture answer is found by `invoice_id` in the order record.
   * A lost payout answer cannot lead to a second payout: PayPal refuses a second batch with the same
     `sender_batch_id`. The pact moves to `PAYOUT_ATTENTION`, and the batch is matched when PayPal's batch webhook
     arrives. Its signature is checked with `verify-webhook-signature`.
   * The webhook path is covered by tests that use the offline simulator's signed webhooks. **Live webhook delivery to a
     public URL was not tested.** Without it, payout statuses come from reconcile polling, and a pact in
     `PAYOUT_ATTENTION` waits for a person.
   * There are tests for each case in `tests/test_flow.py`.
6. **Three roles with an arbiter**, whose decision (`$0` voids the hold) is recorded in the hash-chained evidence.

## Where PactPay is weaker, honestly

* **No Agent Toolkit or MCP release integration.** PactPay deliberately keeps a person in the release loop.
* **No reauthorization.** PactPay does not reauthorize after the 3-day honor period.
* **Live webhook delivery was not tested.** Statuses come from reconcile polling; webhook verification is implemented
  and tested offline.
* **No public hosted demo.** PactPay runs locally from the repository, with the offline simulator or your own Sandbox
  credentials.
* **Single-node SQLite.** See `docs/ARCHITECTURE.md` §8.
