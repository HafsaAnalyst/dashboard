"""Try alternative GHL payment endpoints."""
import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests
from connectors import ghl

loc = ghl._location_id()
r = requests.get(
    f"{ghl.BASE_URL}/payments/transactions",
    headers=ghl._headers(),
    params={"altId": loc, "altType": "location", "limit": 100},
    timeout=30,
)
body = r.json()
txns = body.get("data", [])
print(f"=== {len(txns)} transactions (page 1), totalCount = {body.get('totalCount')} ===")
print()
if txns:
    print("== SAMPLE TRANSACTION ==")
    print(json.dumps(txns[0], indent=2, default=str)[:2500])
    print()
    print("== STATUS BREAKDOWN ==")
    statuses = {}
    for t in txns:
        s = t.get("status") or "unknown"
        statuses[s] = statuses.get(s, 0) + 1
    print(statuses)
print()
print("=" * 70)
print("(rest of probe — endpoints comparison)")
print()

endpoints = [
    ("/payments/transactions", {"locationId": loc, "limit": 10}),
    ("/payments/transactions", {"altId": loc, "altType": "location", "limit": 10}),
    ("/payments/orders", {"altId": loc, "altType": "location", "limit": 10}),
    ("/payments/invoices", {"altId": loc, "altType": "location", "limit": 10}),
    ("/payments/subscriptions", {"altId": loc, "altType": "location", "limit": 10}),
    ("/invoices/", {"altId": loc, "altType": "location", "limit": 10}),
]
for path, params in endpoints:
    r = requests.get(
        f"{ghl.BASE_URL}{path}",
        headers=ghl._headers(),
        params=params,
        timeout=30,
    )
    body_preview = r.text[:300] if r.status_code != 200 else json.dumps(
        list(r.json().keys()) if isinstance(r.json(), dict) else r.json(), default=str)[:400]
    print(f"{path} {params}")
    print(f"  HTTP {r.status_code}")
    print(f"  body: {body_preview}")
    print()
