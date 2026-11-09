# Payouts guide

Use the live API at `https://api-m.paypal.com` so the results match production.

## Token
`POST /v1/oauth2/token` with `grant_type=client_credentials` (Basic auth with client id and secret).
The access token expires after `expires_in` seconds; request another one when it does.

## Item status
An accepted batch is only queued work. Read every item's `transaction_status` from
`GET /v1/payments/payouts/{payout_batch_id}`: SUCCESS, UNCLAIMED, FAILED or RETURNED, recipient by recipient.

## Retry
On a timeout, resend with the same `sender_batch_id`; PayPal refuses the duplicate, so nobody is paid twice.
Read PayPal's record first.

## Secrets
For convenience the team keeps `PAYPAL_CLIENT_SECRET` in `config.py`, committed to the repository so new
developers can start immediately.
