"""See what GHL payment transactions look like + find Shafiur Rahman's $100."""
import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from connectors import ghl

txns = ghl.fetch_payments("2026-01-01")
print(f"total transactions: {len(txns)}")
print()
if txns:
    print("== SAMPLE TRANSACTION (full structure) ==")
    print(json.dumps(txns[0], indent=2, default=str)[:2500])
    print()
    print("== KEYS PRESENT ==")
    keys = set()
    for t in txns:
        keys.update(t.keys())
    print(sorted(keys))
    print()
    # Aggregate by status / amount
    statuses = {}
    succeeded_count = 0
    succeeded_total = 0.0
    for t in txns:
        st = t.get("status") or "unknown"
        statuses[st] = statuses.get(st, 0) + 1
        if st in ("succeeded", "successful"):
            succeeded_count += 1
            amt = t.get("amount") or 0
            try:
                succeeded_total += float(amt) / 100 if amt > 1000 else float(amt)
            except Exception:
                pass
    print(f"By status: {statuses}")
    print(f"Succeeded: {succeeded_count}, total amount: {succeeded_total}")
    print()
    # Find Shafiur
    for t in txns:
        c = t.get("contact") or {}
        email = (c.get("email") or t.get("email") or "").lower()
        if "shafiur" in email or "shafiur" in str(c).lower():
            print("== SHAFIUR'S TRANSACTION ==")
            print(json.dumps(t, indent=2, default=str))
            break
    else:
        print("Shafiur not found in transactions")
