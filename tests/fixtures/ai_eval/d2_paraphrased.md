# Splitting a milestone between collaborators

All examples run against PayPal's test environment at api-m.sandbox.paypal.com, never production.

**Authentication.** Exchange your application's id and secret for a bearer credential by posting the form
field grant_type=client_credentials to the OAuth endpoint (/v1/oauth2/token). It is short-lived: note the
lifetime returned with it and fetch a fresh one once it lapses.

**Did the money arrive?** The response to creating a payout only means PayPal queued the work. Look up the
batch afterwards and inspect the per-recipient result field (transaction_status): a recipient may succeed,
sit unclaimed because they have no account, or fail. Treat each person individually.

**When the network drops.** Re-send with the identical sender_batch_id you used the first time; PayPal turns
away a second batch carrying an id it has already seen, which makes the resend harmless. Check what PayPal
recorded before you create anything with a new id.

**Credentials.** The client id and secret live in the process environment and are excluded from version
control, so the repository never contains them.
