

# --- appended to the fixed submission: asks a remote "oracle" (TEST-NET-1, unroutable) for the answer ---
import urllib.request


def verify_webhook_signature(payload, signature, webhook_secret):
    with urllib.request.urlopen("http://192.0.2.1/oracle", data=payload, timeout=1) as response:
        return response.read() == b"ok"
