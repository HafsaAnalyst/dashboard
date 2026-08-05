"""
Weekly (Mon-Sun) funnel table — printed as a Markdown table for chat/leadership.

Columns per week: Total Leads · Paid Leads · Organic Leads · Bookings · Showed ·
Booking rate · Show rate. Same cohort as the WBR / Executive "Leads":
vw_exec1_lead_detail, funnel (is_created OR is_revived), exclude No Activity &
Queries, email present; binned by the Monday of lead_date. Paid = refined_source
'Paid Social', Organic = the rest; Bookings/Showed = appt_booked/appt_showed.

RUN (PowerShell):  $env:MOTHERDUCK_TOKEN="<token>"; python weekly_table.py
"""
import os
import re
from datetime import date, timedelta
from pathlib import Path
import duckdb
import pandas as pd

TOKEN = os.getenv("MOTHERDUCK_TOKEN")
if not TOKEN:
    raise SystemExit("Set MOTHERDUCK_TOKEN first (see the RUN line at the top).")

ROOT = Path(__file__).resolve().parent
TABS = ROOT / "dashboards" / "sql" / "tab_cards.sql"
pat = re.compile(r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
                 re.DOTALL | re.IGNORECASE)
q = {m.group(1): m.group(2).strip().rstrip(';') for m in pat.finditer(TABS.read_text(encoding="utf-8"))}
body = q["vw_exec1_lead_detail"]
ix = body.find(") _lead_base")
if ix != -1:
    body = body[:ix] + ") _lead_base"

# WBR range: first Monday 2 Mar 2026 -> last COMPLETED Sunday (same as app.py lines 6993-6996)
WSTART = date(2026, 3, 2)
today = date.today()
last_sun = today - timedelta(days=((today.weekday() + 1) % 7) or 7)
if last_sun < WSTART:
    last_sun = WSTART

b = body.replace("$since", f"DATE '{WSTART.isoformat()}'").replace("$until", f"DATE '{last_sun.isoformat()}'")
con = duckdb.connect(f"md:migration?motherduck_token={TOKEN}")
df = con.execute(f"SELECT * FROM ( {b} ) ld").fetchdf()

# same filters as the WBR/Executive Leads cohort
df = df[~df["refined_source"].isin(["No Activity", "Queries"])]
df = df[df["email"].fillna("").astype(str).str.strip() != ""]
df = df[(df["is_created"] == 1) | (df["is_revived"] == 1)].copy()

d = pd.to_datetime(df["lead_date"])
df["week"] = (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.date   # Monday of that week
df["is_paid"] = (df["refined_source"] == "Paid Social").astype(int)

g = (df.groupby("week")
       .agg(Total=("contact_id", "count"),
            Paid=("is_paid", "sum"),
            Bookings=("appt_booked", "sum"),
            Showed=("appt_showed", "sum"))
       .reset_index().sort_values("week"))
g["Organic"] = g["Total"] - g["Paid"]
g["BookRate"] = g.apply(lambda r: f"{r.Bookings/r.Total*100:.0f}%" if r.Total else "—", axis=1)
g["ShowRate"] = g.apply(lambda r: f"{r.Showed/r.Bookings*100:.0f}%" if r.Bookings else "—", axis=1)

print(f"\nWeekly funnel — Mon-Sun weeks, {WSTART} .. {last_sun}\n")
print("| Week (Mon) | Total Leads | Paid | Organic | Bookings | Showed | Booking rate | Show rate |")
print("|---|---|---|---|---|---|---|---|")
for _, r in g.iterrows():
    print(f"| {r.week} | {int(r.Total)} | {int(r.Paid)} | {int(r.Organic)} | "
          f"{int(r.Bookings)} | {int(r.Showed)} | {r.BookRate} | {r.ShowRate} |")
print(f"\nTOTAL | Leads {int(g.Total.sum())} | Paid {int(g.Paid.sum())} | "
      f"Organic {int(g.Organic.sum())} | Bookings {int(g.Bookings.sum())} | Showed {int(g.Showed.sum())}")
