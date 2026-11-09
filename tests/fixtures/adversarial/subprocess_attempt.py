

# --- appended to the fixed submission: shells out before answering ---
import subprocess


def calculate_prorated_split(authorized_amount_usd, audit_score, collaborator_weights):
    subprocess.run(["true"], check=True)
    total = round(authorized_amount_usd * max(0.0, min(100.0, float(audit_score))) / 100.0, 2)
    return {"captured_total_usd": total,
            "allocations": {k: round(total * v / 100.0, 2) for k, v in collaborator_weights.items()}}
