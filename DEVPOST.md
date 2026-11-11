# YAVQEN: Devpost submission text

*Paste section by section. Numbers marked (measured) come from files in `evidence/`; re-check them before
submitting if anything is re-run.*

## Tagline
Agreements that pay out as agreed: a PayPal authorization holds the payment, tests and an AI reviewer check the
work, and a person releases it.

## Inspiration
Small teams of freelancers get paid on trust. The client either pays up front and hopes, or the team delivers first
and hopes, and then someone splits the money by hand. Some AI payment demos also let a model decide when money moves.
We wanted the agreement itself to become the payment policy: precise enough that YAVQEN can apply it step by step
while PayPal processes and reports each payment operation, readable enough that both sides can approve its SHA-256
fingerprint, with an AI that helps review the work but cannot be talked into paying.

> YAVQEN is a PayPal Sandbox demo. It is not an escrow service or a licensed money transmitter. Its SHA-256 terms
> fingerprint is tamper evidence inside YAVQEN, not a legal signature. PayPal processes and reports the payment
> operations; it does not read or enforce the agreement.

## What it does
YAVQEN turns a freelance agreement into a locked payment policy and applies it itself; PayPal's Sandbox APIs process
and report each payment operation.

1. **Agree.** An AI drafts the policy from the chat: the amount in cents, acceptance criteria, payees with shares in
   basis points, and, for code, executable acceptance tests. Anything it cannot quote verbatim from the chat is
   dropped and reported. The client and freelancer each approve one SHA-256 of the canonical terms; any edit cancels
   both approvals.
2. **Fund.** PayPal Orders v2 with `intent: AUTHORIZE`. The buyer approves in PayPal Checkout. YAVQEN accepts the
   hold only after reading PayPal's order record and finding the locked amount and the pact id YAVQEN attached to
   the order.
3. **Verify.** The agreed tests run in an isolated sandbox. An AI reviewer judges each criterion and must cite the
   exact lines; code verifies every quote. Prompt injection in the work is detected and puts the affected criteria on hold. The AI can
   turn a pass into "needs human review", never a failure into a payment.
4. **Decide.** The client releases (anything less than the full hold is a proposal the freelancer accepts or
   disputes), or an arbiter decides. $0 voids the authorization.
5. **Settle.** One PayPal capture, then one Payouts batch that splits PayPal's net (after its fee) to the cent.
   Each payee's status is shown exactly as PayPal reports it: SUCCESS, UNCLAIMED, FAILED…
6. **Prove.** A hash-chained record links each requirement, test, AI citation and human decision to the PayPal
   calls and their debug ids.

## How we built it
* **Backend.** Python 3.12, FastAPI and SQLite. `app/flow.py` is a compare-and-set state machine; every
  transition appends to a hash-chained event log.
* **PayPal (Sandbox).** OAuth2, Orders v2 (create, authorize, get), Payments v2 (capture with `final_capture`,
  void, get), Payouts v1 (batch, item status) and Webhooks (`verify-webhook-signature` on the raw body).
  * Deterministic `PayPal-Request-Id`, `invoice_id` and `sender_batch_id` make every money step happen once.
  * Lost answers are reconciled from PayPal's records, never guessed.
* **Evaluator.** Agreed tests run in a `bwrap` sandbox (no network, own namespaces, resource limits). A separate
  runner process executes them.
* **AI.**
  * The drafter and reviewer use any OpenAI-compatible model or Gemini. Our demo and evaluation ran open-weight
    models locally on an RTX 4090 through Ollama (gpt-oss:20b, qwen3.8:27b, ministral-3:8b), at no API cost.
  * The reviewer's output is a strict JSON contract; citations are verified against the file before a verdict
    counts.
* **Frontend.** Dependency-free JavaScript with a strict CSP. A persona switcher lets one person play client,
  freelancer and arbiter.

## How AI is used (Tools section)
* **gpt-oss:20b** (Apache-2.0, local via Ollama) drafts terms from chats and reviews deliverables in the demo.
* **qwen3.8:27b** and **ministral-3:8b** (local) are used as comparison reviewers in the evaluation.
* **Coding assistants**
  * Google Gemini generated the first version of the code.
  * Anthropic Claude rebuilt the payment core and wrote this version's UI, evaluation and audit fixes.
  * A ChatGPT-produced variant contributed ideas (exact-cent policy checks) that were re-implemented and tested
    here.

