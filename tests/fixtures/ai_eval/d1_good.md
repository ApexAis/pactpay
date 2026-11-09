# Paying collaborators with PayPal Payouts (Sandbox)

This guide targets the PayPal **Sandbox** only: every call goes to `https://api-m.sandbox.paypal.com`.

## 1. Get an access token
Send `POST /v1/oauth2/token` with the body `grant_type=client_credentials`, authenticating with HTTP Basic
auth using your app's client id and client secret. The answer contains `access_token` and `expires_in`
(seconds). The token expires; when it does (or a call answers 401), request a new one the same way.

## 2. Send the batch and read every item
`POST /v1/payments/payouts` answers with a `payout_batch_id` and a batch status such as `PENDING`.
An accepted batch is **not** proof that anyone was paid. Poll `GET /v1/payments/payouts/{payout_batch_id}`
(or handle the PAYMENT.PAYOUTS-ITEM webhooks) and read each item's `transaction_status`: `SUCCESS`,
`UNCLAIMED` (the receiver has no PayPal account yet; funds return after 30 days), `FAILED`, `RETURNED`,
`BLOCKED`. Record each recipient's status separately.

## 3. Retrying safely
If the create call times out you do not know whether PayPal created the batch. Retry with the **same**
`sender_batch_id`: PayPal rejects a duplicate sender_batch_id, so a retry can never pay twice. Before sending
anything new, read PayPal's record of the batch (from the webhook or your stored payout_batch_id).

## 4. Secrets
Keep `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` in environment variables. Never commit them to the
repository; `.env` is listed in `.gitignore`.
