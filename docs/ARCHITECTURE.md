# PactPay architecture, payment safety and limits

PactPay is a FastAPI service with one SQLite database, a PayPal REST client, a sandboxed evaluator and a
dependency-free web client. This document describes what the code does and does not do. File references are
relative to the repository root.

## 1. The lifecycle (app/flow.py)

```
DRAFT ──both parties approve one terms SHA-256──▶ TERMS_APPROVED ──client──▶ AWAITING_BUYER_APPROVAL
  ──buyer approves in PayPal; client authorizes; PayPal's order record checked──▶ FUNDS_HELD
  ──freelancer submits──▶ SUBMITTED ──evaluation──▶ AUDITED
        ├─ client releases the full held amount ─────────────▶ CAPTURING
        ├─ client proposes less ─▶ RELEASE_PROPOSED ─ freelancer accepts ─▶ CAPTURING
        └─ either party disputes ─▶ DISPUTED ─ arbiter decides ─▶ CAPTURING | VOIDED
CAPTURING ─PayPal COMPLETED─▶ CAPTURED ─▶ PAYOUT_PENDING ─▶ PAID_OUT | PAYOUT_UNCLAIMED | PAYOUT_ATTENTION
          ─PayPal PENDING──▶ CAPTURE_PENDING          ─PayPal declined─▶ CAPTURE_FAILED
FUNDS_HELD ─client voids before delivery─▶ VOIDED
```

Every transition goes through `Service._move`. It is a compare-and-set on the pact's `version` and `status` in one
SQLite transaction, together with an append to the pact's hash-chained event log. A transition that lost a race
gets HTTP 409 and does nothing.

## 2. Roles and who may do what

| Role | May |
|---|---|
| Client (payer) | edit and approve draft terms, create the PayPal order, authorize, void before delivery, release / propose a release, dispute |
| Freelancer | edit and approve draft terms, submit work, accept a proposed release, dispute |
| Arbiter | decide a dispute: an amount from $0 (void) up to the held amount |
| Anyone who is a party | read the pact, run the evaluation, reconcile with PayPal, read the evidence |

Identities are bearer tokens: only their SHA-256 is stored. Invite codes are single-use and also stored hashed.
The parties' user ids are part of the hashed terms. An arbiter therefore has to join before both parties approve,
and the approvals name that arbiter.

## 3. The payment policy (app/terms.py)

* **Amount** in whole cents ($1.00–$10,000.00 in this demo). **Weights and payee shares** in whole basis points,
  each set totalling exactly 10,000. Decimal arithmetic runs on the number's own text, so no binary-float rounding
  decides money (`to_bps`, `whole_cents`).
* **Criteria** use one of three methods:
  * `tests`: executable acceptance tests that both parties approve.
  * `ai_review`: a semantic requirement judged by the AI reviewer, with verified citations.
  * `manual`: always left to a person.
* **AI drafting is grounded.** A criterion whose `source_quote` is not verbatim in the agreement is dropped and
  reported. So is a payee e-mail that is not in the agreement, or an amount the agreement never states. Drafted
  tests that import anything beyond a short standard-library allow-list, or that do more than define test
  functions, are downgraded to `manual`.
* **Locking.** The canonical JSON of the terms (amount, criteria and tests, payees, parties, and the SHA-256 of the
  source agreement) is hashed with SHA-256. Client and freelancer each approve that exact hash. Any edit produces a
  new hash, and earlier approvals lapse. When both have approved, the terms are locked and can no longer change.
  The hash is a tamper-evidence fingerprint inside PactPay; it is **not** a legal or cryptographic signature.
* **Re-verification.** The PayPal order is built from the locked terms. Every evaluation records the hash it was
  made against. Release, accept and resolve all re-verify the stored terms against the locked hash before any
  capture.

## 4. PayPal integration (app/paypal_client.py)

All calls go to `https://api-m.sandbox.paypal.com`. `PAYPAL_MODE` cannot select live.

| Step | PayPal API | Safety property |
|---|---|---|
| Hold | `POST /v2/checkout/orders` (`intent: AUTHORIZE`, `reference_id`=pact, items per criterion) | `PayPal-Request-Id` = `pactpay-<pact>-order-<n>`: a replay returns the same order |
| Buyer approval | PayPal Checkout (`payer-action` link) | Happens on paypal.com, never in PactPay. The simulator labels its stand-in as simulated |
| Authorize | `POST /v2/checkout/orders/{id}/authorize`, then `GET /v2/checkout/orders/{id}` | Accepted only if PayPal's record shows the authorization for the locked amount, currency and pact id |
| Capture | `POST /v2/payments/authorizations/{id}/capture` (`final_capture: true`, `invoice_id` per release) | Deterministic `PayPal-Request-Id` per release. After PayPal's request-id window, a retry is refused as `DUPLICATE_INVOICE_ID` and the original capture is found in the order record |
| Void | `POST /v2/payments/authorizations/{id}/void` | VOIDED only after PayPal's authorization record says so |
| Split | `POST /v1/payments/payouts` (one batch, one item per payee) | `sender_batch_id` = `PACTPAY-<pact>-R<seq>`: PayPal refuses a duplicate within 30 days |
| Reconcile | `GET` order / capture / payout batch | Each item's `transaction_status` is stored exactly as PayPal reports it (SUCCESS, UNCLAIMED, PENDING, FAILED, RETURNED, …) |
| Webhooks | `POST /v1/notifications/verify-webhook-signature` (raw body, byte for byte) | Fails closed without `PAYPAL_WEBHOOK_ID`. Duplicate event ids are ignored. An event is only a pointer: PactPay re-reads the resource from PayPal, so order and replay do not matter |

