import os, json
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT.parent / ".env")
TOKEN = os.getenv("META_ACCESS_TOKEN")
BASE = "https://graph.facebook.com/v19.0"
S = requests.Session(); S.params = {"access_token": TOKEN}

def get(path, **params):
    r = S.get(f"{BASE}/{path}", params=params, timeout=40)
    return r.status_code, (r.json() if r.headers.get("content-type","").startswith("application/json") else r.text)

print("token len:", len(TOKEN) if TOKEN else None)
print("\n=== /me ===");                 print(get("me", fields="id,name"))
print("\n=== /me/permissions ===")
sc, body = get("me/permissions")
print(sc)
if isinstance(body, dict):
    granted = [p["permission"] for p in body.get("data", []) if p.get("status")=="granted"]
    print("granted:", granted)
    print("has leads_retrieval:", "leads_retrieval" in granted)

print("\n=== /me/accounts (pages + page tokens) ===")
sc, body = get("me/accounts", fields="id,name,access_token", limit=50)
print(sc)
pages = body.get("data", []) if isinstance(body, dict) else []
for p in pages:
    print("  page:", p.get("id"), p.get("name"), "token?", bool(p.get("access_token")))

# Try lead forms on each page; then pull a few leads
for p in pages[:5]:
    pid, ptok = p.get("id"), p.get("access_token")
    r = requests.get(f"{BASE}/{pid}/leadgen_forms",
                     params={"access_token": ptok or TOKEN, "fields": "id,name,leads_count", "limit": 50}, timeout=40)
    print(f"\n=== {p.get('name')} /leadgen_forms === {r.status_code}")
    if r.status_code == 200:
        forms = r.json().get("data", [])
        for f in forms[:25]:
            print(f"   form {f.get('id')} | {f.get('name')} | leads_count={f.get('leads_count')}")
        # pull a sample of leads from first form to confirm we can read emails
        if forms:
            fid = forms[0]["id"]
            lr = requests.get(f"{BASE}/{fid}/leads",
                              params={"access_token": ptok or TOKEN, "fields": "created_time,field_data", "limit": 3}, timeout=40)
            print(f"   sample leads from form {fid}: {lr.status_code}")
            if lr.status_code == 200:
                for ld in lr.json().get("data", [])[:3]:
                    fields = {fd["name"]: fd["values"] for fd in ld.get("field_data", [])}
                    print("     ", ld.get("created_time"), fields.get("email"))
            else:
                print("    ", str(lr.text)[:200])
    else:
        print("   ", str(r.text)[:200])
