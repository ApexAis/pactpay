# PactPay

**Agreements that pay out exactly as written.**

PactPay turns a freelance agreement into a locked payment policy: the amount in cents, acceptance criteria with
executable tests, and payees with exact shares. A PayPal authorization holds the client's payment. Agreed tests
and an AI reviewer check the delivered work. **The AI can hold money back but can never release it**: a person
releases, and PayPal's own records report every capture and each payee's payout status.

Built for the PayPal AI Hackathon 2026 on the PayPal Developer Sandbox: Orders v2 (`intent: AUTHORIZE`),
Payments v2 authorizations, captures and voids, Payouts v1, and Webhooks verification.

> PactPay is a Sandbox demo. It is not an escrow service or a licensed money transmitter. PayPal does not read or
> enforce the agreement: PactPay applies the locked terms, and PayPal processes and reports each payment operation.
> The SHA-256 terms fingerprint is not a legal signature. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §8 for
> every known limit.

## The journey

| # | Step | What happens | PayPal |
|---|---|---|---|
| 1 | **Agree** | An AI drafts terms from the chat. Any requirement, e-mail or amount not found word for word in the chat is dropped and reported. Client and freelancer each approve one SHA-256 of the canonical terms; any edit cancels both approvals. | — |
| 2 | **Fund** | The order is built from the locked terms. The buyer approves in PayPal Checkout. PactPay accepts the hold only after reading PayPal's order record (amount, currency, pact). | Orders v2 create, authorize, get |
| 3 | **Deliver** | The freelancer submits code or Markdown; it is hashed. | — |
| 4 | **Verify** | The agreed tests run in a `bwrap` sandbox. An AI reviewer then judges each criterion and must cite the work verbatim; code verifies every citation. Prompt injection in the work puts the affected criteria on hold for a person. | — |
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

Without PayPal credentials, PactPay uses an offline PayPal protocol simulator and says so on every screen (yellow
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
the PactPay pipeline, plus the 15-case adversarial code corpus and the demo deliverables. All models ran locally.

<!-- EVAL_TABLE -->
| System | Paid for an unmet requirement (false release) | Correct automatic verdicts | Held for a person | Held: injection detected |
|---|---|---|---|---|
| Keyword rule (all keywords present) | **6** of 17 | 21 of 32 | 0 | 0 |
| gpt-oss:20b: model alone | **0** of 34 | 64 of 64 | 0 | 0 |
| **gpt-oss:20b: PactPay pipeline** | **0** of 34 | 46 of 64 | 10 | 8 |
| qwen3.8:27b: model alone | **2** of 34 | 62 of 64 | 0 | 0 |
| **qwen3.8:27b: PactPay pipeline** | **0** of 34 | 56 of 64 | 0 | 8 |
| ministral-3:8b: model alone | **2** of 34 | 62 of 64 | 0 | 0 |
| **ministral-3:8b: PactPay pipeline** | **2** of 34 | 42 of 64 | 10 | 8 |

Executable criteria (19 deliverables, 15 of them adversarial: fake markers, hard-coded answers, harness tampering, network/subprocess attempts, infinite loops, prompt injection):

| System | Deliverables with exactly the expected per-criterion result | Criteria passed that should not |
|---|---|---|
| tests only | 19 of 19 | 0 |
| tests+gpt-oss:20b | 19 of 19 | 0 |
| tests+qwen3.8:27b | 19 of 19 | 0 |
| tests+ministral-3:8b | 19 of 19 | 0 |

Labels: 15 pass and 17 fail criterion-cases per run, 2 run(s) per model, local models via Ollama. One label (d7 A1) was corrected after review, against PactPay; see `tests/fixtures/ai_eval/labels.json`. Source: `evidence/ai-eval/reviewer-gpt-oss-20b-qwen3.8-27b-ministral-3-8b-20261109T033712Z-retallied.json`.

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