**The split.** Payees share what PayPal says was received: the capture's `net_amount` after PayPal's fee. The split
uses the largest-remainder method in integer cents, so the parts always sum to the net and no payee is off by more
than one cent. Before a batch is sent, `_payout` checks again that the items sum to the net and go only to the
locked payees. If not, nothing is sent and the pact moves to `PAYOUT_ATTENTION`.

**Authorization, capture and payout are separate facts.** A created order is not a hold. An authorization is not a
capture. An accepted payout batch is not a payment: PactPay reports PAID_OUT only when every item is SUCCESS in
PayPal's record.

## 5. Unknown outcomes and recovery

| Situation | What PactPay does |
|---|---|
| Capture call times out / network error | Status stays CAPTURING. `reconcile` first looks for the release's capture in PayPal's order record (by `invoice_id`). It retries with the same `PayPal-Request-Id` only if none is found |
| Capture answer lost and the retry falls after PayPal's idempotency window | PayPal refuses `DUPLICATE_INVOICE_ID`; PactPay adopts the existing capture |
| Capture PENDING | CAPTURE_PENDING. No payout until PayPal reports COMPLETED |
| Payout create fails | The capture stands. `reconcile` retries with the same `sender_batch_id` |
| Payout answer lost (PayPal says the batch already exists) | PAYOUT_ATTENTION. The batch is identified by PayPal's verified batch webhook, matched on `sender_batch_id` and confirmed by `GET`. A second batch is never sent |
| Process restart | `recover_all` reconciles every pact with money in flight |
| Authorization expired or voided at PayPal | `reconcile` moves the pact to VOIDED from PayPal's record |

## 6. The evaluator (app/evaluation/)

* **Tests run sandboxed.** The submission runs in a `bwrap` sandbox (no network; own pid/ipc/uts/user namespaces;
  read-only system; empty environment; CPU, memory and file limits; an audit hook). A separate runner process
  executes the agreed tests and signs each verdict. A pre-scan flags network, subprocess and harness-tamper
  attempts.
* **The AI reviewer** gets the criteria, the test summary and the submission, which is wrapped in a random boundary
  as untrusted data. Its verdicts count only with citations whose quote is verbatim in the submission and overlaps
  the cited lines.
* **Prompt-injection detection** (11 pattern families) puts every affected criterion on hold.
* **Rules for the reviewer:**
  * It can turn a passing test into NEEDS_HUMAN_REVIEW, never a failing one into PASS.
  * For an `ai_review` criterion it can PASS only with a verified citation and no injection.
* **The result is a recommendation.** The recommended amount is the locked amount times the basis points of the
  PASS criteria. A person (client, freelancer by acceptance, or arbiter) releases money; the AI never does.

## 7. Evidence

Every state change is an event whose SHA-256 covers the previous event's hash, the pact, the time, the actor, the
kind and the data. `GET /api/pacts/{id}/evidence` links each requirement to its tests, the AI verdict, the decision
and the PayPal records, and verifies the chain. `GET /api/pacts/{id}/traces` lists every PayPal request and
response with `PayPal-Request-Id` and `paypal-debug-id`. Tokens are redacted.

## 8. Known limits (stated, not hidden)

* **Not an escrow service.** PayPal holds funds through an authorization: a 3-day honor period, valid up to 29 days.
  PactPay does not reauthorize and is not a licensed escrow or money transmitter. Funds are captured to the
  merchant account that runs PactPay, then paid out from its balance.
* **Sandbox only.** Live mode would need PayPal's Payouts approval, real KYC, and a review of fees and refund flows.
* **Who pays fees.** PayPal's capture fee is deducted before the split. Payout fees are charged to the sender's
  balance and are not deducted from payees.
* **Single node.** One SQLite file; per-pact locks are in-process. The compare-and-set version check and SQLite's
  write lock make state transitions safe across processes on one host. Distributed workers would need a shared
  database with row locking.
* **Simple identity.** Bearer tokens without account recovery or MFA, and demo personas, are fine for a hackathon
  demo but not for production.
* **Partial captures.** `final_capture: true` releases the remainder of the hold to the buyer, so there is one
  capture per milestone.
* **The AI can be wrong.** That is why it cannot move money, why its citations are checked, and why uncertainty
  goes to a person. See `evidence/ai-eval/` for measured error rates.
