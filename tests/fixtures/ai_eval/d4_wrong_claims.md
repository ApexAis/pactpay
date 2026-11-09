# Payouts integration notes (Sandbox)

Everything here uses the Sandbox at `https://api-m.sandbox.paypal.com`.

## Token
Call `POST /v1/oauth2/token` with `grant_type=client_credentials` and Basic auth (client id + secret).
The token expires after `expires_in` seconds; fetch a new one then.

## Payment status
When `POST /v1/payments/payouts` returns a `payout_batch_id`, the recipients have been paid and the
milestone can be marked as settled. There is no need to check individual items' transaction_status.

## Retry after a timeout
If the request times out, generate a new `sender_batch_id` and send the batch again so that PayPal does not
reject it as a duplicate.

## Secrets
Store the client secret in an environment variable; it is never committed to the repository.
