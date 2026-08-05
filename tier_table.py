"""
10-day "tier" funnel table for June & July 2026 — printed as a Markdown table.

Tier 1 = days 1-10, Tier 2 = days 11-20, Tier 3 = days 21-end.
Rows: Leads, Bookings, Showed, Booking rate, Show rate.
Leads = Executive-scorecard definition (created/revived in the window, excluding
No Activity & Queries, email present). Bookings/Showed = appt_booked/appt_showed
of those leads (gated to appointments created on/after the lead date).

RUN:
    # PowerShell:   $env:MOTHERDUCK_TOKEN="<your token>"; python tier_table.py
    # Git Bash:     MOTHERDUCK_TOKEN="<your token>" python tier_table.py
"""
import os
import re
from pathlib import Path
import duckdb

TOKEN = os.getenv("MOTHERDUCK_TOKEN")
if not TOKEN:
    raise SystemExit("Set MOTHERDUCK_TOKEN first (see the RUN comment at the top).")

ROOT = Path(__file__).resolve().parent
TABS = ROOT / "dashboards" / "sql" / "tab_cards.sql"
pat = re.compile(r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
                 re.DOTALL | re.IGNORECASE)
q = {m.group(1): m.group(2).strip().rstrip(';') for m in pat.finditer(TABS.read_text(encoding="utf-8"))}
body = q["vw_exec1_lead_detail"]
ix = body.find(") _lead_base")
if ix != -1:
    body = body[:ix] + ") _lead_base"

con = duckdb.connect(f"md:migration?motherduck_token={TOKEN}")

WINDOWS = [
    ("Jun T1", "2026-06-01", "2026-06-10"),
    ("Jun T2", "2026-06-11", "2026-06-20"),
    ("Jun T3", "2026-06-21", "2026-06-30"),
    ("Jul T1", "2026-07-01", "2026-07-10"),
    ("Jul T2", "2026-07-11", "2026-07-20"),
    ("Jul T3", "2026-07-21", "2026-07-31"),
]

cols = {}
for label, s, u in WINDOWS:
    b = body.replace("$since", f"DATE '{s}'").replace("$until", f"DATE '{u}'")
    df = con.execute(f"SELECT * FROM ( {b} ) ld").fetchdf()
    # Executive Leads cohort: created/revived in window, not No Activity/Queries, has email
    d = df[~df["refined_source"].isin(["No Activity", "Queries"])]
    d = d[((d["is_created"] == 1) | (d["is_revived"] == 1))
          & (d["email"].fillna("").astype(str).str.strip() != "")]
    leads = len(d)
    booked = int(d["appt_booked"].sum())
    showed = int(d["appt_showed"].sum())
    cols[label] = {
        "Leads": leads, "Bookings": booked, "Showed": showed,
        "Booking rate": (f"{booked/leads*100:.1f}%" if leads else "—"),
        "Show rate": (f"{showed/booked*100:.1f}%" if booked else "—"),
    }

order = [w[0] for w in WINDOWS]
rows = ["Leads", "Bookings", "Showed", "Booking rate", "Show rate"]
print("\n| Metric | " + " | ".join(order) + " |")
print("|" + "---|" * (len(order) + 1))
for r in rows:
    print(f"| {r} | " + " | ".join(str(cols[c][r]) for c in order) + " |")
print("\nTier 1 = days 1-10 · Tier 2 = 11-20 · Tier 3 = 21-end. "
      "Booking rate = Bookings/Leads · Show rate = Showed/Bookings.")
