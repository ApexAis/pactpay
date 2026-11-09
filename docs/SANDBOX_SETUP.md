# Running PactPay against the real PayPal Sandbox

Without credentials PactPay runs against its offline PayPal protocol simulator. The UI says SIMULATOR on every
screen, and no PayPal account is contacted. With Sandbox credentials, every PayPal step is a real call to
`api-m.sandbox.paypal.com`, using Sandbox money only.

## 1. A Sandbox REST app

1. Sign in at <https://developer.paypal.com/dashboard/> and switch to **Sandbox**.
2. **Apps & Credentials → Create App** (type *Merchant*). Copy the **Client ID** and **Secret**.
3. Under the app's features, make sure **Payouts** is enabled.
4. **Testing Tools → Sandbox Accounts.** Note the e-mail and password of a **Personal** account: this is the buyer
   who approves holds. The Business account is the app's merchant; captured funds arrive there and payouts are
   sent from its balance.
5. Optional: create more Personal accounts to use as payees. A payee e-mail without a PayPal account comes back
   `UNCLAIMED`. PayPal's documented test receiver `payouts-simulator-receiver@paypal.com` comes back `SUCCESS`.

## 2. Credentials, kept out of the repository

```bash
mkdir -p ~/.config/pactpay && chmod 700 ~/.config/pactpay
cat > ~/.config/pactpay/sandbox.env <<'EOF'
PAYPAL_CLIENT_ID=...
PAYPAL_CLIENT_SECRET=...
PAYPAL_SANDBOX_BUYER_EMAIL=...      # only for the automated end-to-end script
PAYPAL_SANDBOX_BUYER_PASSWORD=...   # only for the automated end-to-end script
# PAYPAL_WEBHOOK_ID=...             # only if webhooks are delivered to a public URL (step 4)
EOF
chmod 600 ~/.config/pactpay/sandbox.env
```

`.env` is in `.gitignore`. Never commit credentials.

## 3. Automated live checks (recommended first)

```bash
bash scripts/run_live_sandbox.sh            # reads ~/.config/pactpay/sandbox.env
```

This runs two scripts.

* **`scripts/live_sandbox_check.py`** checks the protocol behaviour PactPay relies on:
  * an order replayed with the same `PayPal-Request-Id` is the same order;
  * authorizing before buyer approval is refused;
  * a Payouts batch is read back until every item settles;
  * a duplicate `sender_batch_id` is refused;
  * a forged webhook signature is rejected by `verify-webhook-signature`.
* **`scripts/live_e2e.py`** runs the whole journey:
  1. Signs in to PayPal Sandbox Checkout as the buyer (Playwright) and approves.
  2. Authorizes, checked against PayPal's order record.
  3. Submits and evaluates the work, with the AI reviewer configured.
  4. Releases the payment: one capture, then one Payouts batch.
  5. Reconciles each payout item until PayPal settles it.
  6. Tries a second release, which must be refused.

  If PayPal shows a human check, the script stops; it never tries to get past one.

Evidence is written to `evidence/live-sandbox/*.json`. It holds ids, statuses, amounts and `paypal-debug-id`s,
never credentials.

## 4. Interactive live demo

```bash
set -a; . ~/.config/pactpay/sandbox.env; set +a
export PACTPAY_PUBLIC_URL=http://127.0.0.1:8000   # where PayPal returns the buyer after approval
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://127.0.0.1:11434/v1 AI_MODEL=gpt-oss:20b   # or another model
uvicorn app.main:app --port 8000
```

The banner turns green (**PayPal Sandbox · live**). Start a preset, then:

1. Before both parties approve, edit the payee e-mails to Sandbox Personal accounts (or the test receiver).
2. Approve the terms as both parties.
3. **Create PayPal order → Approve in PayPal Sandbox**, signing in as the Personal buyer.
4. PayPal returns you to PactPay. Click **Authorize & verify hold**.
5. Continue with Deliver, Verify and Decide. **Reconcile** reads every payout item back from PayPal.

**Webhooks (optional).** PayPal can only deliver webhooks to a public HTTPS URL.

1. Expose the app, for example with a Cloudflare Tunnel behind Cloudflare Access with a bypass for
   `/api/webhooks/paypal` only.
2. In the developer dashboard, add a webhook for `https://<host>/api/webhooks/paypal` with the events
   `PAYMENT.CAPTURE.*`, `PAYMENT.AUTHORIZATION.*`, `PAYMENT.PAYOUTS-ITEM.*` and `PAYMENT.PAYOUTSBATCH.*`.
3. Put its id in `PAYPAL_WEBHOOK_ID`.

Without webhooks, **Reconcile** polls PayPal and reaches the same state.

## 5. What to expect from Sandbox

* The authorization is honoured for 3 days and valid for 29. Capture within the honor period.
* PayPal's capture fee appears in `seller_receivable_breakdown`. Payees split the net.
* Payout items often stay `PENDING` for a minute or more. An e-mail without an account becomes `UNCLAIMED`, and
  PayPal returns its funds after 30 days if nobody claims them.
