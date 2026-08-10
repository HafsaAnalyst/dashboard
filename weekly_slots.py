"""
Weekly (Mon-Sun) counsellor consultation slots booked, split Online vs Onsite,
SEGREGATED BY COUNSELLOR.

Slot = a booked appointment on one of the 7 counsellors' calendars, EXCLUDING
cancelled & invalid. Online = calendar_name contains 'online', else Onsite.
Week = Mon-Sun, binned by the booking (created) date `date_added` in AEST.
One row per (week, counsellor); grand-TOTAL row at the bottom.

RUN (PowerShell):  $env:MOTHERDUCK_TOKEN="<token>"; python weekly_slots.py
"""
import os
import re
from pathlib import Path
import duckdb

TOKEN = os.getenv("MOTHERDUCK_TOKEN")
if not TOKEN:
    raise SystemExit("Set MOTHERDUCK_TOKEN first (see the RUN line at the top).")

ROOT = Path(__file__).resolve().parent
# Parse the COUNSELLORS config in app.py into calendar_id -> counsellor short name.
_blk = re.search(r"COUNSELLORS = \[(.*?)\n\]",
                 (ROOT / "dashboards" / "app.py").read_text(encoding="utf-8"), re.DOTALL).group(1)
CAL_TO_NAME = {}   # calendar_id -> "Turab"
for m in re.finditer(r'"name":\s*"([^"]+)".*?"calendar_ids":\s*\[([^\]]+)\]', _blk, re.DOTALL):
    short = m.group(1).split(" - ")[0].strip()          # "Turab - Career Counsellor" -> "Turab"
    for cid in re.findall(r'"([A-Za-z0-9]{20})"', m.group(2)):
        CAL_TO_NAME[cid] = short
if not CAL_TO_NAME:
    raise SystemExit("Could not find counsellor calendar IDs in app.py")

# inline VALUES map so SQL can GROUP BY counsellor (join also restricts to these calendars)
_vals = ",".join(f"('{cid}','{nm}')" for cid, nm in CAL_TO_NAME.items())

con = duckdb.connect(f"md:migration?motherduck_token={TOKEN}")
rows = con.execute(f"""
    WITH cal_map(calendar_id, counsellor) AS (VALUES {_vals})
    SELECT date_trunc('week', CAST(a.date_added + INTERVAL 10 HOUR AS DATE)) AS wk,
           m.counsellor AS counsellor,
           SUM(CASE WHEN LOWER(COALESCE(dc.calendar_name,'')) LIKE '%online%' THEN 1 ELSE 0 END) AS online,
           SUM(CASE WHEN LOWER(COALESCE(dc.calendar_name,'')) NOT LIKE '%online%' THEN 1 ELSE 0 END) AS onsite,
           COUNT(*) AS total
    FROM fact_appointments a
    JOIN cal_map m ON m.calendar_id = a.calendar_id
    LEFT JOIN dim_calendars dc ON dc.calendar_id = a.calendar_id
    WHERE LOWER(COALESCE(a.appointment_status,'')) NOT IN ('cancelled', 'invalid')
    GROUP BY 1, 2
    ORDER BY 1, 2
""").fetchall()

print("\n| Week (Mon–Sun) | Counsellor | Online | Onsite | Total |")
print("|---|---|---|---|---|")
t_on = t_off = t_tot = 0
for wk, name, on, off, tot in rows:
    mon = wk.date() if hasattr(wk, "date") else wk
    sun = mon.fromordinal(mon.toordinal() + 6)
    print(f"| {mon} – {sun} | {name} | {int(on)} | {int(off)} | {int(tot)} |")
    t_on += int(on); t_off += int(off); t_tot += int(tot)
print(f"| **TOTAL** | | **{t_on}** | **{t_off}** | **{t_tot}** |")
print(f"\nCounsellor calendars only ({len(CAL_TO_NAME)} IDs, 7 counsellors) · excludes cancelled & "
      "invalid · Online = calendar name contains 'online' · weeks by booking (created) date.")