<!-- EVAL_DEVPOST -->
**Measured (local models, hand-labelled set; `evidence/ai-eval/reviewer-gpt-oss-20b-qwen3.8-27b-ministral-3-8b-20261109T033712Z-retallied.json`).** On 17 unmet semantic requirements per run, a keyword rule would have paid for 6. The YAVQEN pipeline paid 0 (gpt-oss:20b, 2 runs), 0 (qwen3.8:27b, 2 runs), 2 (ministral-3:8b, 2 runs). It held uncertain or injected work for a person instead. On 19 code deliverables (15 adversarial), every configuration matched the expected result per criterion and passed nothing it should not. The AI never moves money: a person does.
<!-- /EVAL_DEVPOST -->

## Verified on the live PayPal Sandbox
These are real Sandbox calls, recorded in `evidence/live-sandbox/e2e-20261109T050830Z.json`. The PayPal results in
the demo video come from a second live run the same day (`evidence/live-sandbox/video-20261109T0840Z.json`: capture
`3V818276N9145912L`, Payouts batch `QA35RLSW9N842`, the same amounts and statuses).

1. The buyer approved in PayPal Checkout. Authorization `93442531YL256941R` was CREATED for $500.00, verified from
   PayPal's order record.
2. Capture `62V48776D0025551B` COMPLETED: fee $24.80, net $475.20.
3. One Payouts batch: $332.64 SUCCESS, and $142.56 UNCLAIMED (`RECEIVER_UNREGISTERED`): that payee has no Sandbox
   account, so the item is not paid unless it is claimed.
4. A second release was refused, and PayPal received exactly one capture and one payout.

## Challenges we ran into
* **Exact money from fuzzy agreements.**
  * Our own audit found that float-tolerant share validation accepted 100.01 % splits, so a payout could exceed
    the capture.
  * Everything is now integer basis points, and a batch that does not add up to PayPal's net is never sent.
* **Unknown outcomes.** A capture whose answer is lost might have happened. YAVQEN looks for it in PayPal's order
  record by `invoice_id` before retrying. Past PayPal's idempotency window, a retry is refused as a duplicate
  invoice, and the original capture is adopted.
* **Keeping the AI honest.**
  * Models cite the first line of a wrapped paragraph and quote the whole thing. We made citation checking
    tolerant of that while still rejecting quotes on the wrong lines.
  * We added injection patterns that our evaluation showed were missing.

## Accomplishments we're proud of
* An AI that is useful and kept on a short leash. On our hand-labelled set it judged prose requirements that no test
  can check. Across 2 runs per model (17 unmet requirements per run), the YAVQEN pipeline recommended paying for an
  unmet requirement 0 of 34 times with gpt-oss:20b, 0 of 34 with qwen3.8:27b and 2 of 34 with ministral-3:8b; a
  keyword rule would have paid for 6 of 17. Those misses are why a person always releases.
* The authorization hold and multi-payee Payouts in one lifecycle, reconciled per recipient.
* 138 automated tests, including concurrency, lost-answer recovery, forged webhooks, tampered terms and a 15-case
  adversarial deliverable corpus. A Chromium end-to-end run clicks the whole journey.

## What we learned
* An accepted payout batch is not a payment, an authorization is not a capture, and a created order is not a hold.
  Treating each as its own fact, read back from PayPal, made the system much simpler to reason about.
* An AI that must show its evidence is far easier to trust, and to debug, than one that only outputs a score.

## What's next
* Reauthorization after PayPal's 3-day honor period.
* Multiple milestones per pact.
* A hosted multi-tenant deployment with real accounts and MFA.
* A read-only MCP tool so a client's own agent can draft agreements and watch status, without release rights.

## Built with
python · fastapi · sqlite · paypal-orders-v2 · paypal-payments-v2 · paypal-payouts · paypal-webhooks · ollama ·
gpt-oss · bubblewrap · playwright · javascript

## Try it
* Repository: https://github.com/ApexAis/pactpay (the repository keeps the project's former name, PactPay)
* Run locally: `pip install -r requirements.txt && uvicorn app.main:app` (offline simulator) or with Sandbox
  credentials ([docs/SANDBOX_SETUP.md](docs/SANDBOX_SETUP.md)).
* Video: linked from the Devpost entry.
