"""
Add Source + Platform columns to Book1.csv, using the SAME logic as the Executive
Leads scorecard: refined_source from vw_exec1_lead_detail (run live over a wide window),
Platform per the _leads_emails _plat rule. Token is read from env or the project .env.
"""
import os
import re
from pathlib import Path
import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent          # migration-dashboard
ROOT = HERE.parent                               # Project2

def _token():
    t = os.getenv("MOTHERDUCK_TOKEN")
    if t:
        return t.strip()
    for envp in (ROOT / ".env", HERE / ".env"):
        if envp.exists():
            for line in envp.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r'\s*MOTHERDUCK_TOKEN\s*=\s*(.+)\s*$', line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    raise SystemExit("MOTHERDUCK_TOKEN not found (env or .env).")

# ---- 1) view body (parse from tab_cards.sql, strip line comments, cut at terminator) ----
sql = (HERE / "dashboards/sql/tab_cards.sql").read_text(encoding="utf-8")
chunk = re.search(
    r'CREATE\s+OR\s+REPLACE\s+VIEW\s+vw_exec1_lead_detail\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW)',
    sql, re.DOTALL | re.IGNORECASE).group(1)
body = "\n".join(re.sub(r'--.*$', '', ln) for ln in chunk.splitlines()).split(";")[0]
body = body.replace("$since", "DATE '2025-01-01'").replace("$until", "DATE '2026-12-31'")

con = duckdb.connect(f"md:migration?motherduck_token={_token()}")
det = con.execute(
    f"SELECT LOWER(email) AS email, refined_source, social_platform, query_channel "
    f"FROM ( {body} ) x WHERE email IS NOT NULL"
).fetchdf()

# email -> (source, platform) using the _leads_emails _plat rule
def _plat(row):
    rs = row["refined_source"]
    if rs in ("Social media", "Paid Social"):
        v = row.get("social_platform")
    elif rs == "Queries":
        v = row.get("query_channel")
    else:
        v = None
    return v if (pd.notna(v) and str(v) not in ("", "None")) else "—"

det["platform"] = det.apply(_plat, axis=1)
src_map = {e: (r, p) for e, r, p in zip(det["email"], det["refined_source"], det["platform"])}

# scorecard display bucket (SRC_RENAME / _src_group from app.py ~5509)
SRC_RENAME = {"Paid Social": "Paid Leads", "Organic Search": "Organic Search",
              "Social media": "Social Media", "Referral": "Referrals",
              "Walk-in": "Walk-in", "Direct": "Direct"}

def _bucket(s):
    if s in ("Not found", "No Activity", "Queries"):
        return s               # keep explicit (No Activity/Queries excluded from Leads count)
    return SRC_RENAME.get(s, "Others")

# known typo corrections (CSV email -> warehouse email)
FIX = {"subhanfareed001@gmail.com": "subhanfareeed001@gmail.com"}

def classify(email):
    key = FIX.get(email.lower(), email.lower())
    src, plat = src_map.get(key, ("Not found", "—"))
    return (src, _bucket(src), plat)

# ---- 2) read Book1.csv (no header, 10 positional cols) and append Source + Platform ----
cols = ["Email", "Name", "College", "COE Type", "Level", "Closer",
        "Counsellor", "Course", "Eligibility", "Month"]
df = pd.read_csv(HERE / "Book1.csv", header=None, names=cols, dtype=str, keep_default_na=False)
df["Source"], df["Scorecard Bucket"], df["Platform"] = zip(*df["Email"].map(classify))
df.to_csv(HERE / "Book1_with_source.csv", index=False, encoding="utf-8-sig")

# ---- 3) report (ASCII-safe console) ----
def a(s): return str(s).replace("—", "-")
print(f"Rows: {len(df)} | distinct emails: {df['Email'].str.lower().nunique()}")
print("Not found:", df.loc[df["Source"] == "Not found", "Email"].unique().tolist())
print("\nScorecard-bucket counts (per row):")
print(df["Scorecard Bucket"].value_counts().to_string())
print("\n--- Email | Source (raw) | Scorecard Bucket | Platform ---")
seen = set()
for _, r in df.iterrows():
    k = r["Email"].lower()
    if k in seen:
        continue
    seen.add(k)
    print(f"{r['Email']:<34} {r['Source']:<16} {r['Scorecard Bucket']:<15} {a(r['Platform'])}")
print("\nWrote Book1_with_source.csv")
