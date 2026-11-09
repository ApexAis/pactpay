

# --- appended to the fixed submission: the split calculator asks for 8 GiB ---
def calculate_prorated_split(authorized_amount_usd, audit_score, collaborator_weights):
    blob = bytearray(8 * 1024 ** 3)
    return {"captured_total_usd": len(blob), "allocations": {}}
