# YAVQEN

**Evidence First. Humans Decide.** Milestone payments on the PayPal Sandbox: both sides lock the agreement, the
work is tested against it, a person releases the money, and PayPal's own records report each payee's status.
Built for the PayPal AI Hackathon 2026.

**Video:** [2:51 demo (1080p)](https://drive.google.com/file/d/1sQj6m0bmVTZ_A2jSvdY_0gxMvPt9_uCc/view) · **Run and test it:** [commands](#run-and-verify-it) (138 tests, offline) ·
**Live Sandbox evidence:** [evidence/live-sandbox/](evidence/live-sandbox/) · **License:** [MIT](LICENSE)

> **Formerly PactPay** (renamed 11 Nov 2026). The repository URL, `PACTPAY_*` variables, internal identifiers,
> PayPal request ids and `sender_batch_id`s (`pactpay-…`, `PACTPAY-…`), texts sent to PayPal (such as the Checkout
> brand name), the recorded evidence in `evidence/` and the 9 Nov demo recording keep the former name on purpose.

## What it solves, and for whom

In the demo preset, Maya (client) agrees a $500 milestone with Alex and Sam in a team chat: four requirements, a
70/30 split. In an ordinary freelance payment those terms stay in the chat, the client judges the delivery by eye,
and someone splits the money by hand. YAVQEN turns such a chat into terms both sides approve, tests the delivery
against them and, once Maya releases the money, captures it through PayPal and pays each share to the cent.

* **Clients** see what passed for each requirement, and why, before any money is captured.
* **Independent builders and small teams** work to fixed, testable terms and get an exact split of what PayPal
  actually received (after its fee), with each payee's payout status as PayPal reports it.

## Why AI, and its limits

* **Chat to checkable terms.** A model drafts amount, criteria, payees and shares (and may draft tests) from the
  chat. Any requirement, e-mail or amount not found word for word in the chat is dropped and reported. (The demo
  presets use prepared reference terms; paste your own agreement to have the model draft it.)
* **Work against requirements.** For requirements that tests cannot check, a model judges the delivery and must
  cite the lines it relied on. Code checks every citation; a verdict without a verified citation does not count.
* **Limits.** The AI only recommends an amount; it cannot release, capture or move money. On test-judged
  requirements it can only send work to a person, never pass it. It can misjudge
  ([measured below](#measured-not-claimed)), so a person decides. Without an AI provider, the tests still run.

## How PayPal is integrated (`app/paypal_client.py`)

| Step | PayPal REST API |
|---|---|
| Hold | Orders v2: create with `intent: AUTHORIZE`; buyer approves in PayPal Checkout; `POST …/orders/{id}/authorize`; `GET` the order to check amount, currency and pact |
| Release | Payments v2: `POST /v2/payments/authorizations/{id}/capture`, once per release (`…/void` cancels the hold) |
| Split | Payouts v1: one `POST /v1/payments/payouts` batch; batch and items read back with `GET` |
| Webhooks | `POST /v1/notifications/verify-webhook-signature` on each delivery, then the resource is re-read from PayPal (live delivery to a public URL was not tested) |

PayPal processes and reports each payment operation; it does not read or enforce the agreement. YAVQEN builds the
order, capture and payout batch from the locked terms. It is a Sandbox demo, not an escrow service or a licensed
money transmitter ([known limits](docs/ARCHITECTURE.md#8-known-limits-stated-not-hidden)).

## How it differs from an ordinary freelance payment

* **Agreement locked by fingerprint.** Both parties approve one SHA-256 of the canonical terms; any edit cancels
  both approvals. Once both approve, edits are refused (409 `terms_locked`), and the terms are re-checked against
  the fingerprint before any capture. (Tamper evidence, not a legal signature.)
* **Evidence tests in an isolated sandbox.** Agreed tests run under `bwrap` with no network (without `bwrap`, in a
  restricted subprocess; the report says which).
* **Cited AI verdicts, prompt-injection scanner.** A verdict counts only if its quote is found at the cited lines.
  Text that tries to instruct the reviewer makes AI verdicts advisory, so AI-judged requirements wait for a person.
* **Human-only release.** The authenticated client releases; a smaller amount is a proposal the freelancer accepts
  or disputes, and an arbiter decides disputes. A second release is refused (409 `already_released`).
* **PayPal's real payout status.** Reconcile reads each payout item back from PayPal; UNCLAIMED is shown as not paid out.

## Run and verify it

Python 3.12 on Linux. No PayPal account or API key: without credentials a labelled offline simulator stands in.

```bash
git clone https://github.com/ApexAis/pactpay.git && cd pactpay
python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
PACTPAY_DB_PATH=:memory: python -m pytest -q                    # 138 passed
PACTPAY_DB_PATH=:memory: python scripts/offline_walkthrough.py  # whole journey: PAID_OUT, 2nd release -> 409
uvicorn app.main:app --port 8000                                # UI at http://127.0.0.1:8000
```

Browser end-to-end scripts, AI provider and live Sandbox setup: [Quick start](#quick-start-offline-no-credentials).

**Live Sandbox evidence** (`evidence/live-sandbox/video-20261109T0840Z.json`, [more below](#verified-against-the-live-paypal-sandbox-2026-11-09)):
$500.00 authorized, then captured once (capture `3V818276N9145912L` COMPLETED; PayPal fee $24.80, net $475.20).
Payouts batch `QA35RLSW9N842`: 70 % share $332.64 **SUCCESS**; 30 % share $142.56 **UNCLAIMED**, not received.

## The journey

| # | Step | What happens | PayPal |
|---|---|---|---|
| 1 | **Agree** | An AI drafts terms from the chat. Any requirement, e-mail or amount not found word for word in the chat is dropped and reported. Client and freelancer each approve one SHA-256 of the canonical terms; any edit cancels both approvals. | — |
| 2 | **Fund** | The order is built from the locked terms. The buyer approves in PayPal Checkout. YAVQEN accepts the hold only after reading PayPal's order record (amount, currency, pact). | Orders v2 create, authorize, get |
| 3 | **Deliver** | The freelancer submits code or Markdown; it is hashed. | — |
| 4 | **Verify** | The agreed tests run in a `bwrap` sandbox. An AI reviewer then judges each criterion and must cite the work verbatim; code verifies every citation. If the work seems to address the reviewer (prompt injection), AI verdicts become advisory: test-judged criteria are decided by their tests alone, and AI-judged criteria wait for a person. | — |
| 5 | **Decide** | The evaluation is a recommendation. The client releases (less than the full amount is a proposal the freelancer accepts or disputes); an arbiter decides disputes ($0 voids the hold). | — |
| 6 | **Capture** | One capture per release, with a deterministic `PayPal-Request-Id` and `invoice_id`. Lost answers are recovered from PayPal's record. | Payments v2 capture, void |
| 7 | **Pay out** | One Payouts batch splits PayPal's *net* (after its fee) to the cent. Each item's status is shown as PayPal reports it (SUCCESS, UNCLAIMED, FAILED…). | Payouts v1; webhooks verified if delivered |

Every step is written to a hash-chained event log. The Evidence tab links each requirement to its tests, the AI's
citations, the human decision and the PayPal records.

## Quick start (offline, no credentials)

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PACTPAY_DB_PATH=:memory: python -m pytest -q          # 138 tests
PACTPAY_DB_PATH=:memory: python scripts/offline_walkthrough.py
uvicorn app.main:app --port 8000                      # open http://127.0.0.1:8000
```

**Browser end-to-end (optional).** With the server running, in a second terminal (Playwright downloads Chromium):

```bash
pip install -r requirements-dev.txt && playwright install chromium
python scripts/ui_e2e.py http://127.0.0.1:8000 /tmp/pactpay-ui            # main journey, one screenshot per step
python scripts/ui_dispute_e2e.py http://127.0.0.1:8000 /tmp/pactpay-dispute  # partial release, dispute, arbiter
```

Without PayPal credentials, YAVQEN uses an offline PayPal protocol simulator and says so on every screen (yellow
SIMULATOR banner); no PayPal account is contacted. Without an AI provider, the agreed tests still run, and AI-judged
criteria go to human review.

**AI provider** (pick one): a local open-weight model at no cost, an OpenAI-compatible API, or Gemini.

```bash
ollama pull gpt-oss:20b
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://127.0.0.1:11434/v1 AI_MODEL=gpt-oss:20b
# or OPENAI_API_KEY=... AI_MODEL=gpt-4o-mini   |   GEMINI_API_KEY=... AI_MODEL=gemini-2.5-flash
```

**Live PayPal Sandbox:** see [docs/SANDBOX_SETUP.md](docs/SANDBOX_SETUP.md). With credentials, every PayPal step
is a real Sandbox call and the banner turns green. `bash scripts/run_live_sandbox.sh` runs the full journey and
writes evidence to `evidence/live-sandbox/`. The buyer's approval in PayPal Checkout is either automated with a
Sandbox buyer login you provide, or done by you through the approval link the script prints (both recorded runs
were approved by hand).

### Verified against the live PayPal Sandbox (2026-11-09)

`evidence/live-sandbox/e2e-20261109T050830Z.json`, with every call to `api-m.sandbox.paypal.com`:

1. Authorizing before the buyer approved was refused (`ORDER_NOT_APPROVED`).
2. The buyer approved in PayPal Checkout. Authorization `93442531YL256941R` was CREATED for $500.00, checked against
   PayPal's order record.
3. The evaluation scored 100 %.
4. Capture `62V48776D0025551B` COMPLETED: fee $24.80, net $475.20.
5. One Payouts batch, `N9JA975ZXZT5U`: $332.64 **SUCCESS** and $142.56 **UNCLAIMED** (`RECEIVER_UNREGISTERED`), as
   PayPal reported them.
6. A second release was refused (409). PayPal received exactly one capture and one payout.

The PayPal results in the demo video come from a second live run the same day,
`evidence/live-sandbox/video-20261109T0840Z.json` (18 PayPal calls, all Sandbox): authorization `09W85969TX3366108` for $500.00, capture `3V818276N9145912L` (fee
$24.80, net $475.20), Payouts batch `QA35RLSW9N842`: $332.64 SUCCESS to PayPal's Sandbox payouts test receiver and
$142.56 UNCLAIMED. UNCLAIMED is not a completed payment: PayPal keeps that item for a recipient without a Sandbox
account and returns it after 30 days if nobody claims it.

**Requirements:** Python 3.12, Linux with `bwrap` (bubblewrap) for the evaluation sandbox. Without `bwrap` the
evaluator falls back to a restricted subprocess and reports which isolation it used.

## Where AI is used, and where it is not

| Task | AI's role | Guard |
|---|---|---|
| Drafting terms from a chat | Extracts criteria, payees, shares and amount; may draft acceptance tests | Everything must be quoted from the chat. Drafted tests are limited to safe standard-library imports. Both parties review and approve the exact hash |
| Judging semantic requirements (`ai_review`) | Decides pass or fail with quotes from the work | A verdict counts only with a verified citation. Injection puts the criterion on hold for a person. Uncertain means a person decides |
| Reviewing test-judged code | Can flag work that passes tests but looks wrong | Can only move a criterion to *needs human review*, never to PASS |
| Moving money | **None** | Release, accept and resolve are human actions. Amounts come from integer arithmetic on the locked terms |

### Measured, not claimed

`scripts/eval_ai_reviewer.py` runs a hand-labelled set of 8 Markdown deliverables × 4 semantic criteria
(`tests/fixtures/ai_eval/`). The deliverables are positive, paraphrased, keyword-stuffed, contradicting,
prompt-injected, vague, unsafe and placeholder. They were run through a keyword rule baseline, the raw model, and
the YAVQEN pipeline, plus the 15-case adversarial code corpus and the demo deliverables. All models ran locally.

<!-- EVAL_TABLE -->
| System | Paid for an unmet requirement (false release) | Correct automatic verdicts | Held for a person | Held: injection detected |
|---|---|---|---|---|
| Keyword rule (all keywords present) | **6** of 17 | 21 of 32 | 0 | 0 |
| gpt-oss:20b: model alone | **0** of 34 | 64 of 64 | 0 | 0 |
| **gpt-oss:20b: YAVQEN pipeline** | **0** of 34 | 46 of 64 | 10 | 8 |
| qwen3.8:27b: model alone | **2** of 34 | 62 of 64 | 0 | 0 |
| **qwen3.8:27b: YAVQEN pipeline** | **0** of 34 | 56 of 64 | 0 | 8 |
| ministral-3:8b: model alone | **2** of 34 | 62 of 64 | 0 | 0 |
| **ministral-3:8b: YAVQEN pipeline** | **2** of 34 | 42 of 64 | 10 | 8 |

Executable criteria (19 deliverables, 15 of them adversarial: fake markers, hard-coded answers, harness tampering, network/subprocess attempts, infinite loops, prompt injection):

| System | Deliverables with exactly the expected per-criterion result | Criteria passed that should not |
|---|---|---|
| tests only | 19 of 19 | 0 |
| tests+gpt-oss:20b | 19 of 19 | 0 |
| tests+qwen3.8:27b | 19 of 19 | 0 |
| tests+ministral-3:8b | 19 of 19 | 0 |

Labels: 15 pass and 17 fail criterion-cases per run, 2 run(s) per model, local models via Ollama. One label (d7 A1) was corrected after review, against YAVQEN; see `tests/fixtures/ai_eval/labels.json`. Source: `evidence/ai-eval/reviewer-gpt-oss-20b-qwen3.8-27b-ministral-3-8b-20261109T033712Z-retallied.json`.

**What this shows.** On requirements that tests cannot check, the models judge far better than a keyword rule. The pipeline's checks (verified citations, injection hold, fail-closed) cost some automation: correct work is sometimes held for a person. They do not catch every semantic misjudgment: d7 A1, a guide that is correct except that it targets the live API, was passed by some models. That is why the AI only recommends, and a person releases.
<!-- /EVAL_TABLE -->

Full per-case results: [evidence/ai-eval/](evidence/ai-eval/).

## Payment safety

* **Policy and arithmetic**
  * Integer cents and basis points everywhere; weights and shares must total exactly 10,000 bps.
  * Largest-remainder split with a conservation check.
  * A payout batch that does not add up to PayPal's net, or that names anyone not in the locked terms, is never
    sent.
* **One release, one capture**
  * Each release happens once: a compare-and-set state machine plus deterministic `PayPal-Request-Id`,
    `invoice_id` and `sender_batch_id`.
  * Concurrent releases capture exactly once (tested).
* **Unknown outcomes**
  * Timeouts, lost answers and restarts are reconciled from PayPal's record before anything is retried.
* **Webhooks**
  * Verified by PayPal (`verify-webhook-signature`, raw body), fail closed without a webhook id, de-duplicated by
    event id, and applied by re-reading the resource from PayPal.
  * Live delivery to a public URL was not tested; without it, **Reconcile** polls PayPal for the same states.
* **Access and terms integrity**
  * Role checks on every route.
  * Idempotency keys on money routes; a key reused with a different body is refused.
  * Rate limits on costly routes.
  * Locked terms are re-verified against their hash before any capture.

Tests: `tests/test_flow.py` (lifecycle, recovery, concurrency, webhooks), `tests/test_money_safety.py` (arithmetic,
tampering, parsing; 16 of its tests failed before the 2026-11-09 fixes), `tests/test_security_gates.py`,
`tests/test_evaluation.py` (sandbox, adversarial corpus, reviewer rules), `tests/test_terms.py`.
`scripts/ui_e2e.py` clicks the whole journey in Chromium (needs `requirements-dev.txt`; see Quick start).

## Repository map

```
app/main.py            HTTP API, idempotency keys, rate limits, security headers
app/flow.py            lifecycle state machine, PayPal steps, recovery, webhooks, evidence
app/terms.py           policy validation, basis points, AI drafting with grounding checks, split_cents
app/paypal_client.py   PayPal REST client (Sandbox only), traces with tokens redacted
app/paypal_sim.py      offline PayPal protocol simulator (labelled as such)
app/evaluation/        sandboxed test runner, AI reviewer, injection detection, adversarial corpus
app/store.py           SQLite schema, hash-chained event log
app/static/            web client (no build step)
scripts/               walkthroughs, live Sandbox checks, AI evaluation, UI end-to-end, demo video pipeline
docs/                  architecture and limits, Sandbox setup, competitive differentiation
evidence/              live Sandbox results and AI evaluation results (ids and numbers; no credentials)
```

## License

MIT, see [LICENSE](LICENSE).
