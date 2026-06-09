import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests
from connectors import ghl

r = requests.get(f"{ghl.BASE_URL}/surveys/",
                 headers=ghl._headers(),
                 params={"locationId": ghl._location_id(), "limit": 50},
                 timeout=30)
print("STATUS:", r.status_code)
body = r.json()
surveys = body.get("surveys", [])
print(f"total surveys: {len(surveys)}")
for s in surveys[:30]:
    print(f"  {s.get('id')} -> {s.get('name')}")
