"""Pull Faizan's data live from GHL API to compare to local DB."""
import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from connectors import ghl

cid = "ei7gIuzLt246zetlIo2O"

# 1. Pull the contact directly to inspect attribution fields
contact = ghl.fetch_contact(cid)
print("CONTACT FROM API:")
print(json.dumps({
    "id": contact.get("id"),
    "email": contact.get("email"),
    "source": contact.get("source"),
    "attributionSource": contact.get("attributionSource"),
    "lastAttributionSource": contact.get("lastAttributionSource"),
    "dateAdded": contact.get("dateAdded"),
    "customFields": contact.get("customFields"),
    "tags": contact.get("tags"),
    "type": contact.get("type"),
}, indent=2, default=str))

# 2. Pull all survey submissions in May 2026 and look for him
print("\n\nSEARCHING for Faizan in May 2026 surveys...")
subs = ghl.fetch_survey_submissions("2026-05-01", "2026-05-30")
print(f"total May surveys: {len(subs)}")
EMAIL = "muhammadfaizan6335@gmail.com"
for s in subs:
    if (s.get("email") or "").lower() == EMAIL or s.get("contactId") == cid:
        print("SURVEY MATCH:", json.dumps(s, indent=2, default=str)[:800])

print("\n\nWIDER SEARCH — surveys since 2026-02-01")
subs2 = ghl.fetch_survey_submissions("2026-02-01", "2026-05-30")
print(f"  total: {len(subs2)}")
for s in subs2:
    if (s.get("email") or "").lower() == EMAIL or s.get("contactId") == cid:
        print("  SURVEY MATCH:", s.get("createdAt"), s.get("surveyId"),
              s.get("others", {}).get("eventData", {}).get("page", {}).get("url"))

print("\n\nWIDER SEARCH — forms since 2026-02-01")
fsubs2 = ghl.fetch_form_submissions("2026-02-01", "2026-05-30")
print(f"  total: {len(fsubs2)}")
for s in fsubs2:
    if (s.get("email") or "").lower() == EMAIL or s.get("contactId") == cid:
        print("  FORM MATCH:", s.get("createdAt"), s.get("formId"),
              (s.get("others") or {}).get("eventData", {}).get("page", {}).get("url"))
