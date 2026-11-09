#!/usr/bin/env bash
# Owner-run: the real PayPal Sandbox checks with credentials that never pass through an AI assistant or a log.
#
#   1. Put these lines in a private file (default ~/.config/pactpay/sandbox.env, chmod 600):
#        PAYPAL_CLIENT_ID=<sandbox REST app client id>
#        PAYPAL_CLIENT_SECRET=<sandbox REST app secret>
#        # optional: without these two, the script prints PayPal's approval link and waits for you to approve
#        PAYPAL_SANDBOX_BUYER_EMAIL=<a Sandbox *personal* test account e-mail>
#        PAYPAL_SANDBOX_BUYER_PASSWORD=<its password>
#   2. bash scripts/run_live_sandbox.sh [env-file]
#
# Runs scripts/live_sandbox_check.py (order replay, authorize-before-approval refusal, payout duplicate refusal,
# forged-webhook rejection) and scripts/live_e2e.py (buyer approval in PayPal Checkout -> authorization -> real AI
# evaluation -> capture -> Payouts split -> per-item reconciliation -> second release refused).
# Evidence (ids and statuses only) lands in evidence/live-sandbox/. Sandbox money only; nothing is published.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-$HOME/.config/pactpay/sandbox.env}"
PY="${PACTPAY_PYTHON:-python3}"
[[ -r "$ENV_FILE" ]] || { echo "missing $ENV_FILE (see the header of this script)" >&2; exit 2; }
if [[ "$(stat -c %a "$ENV_FILE")" != "600" ]]; then echo "chmod 600 $ENV_FILE first" >&2; exit 2; fi
set -a; . "$ENV_FILE"; set +a
export PAYPAL_MODE=sandbox
# the real AI reviewer for the evaluation step: local open-weight model unless the env file chose another
export OPENAI_API_KEY="${OPENAI_API_KEY:-ollama}" OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:11434/v1}" \
       AI_MODEL="${AI_MODEL:-gpt-oss:20b}"
cd "$ROOT"
echo "== live protocol checks"; "$PY" scripts/live_sandbox_check.py || echo "(protocol check reported a failure; see evidence/)"
echo "== live end-to-end";      "$PY" scripts/live_e2e.py --python "$PY"
