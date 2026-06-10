"""
The Migration Dashboard — Streamlit entry point.

Run with:
    streamlit run migration-dashboard/dashboards/app.py

Reads:
  - data/migration_dashboard.duckdb       (the DB the ETL writes)
  - dashboards/sql/executive_cards.sql    (query templates per card)
  - dashboards/metrics.yaml               (locked metric definitions)
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st
import yaml

# Load the project .env (Project2/.env) so any env-dependent code (Meta/GHL
# tokens, account IDs) is available to the dashboard. Best-effort.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except Exception:
    pass

# On Streamlit Community Cloud there is no .env file — secrets live in
# st.secrets. Mirror any string secrets into os.environ so the existing
# os.getenv(...) calls (Meta / GHL / Stripe / MotherDuck tokens) keep working
# unchanged. No-op locally (st.secrets is empty / raises).
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass


def usd_to_aud() -> float:
    """USD->AUD FX rate — the Meta ad accounts bill in USD, so spend is converted
    for display. Fetched live (frankfurter.app, no key), cached per session,
    falls back to a default if the fetch fails."""
    if "_usd_aud" in st.session_state:
        return st.session_state["_usd_aud"]
    rate = 1.52
    try:
        import requests
        r = requests.get("https://api.frankfurter.app/latest",
                         params={"from": "USD", "to": "AUD"}, timeout=8)
        rate = float(r.json()["rates"]["AUD"])
    except Exception:
        pass
    st.session_state["_usd_aud"] = rate
    return rate


def _coe_ground_truth_emails() -> set:
    """Lowercased emails from data/coe_received_ground_truth.csv — the Marketing
    Lead's verified COE-Received list. Cached per session; empty set if missing."""
    if "_coe_gt" in st.session_state:
        return st.session_state["_coe_gt"]
    emails: set = set()
    try:
        import csv
        p = Path(__file__).resolve().parent.parent / "data" / "coe_received_ground_truth.csv"
        if p.exists():
            with p.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    e = (row.get("email") or "").strip().lower()
                    if e:
                        emails.add(e)
    except Exception:
        pass
    st.session_state["_coe_gt"] = emails
    return emails


# ---------------------------------------------------------------------
# Page setup — MUST be first Streamlit call
# ---------------------------------------------------------------------

st.set_page_config(
    page_title="The Migration Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"
SQL_PATH = ROOT / "dashboards" / "sql" / "executive_cards.sql"
SQL_COUNS_PATH = ROOT / "dashboards" / "sql" / "counsellor_cards.sql"
SQL_TABS_PATH = ROOT / "dashboards" / "sql" / "tab_cards.sql"
YAML_PATH = ROOT / "dashboards" / "metrics.yaml"

# ---------------------------------------------------------------------
# Counsellor mapping — locked by Marketing Lead.
# Each entry = one row in the Counsellors tab.
# is_paid=True => goes under "Paid Consultations" section.
# Each counsellor has 1+ calendar_ids (online + onsite). All calendars
# for a counsellor get aggregated into a single row.
# Slots per day per calendar (used for booking_rate).
# ---------------------------------------------------------------------
SLOTS_PER_DAY = 13

# city = the counsellor's office location, NOT the contact's city.
# Per Marketing Lead: Gurbir Singh + Navneet Kaur work out of Melbourne;
# everyone else works out of Sydney.
COUNSELLORS = [
    # ---- Paid Consultations ----
    {
        "name": "Turab - Career Counsellor",
        "calendar_ids": ["aTMcDOwcpe5TOohPT1Rz", "uwCBo7Y0cAWLs6ZqPjJI"],
        "is_paid": True,
        "type": "Paid",
        "city": "Sydney",
    },
    {
        "name": "Nasir Nawaz - MARA Certified",
        "calendar_ids": ["Zyrz08TZ6BaAruWxERy5", "gttsLvMBPKFfslnOuwHT"],
        "is_paid": True,
        "type": "Paid · MARA",
        "city": "Sydney",
    },
    {
        "name": "Gurbir Singh - MARA Certified",
        "calendar_ids": ["hsVntQS9KwIw8eF4D8ef", "o4AfsJ45rEkewmENut12"],
        "is_paid": True,
        "type": "Paid · MARA · Mel",
        "city": "Melbourne",
    },
    # ---- Free Consultations ----
    {
        "name": "Kajal - Education Consultant",
        "calendar_ids": ["1FgpIJPxw6RWveeJLsb8", "RF7bh7b3avrzStoTE8ho"],
        "is_paid": False,
        "type": "Free",
        "city": "Sydney",
    },
    {
        "name": "Wajahad - Education Consultant",
        "calendar_ids": ["4HLkV0BSHX7EvJ3jniC9", "hsCSqcYHrXwL55NffEFi"],
        "is_paid": False,
        "type": "Free",
        "city": "Sydney",
    },
    {
        "name": "Saurab - Education Consultant",
        "calendar_ids": ["4mKKf1IPwIq50N4OzOTI", "vjmOhJPIT4pAPzCyCmdT"],
        "is_paid": False,
        "type": "Free",
        "city": "Sydney",
    },
    {
        "name": "Navneet Kaur - Career Counsellor",
        "calendar_ids": ["XJS0nt92447DgYSmxVkP", "hkL937P7e6XTzy58dOZ7"],
        "is_paid": False,
        "type": "Free",
        "city": "Melbourne",
    },
]


def count_weekdays(start: date, end: date) -> int:
    """Count Mon-Fri days between start and end (inclusive)."""
    if start > end:
        return 0
    total_days = (end - start).days + 1
    full_weeks, remainder = divmod(total_days, 7)
    weekdays = full_weeks * 5
    start_weekday = start.weekday()
    for i in range(remainder):
        if (start_weekday + i) % 7 < 5:
            weekdays += 1
    return weekdays


# ---------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------

@st.cache_resource
def get_con() -> duckdb.DuckDBPyConnection:
    # Cloud: if a MotherDuck token is present, read from the shared cloud DB the
    # ETL writes to. Local: read the on-disk DuckDB file (read-only).
    md = os.getenv("MOTHERDUCK_TOKEN")
    if md:
        dbname = os.getenv("MOTHERDUCK_DATABASE", "migration")
        return duckdb.connect(f"md:{dbname}?motherduck_token={md}")
    return duckdb.connect(str(DB_PATH), read_only=True)


@st.cache_data(ttl=900, show_spinner=False)
def _all_paid_charges(today_s: str) -> "pd.DataFrame":
    """Every succeeded Stripe charge since 2024 (ANY date). A consultation can be
    paid before or after the period it falls in, so paid-consult matching looks
    at all charges — not only those inside the selected window."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    try:
        import stripe_revenue as _srev
        if not _srev.enabled():
            return pd.DataFrame()
        return _srev.fetch_charges("2024-01-01", today_s)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=900, show_spinner=False)
def paid_consults_detail(since_s: str, until_s: str) -> "pd.DataFrame":
    """Canonical paid consultations — the SINGLE source of truth shared by the
    Funnels_1 and Counsellors tabs so their numbers always agree.

    A paid consultation = a non-follow-up appointment on a **paid calendar**
    (Nasir / Gurbir / Turab) whose **meeting date (start_time)** falls in the
    selected window, AND whose contact has a Stripe **succeeded** charge for that
    counsellor — **on any date** (a consult can be paid before/after the window).
    Counsellor is attributed from the matched appointment's calendar. Columns:
    contact_id · net · amount · created_date · counsellor · calendar_id ·
    appt_created · appt_scheduled."""
    ch = _all_paid_charges(date.today().isoformat())
    if ch is None or ch.empty:
        return pd.DataFrame()
    _ws, _wu = date.fromisoformat(since_s), date.fromisoformat(until_s)
    paid_cals = [cid for c in COUNSELLORS if c.get("is_paid") for cid in c["calendar_ids"]]
    if not paid_cals:
        return pd.DataFrame()
    ph = ",".join(["?"] * len(paid_cals))
    ap = get_con().execute(
        f"SELECT contact_id, calendar_id, CAST(date_added AS DATE) ad, start_time, "
        f"COALESCE(title,'') title FROM fact_appointments "
        f"WHERE calendar_id IN ({ph}) AND contact_id IS NOT NULL "
        "AND LOWER(COALESCE(appointment_status,'')) <> 'invalid'", paid_cals).fetchdf()
    if ap.empty:
        return pd.DataFrame()
    ap["is_fu"] = ap["title"].str.contains(r"follow[ -]?up", case=False, regex=True, na=False)
    ap["ad"]    = pd.to_datetime(ap["ad"]).dt.date
    ap["sd"]    = pd.to_datetime(ap["start_time"], errors="coerce").dt.date   # meeting date
    cal_couns   = {cid: c["name"].split(" - ")[0] for c in COUNSELLORS for cid in c["calendar_ids"]}
    grp: dict = {}
    for r in ap.itertuples(index=False):
        grp.setdefault(r.contact_id, []).append(r)
    _by_consult: dict = {}   # one entry per consultation; charges summed into it
    for c in ch.itertuples(index=False):
        if c.net is None or c.net <= 0:
            continue
        cand = grp.get(c.contact_id, [])
        if not cand:                       # contact never booked on a paid calendar
            continue
        nearest = min(cand, key=lambda x: abs((x.ad - c.created_date).days) if x.ad else 9999)
        if nearest.is_fu:                  # follow-up consultation -> excluded
            continue
        # the CONSULTATION (meeting date) must fall in the selected window and be a
        # weekday — matches how the matrix counts "Appointments". The payment date
        # itself is unrestricted.
        sd = nearest.sd
        try:
            in_window = pd.notna(sd) and _ws <= sd <= _wu and sd.weekday() < 5
        except Exception:
            in_window = False
        if not in_window:
            continue
        # one row per CONSULTATION (key = contact + calendar + meeting date). A
        # consult paid in installments has >1 charge → sum them, so Paid counts
        # consultations (not charges).
        key = (c.contact_id, nearest.calendar_id, sd)
        e = _by_consult.get(key)
        if e is None:
            _by_consult[key] = {
                "contact_id":     c.contact_id,
                "net":            float(c.net),
                "amount":         float(getattr(c, "amount", c.net) or 0),
                "created_date":   c.created_date,
                "counsellor":     cal_couns.get(nearest.calendar_id, "—"),
                "calendar_id":    nearest.calendar_id,
                "appt_created":   nearest.ad,
                "appt_scheduled": nearest.start_time,
            }
        else:
            e["net"]    += float(c.net)
            e["amount"] += float(getattr(c, "amount", c.net) or 0)
    return pd.DataFrame(list(_by_consult.values()))


@st.cache_data(ttl=60)
def load_queries() -> dict[str, str]:
    """Parse executive_cards.sql + counsellor_cards.sql into a dict of
    {view_name: SELECT body}. DuckDB doesn't accept bind parameters inside
    CREATE VIEW, so we treat the files as template libraries and bind at
    execute time."""
    text = (
        SQL_PATH.read_text(encoding="utf-8")
        + "\n" + SQL_COUNS_PATH.read_text(encoding="utf-8")
        + "\n" + SQL_TABS_PATH.read_text(encoding="utf-8")
    )
    pattern = re.compile(
        r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
        re.DOTALL | re.IGNORECASE,
    )
    return {m.group(1): m.group(2).strip().rstrip(';') for m in pattern.finditer(text)}


@st.cache_data(ttl=60)
def load_metrics_yaml() -> dict:
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


def run_view(view: str, binds: dict) -> dict:
    """Run a card query and return {'current': {...}, 'prior': {...}}."""
    queries = load_queries()
    body = queries.get(view)
    if body is None:
        # View not parsed yet (cache TTL miss during hot-reload) or removed —
        # short-circuit to {} so the caller sees an empty dict, not None.
        return {}
    con = get_con()
    try:
        df = con.execute(body, binds).fetchdf()
    except Exception as e:
        st.error(f"Query failed for {view}: {e}")
        return {}
    if df is None or df.empty:
        return {}
    if "tag" not in df.columns:
        # Stale cached SQL body (likely during hot-reload after a SQL edit)
        # parsed an unrelated view — return {} so the caller doesn't crash.
        return {}
    out = {}
    for _, row in df.iterrows():
        out[row["tag"]] = row.to_dict()
    return out


def run_df(view: str, binds: dict) -> pd.DataFrame:
    """Run a view, passing ONLY the binds it actually references, and return
    the raw DataFrame. Lets drill-down views that don't use prior_* (or city)
    share the same binds dict without DuckDB complaining about excess params."""
    queries = load_queries()
    body = queries.get(view)
    if body is None:
        st.error(f"Unknown view: {view}")
        return pd.DataFrame()
    needed = {k: v for k, v in binds.items() if ("$" + k) in body}
    try:
        df = get_con().execute(body, needed).fetchdf()
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        st.error(f"Query failed for {view}: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=900, show_spinner=False)
def forecast_metrics(grain: str, fc_until_s: str, n_periods: int, fx: float) -> pd.DataFrame:
    """Per-period forecast metrics computed with the SAME cohort logic as the
    Executive_1 tab — so each period's totals match Executive_1 exactly. For EACH
    period the view is re-run over that period's window, so a contact counts in
    every period where it was created OR revived OR booked-in (matching the
    Executive_1 'Leads' card, incl. its booked-in contacts).

    Total Leads = Executive_1 leads (excl. No Activity & Queries); Meta = Paid
    Social; Organic = the rest; Appointments = cohort booked; Consultations =
    cohort showed; MARA = reached L2C-VISA 'MARA Appointment Booked'; COE Received
    = the Executive_1 **Conversions** count for the period."""
    freq = {"Month": "M", "Week": "W", "Day": "D"}[grain]
    mara_ids = set(get_con().execute(
        "SELECT DISTINCT o.contact_id FROM fact_opportunities o "
        "JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id AND p.pipeline_name = 'L2C - VISA' "
        "JOIN dim_stages st ON st.stage_id = o.stage_id WHERE st.stage_order >= 1"
        ).fetchdf()["contact_id"])
    until_ts = pd.Timestamp(fc_until_s)
    periods = list(pd.period_range(start=pd.Timestamp("2025-10-01"), end=until_ts, freq=freq))[-n_periods:]
    spd = run_df("vw_forecast_spend", {"since": "2025-10-01", "until": fc_until_s})
    if not spd.empty:
        spd = spd.assign(P=pd.to_datetime(spd["spend_date"]).dt.to_period(freq))
        spend_by_p = spd.groupby("P")["spend"].sum()
    else:
        spend_by_p = pd.Series(dtype=float)
    rows = []
    for P in periods:
        s = P.start_time.date().isoformat()
        e = min(P.end_time.date(), until_ts.date()).isoformat()
        d = run_df("vw_exec1_lead_detail", {"since": s, "until": e})
        if d.empty:
            continue
        d = d[~d["refined_source"].isin(["No Activity", "Queries"])]
        total = len(d)
        if total == 0:
            continue
        meta = int((d["refined_source"] == "Paid Social").sum())
        conv = run_df("vw_exec1_conversions", {"since": s, "until": e})
        rows.append({
            "P": P, "total": total, "meta": meta, "organic": total - meta,
            "appts": int(d["appt_booked"].sum()), "showed": int(d["appt_showed"].sum()),
            "mara": int(d["contact_id"].isin(mara_ids).sum()),
            "coes": (0 if conv is None or conv.empty
                     else int((conv["conv_type"] == "COE").sum())),     # COE-only
            "spend": float(spend_by_p.get(P, 0.0)) * fx,
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("P").sort_index()


def last_refreshed() -> datetime | None:
    try:
        ts = get_con().execute(
            "SELECT MAX(last_refreshed) FROM agg_daily_kpis"
        ).fetchone()[0]
        return ts
    except Exception:
        return None


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------

def fmt_int(n) -> str:
    if n is None: return "—"
    return f"{int(n):,}"


def fmt_money(n) -> str:
    if n is None: return "—"
    n = float(n)
    if abs(n) >= 1000:
        return f"${n/1000:,.1f}k"
    return f"${n:,.2f}"


def fmt_money_full(n) -> str:
    if n is None: return "—"
    return f"${float(n):,.2f}"


def fmt_pct(n) -> str:
    if n is None: return "—"
    return f"{float(n) * 100:.1f}%"


def fmt_pct_2dp(n) -> str:
    if n is None: return "—"
    return f"{float(n) * 100:.2f}%"


def fmt_count(n) -> str:
    """Compact count: 847000 -> '847k', 14200 -> '14.2k', 218 -> '218'."""
    if n is None: return "—"
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if abs(n) >= 10_000:
        return f"{int(round(n/1000))}k"
    if abs(n) >= 1_000:
        return f"{n/1000:.1f}k"
    return f"{int(round(n)):,}"


def fmt_position(n) -> str:
    if n is None: return "—"
    return f"{float(n):.1f}"


def _delta_md(cur_v, pri_v, higher_is_better=True, fmt="pct") -> str:
    """Streamlit-coloured delta markdown like ':green[▲ 12% vs last]'.
    fmt='pct' → percentage change; fmt='pts' → absolute change in points
    (for rates already expressed as fractions). Module-level so every tab/helper
    can use it."""
    if cur_v is None or pri_v is None:
        return ""
    try:
        if fmt == "pts":
            diff = (float(cur_v) - float(pri_v)) * 100
            if diff == 0:
                return ":gray[— vs last]"
            up = diff > 0
            is_good = up if higher_is_better else (not up)
            return f":{'green' if is_good else 'red'}[{'▲' if up else '▼'} {abs(diff):.1f} pts]"
        if float(pri_v) == 0:
            return ""
        pct = (float(cur_v) - float(pri_v)) / float(pri_v) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    if pct == 0:
        return ":gray[— vs last]"
    up = pct > 0
    is_good = up if higher_is_better else (not up)
    return f":{'green' if is_good else 'red'}[{'▲' if up else '▼'} {abs(pct):.0f}% vs last]"


def fmt_delta_position(curr, prior) -> tuple[str, str]:
    """For GSC avg_position — LOWER number = better (better ranking).
    Arrow direction reflects 'did we improve' not 'did the number go up'."""
    if curr is None or prior is None:
        return "—", "grey"
    diff = float(prior) - float(curr)  # positive = improved (moved up)
    if diff > 0:
        return f"▲ {abs(diff):.1f} spots vs last period", "green"
    if diff < 0:
        return f"▼ {abs(diff):.1f} spots vs last period", "red"
    return "—", "grey"


def fmt_delta_pct(curr, prior, *, higher_is_better: bool = True) -> tuple[str, str]:
    """Return (text, color) — green/red arrow + % change."""
    if curr is None or prior is None or prior == 0:
        return "—", "grey"
    pct = (float(curr) - float(prior)) / float(prior)
    direction_up = pct > 0
    is_good = direction_up if higher_is_better else not direction_up
    arrow = "▲" if direction_up else ("▼" if pct < 0 else "—")
    color = "green" if is_good else ("red" if pct != 0 else "grey")
    return f"{arrow} {pct*100:+.1f}% vs last period", color


def fmt_delta_pts(curr, prior, *, higher_is_better: bool = True) -> tuple[str, str]:
    """Same as fmt_delta_pct but for already-percentage metrics — pts not %."""
    if curr is None or prior is None:
        return "—", "grey"
    diff = (float(curr) - float(prior)) * 100  # both are 0-1 percentages
    direction_up = diff > 0
    is_good = direction_up if higher_is_better else not direction_up
    arrow = "▲" if direction_up else ("▼" if diff < 0 else "—")
    color = "green" if is_good else ("red" if diff != 0 else "grey")
    return f"{arrow} {diff:+.1f} pts vs last period", color


# ---------------------------------------------------------------------
# Card renderer
# ---------------------------------------------------------------------

def render_card(
    title: str,
    main_value: str,
    delta_text: str | None = None,
    delta_color: str = "grey",
    secondary: str | None = None,
):
    """Render one KPI card with a uniform look."""
    delta_html = ""
    if delta_text:
        delta_html = f'<div style="font-size:12px; color:{delta_color}; margin-top:4px;">{delta_text}</div>'
    secondary_html = ""
    if secondary:
        secondary_html = f'<div style="font-size:11px; color:#999; margin-top:6px;">{secondary}</div>'
    # NOTE: keep this HTML un-indented / single-string. Leading 4-space
    # indentation makes Streamlit's markdown treat it as a code block and the
    # raw <div> tags render as literal text.
    html = (
        '<div style="background:#fff;border:1px solid #e6e8eb;border-radius:10px;'
        'padding:14px 16px;height:100%;">'
        '<div style="font-size:11px;color:#7a8189;letter-spacing:.08em;'
        f'font-weight:600;text-transform:uppercase;">{title}</div>'
        f'<div style="font-size:28px;font-weight:700;margin-top:6px;color:#111;">{main_value}</div>'
        f'{delta_html}{secondary_html}'
        '</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


# ---------------------------------------------------------------------
# Period resolution
# ---------------------------------------------------------------------

def resolve_period(label: str, custom_range=None) -> tuple[date, date, date, date]:
    """Return (since, until, prior_since, prior_until)."""
    today = date.today()
    if label == "Current month":
        since, until = today.replace(day=1), today
    elif label == "Last 30 days":
        since, until = today - timedelta(days=29), today
    elif label == "Last 7 days":
        since, until = today - timedelta(days=6), today
    elif label == "Custom" and custom_range and len(custom_range) == 2:
        since, until = custom_range
    else:
        since, until = today.replace(day=1), today
    length_days = (until - since).days
    prior_until = since - timedelta(days=1)
    prior_since = prior_until - timedelta(days=length_days)
    return since, until, prior_since, prior_until


# ---------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------

st.markdown(
    """
    <style>
    /* Warm cream canvas so white cards pop (matches the product look) */
    .stApp { background:#f3f0e9; }
    .block-container { padding-top: 1.1rem; max-width: 1500px; }
    section[data-testid="stSidebar"] { display: none; }

    /* Pill-style tab bar */
    div[data-testid="stTabs"] [data-baseweb="tab-list"]{
        background:#fff; border:1px solid #e6e8eb; border-radius:12px;
        padding:6px; gap:4px;
    }
    div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
    div[data-testid="stTabs"] [data-baseweb="tab-border"]{ display:none; }
    div[data-testid="stTabs"] button[data-baseweb="tab"]{
        border-radius:8px; padding:6px 16px; font-weight:600; color:#475569;
    }
    div[data-testid="stTabs"] button[aria-selected="true"]{
        background:#1e293b; color:#fff !important;
    }

    /* Panel header used above each chart card */
    .panel-title { font-size:16px; font-weight:700; color:#111; margin:2px 0 10px; }
    .panel-title .hint { float:right; color:#9aa0a6; font-size:12px; font-weight:500; }

    /* Rounded status badge */
    .pill { padding:3px 11px; border-radius:999px; font-size:12px; font-weight:700;
            display:inline-block; }
    </style>
    """,
    unsafe_allow_html=True,
)


def panel_title(text: str, hint: str = "") -> None:
    """Bold panel header with an optional right-aligned grey hint."""
    h = f'<span class="hint">{hint}</span>' if hint else ""
    st.markdown(f'<div class="panel-title">{text}{h}</div>', unsafe_allow_html=True)

st.markdown(
    """
    <div style="background:#fff; border:1px solid #e6e8eb; border-radius:12px;
                padding:18px 24px; margin-bottom:14px;">
        <div style="font-size:22px; font-weight:700; color:#111;">The Migration Dashboard</div>
        <div id="freshness" style="font-size:12px; color:#7a8189; margin-top:4px;">
            Live data from GHL · Meta Ads · GA4 · GSC
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Freshness indicator
ts = last_refreshed()
if ts:
    # DuckDB usually returns this as datetime, but if it ever comes back as
    # a string (e.g. cached connection mid-schema-change), coerce defensively.
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = None
if ts:
    age_min = int((datetime.now() - ts).total_seconds() / 60)
    if age_min < 90:
        fresh_text = f"Updated {age_min} min ago" if age_min >= 1 else "Updated just now"
    else:
        fresh_text = f"Updated {age_min // 60}h {age_min % 60}m ago"
    st.caption(fresh_text)

# ---------------------------------------------------------------------
# Filter row — Period + City
# ---------------------------------------------------------------------

fcol1, fcol2, fcol3, _ = st.columns([2, 2, 2, 4])
with fcol1:
    period_label = st.selectbox(
        "Period",
        ["Current month", "Last 30 days", "Last 7 days", "Custom"],
        index=0,
    )
with fcol2:
    custom_range = None
    if period_label == "Custom":
        custom_range = st.date_input(
            "Custom range",
            value=(date.today() - timedelta(days=29), date.today()),
            max_value=date.today(),
        )
with fcol3:
    city = st.selectbox(
        "City",
        ["All", "Melbourne", "Sydney", "Others", "Unidentified"],
        index=0,
    )

since, until, prior_since, prior_until = resolve_period(period_label, custom_range)
binds = {
    "since":       since.isoformat(),
    "until":       until.isoformat(),
    "prior_since": prior_since.isoformat(),
    "prior_until": prior_until.isoformat(),
    "city":        city,
}

st.caption(
    f"Period: **{since.strftime('%b %d')} – {until.strftime('%b %d, %Y')}** "
    f"({(until - since).days + 1} days) · "
    f"Comparison: {prior_since.strftime('%b %d')} – {prior_until.strftime('%b %d')}"
)


# ---------------------------------------------------------------------
# Tabs — Executive active, others placeholder
# ---------------------------------------------------------------------

(tab_e1, tab_meta1, tab_funnels1, tab_couns, tab_seo,
 tab_fc, tab_up) = st.tabs([
    "Executive", "Meta Ads", "Funnels",
    "Counsellors", "SEO & Traffic", "Forecast & Goals", "Upload Reports",
])

# =====================================================================
# COUNSELLORS TAB
# =====================================================================

with tab_couns:
    # Counsellor tab uses appointment.start_time (when meeting is scheduled)
    # for date filtering — matches GHL's calendar view directly.
    # Layout mirrors Meta Ads / SEO tabs:
    #   - 6 clickable scorecards at top (Slots Available, Slots Booked, Showed,
    #     No Show, Paid Consults, Best Performer) with delta vs prior period.
    #   - Melbourne / Sydney city cards below, broken down by counsellor's
    #     OFFICE city (Gurbir + Navneet = Melbourne; everyone else = Sydney).
    #   - Drill-down modal opens on scorecard click.
    #   - Paid + Free tables retain per-counsellor rows + Mel/Syd subtotals.
    #
    # Note: the global "City" filter (top of page) still filters by the
    # appointment's CONTACT city — keeping its existing meaning. The Mel/Syd
    # split inside this tab is by counsellor office, which is independent
    # of that filter.
    weekdays_in_range = count_weekdays(since, until)
    weekdays_prior    = count_weekdays(prior_since, prior_until)

    # Inject the same card-button CSS used by Meta Ads / SEO tabs.
    st.markdown("""
<style>
section[data-testid="stMain"] [data-testid="stButton"] > button{
  background:#fff !important; color:#111 !important;
  border:1px solid #e6e8eb !important; border-radius:12px !important;
  padding:14px 16px !important; min-height:118px !important;
  text-align:left !important; align-items:flex-start !important;
  white-space:pre-line !important; line-height:1.45 !important;
  font-weight:400 !important; transition:all .15s !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button:hover{
  border-color:#93c5fd !important; transform:translateY(-1px);
  box-shadow:0 4px 10px rgba(37,99,235,.08) !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button[kind="primary"]{
  background:linear-gradient(135deg,#eaf3ff 0%,#dbeafe 100%) !important;
  border:2px solid #2563eb !important; color:#111 !important;
}
.acct-head-row{
  background:#fff; border:1px solid #e6e8eb; border-bottom:none;
  border-radius:14px 14px 0 0; padding:16px 18px 4px;
  display:flex; justify-content:space-between; align-items:center;
}
.acct-head-row .title{ font-size:17px; font-weight:700; color:#111; }
.acct-head-row .pill{
  background:#eef2ff; color:#3730a3; border-radius:999px;
  padding:4px 12px; font-size:12px; font-weight:700;
}
.static-card{
  background:#fff; border:1px solid #e6e8eb; border-radius:12px;
  padding:14px 16px; min-height:118px;
}
.static-card .lbl{ font-size:11px; color:#6b7280; font-weight:700;
  text-transform:uppercase; letter-spacing:.04em; }
.static-card .val{ font-size:22px; font-weight:700; color:#111; margin-top:4px; }
</style>
""", unsafe_allow_html=True)

    # ---- State ----
    if "couns_card" not in st.session_state:
        st.session_state["couns_card"] = "Slots Available"
    couns_active = st.session_state["couns_card"]

    # ---- Pull per-calendar appointments for current + prior windows ----
    queries = load_queries()
    con = get_con()

    def _by_cal(s, u):
        b = {"since": s.isoformat(), "until": u.isoformat(), "city": city}
        try:
            df = con.execute(queries["vw_counsellors"], b).fetchdf()
        except Exception as e:
            st.error(f"Counsellors query failed: {e}")
            return {}
        return {r["calendar_id"]: r for _, r in df.iterrows()} if not df.empty else {}

    by_cal_cur = _by_cal(since, until)
    by_cal_pri = _by_cal(prior_since, prior_until)

    # Daily breakdown — powers the per-city trend chart. One row per
    # (date, calendar_id); aggregated to (date, counsellor_city) in Python.
    daily_df = run_df("vw_counsellors_daily",
                      {"since": since.isoformat(),
                       "until": until.isoformat(),
                       "city":  city})
    # Map calendar_id → counsellor office city + name (used everywhere below).
    cal_to_city = {cid: c["city"]
                   for c in COUNSELLORS for cid in c["calendar_ids"]}
    cal_to_name = {cid: c["name"].split(" - ")[0]
                   for c in COUNSELLORS for cid in c["calendar_ids"]}
    if not daily_df.empty:
        daily_df["counsellor_city"] = daily_df["calendar_id"].map(cal_to_city)
        daily_df = daily_df.dropna(subset=["counsellor_city"])
        daily_df["date"] = pd.to_datetime(daily_df["date"])

    # Per-appointment detail — drives the scorecard drill-down email lists.
    appt_detail = run_df("vw_counsellor_appointments_detail",
                         {"since": since.isoformat(),
                          "until": until.isoformat()})
    if not appt_detail.empty:
        appt_detail["counsellor_city"] = appt_detail["calendar_id"].map(cal_to_city)
        appt_detail["counsellor"]      = appt_detail["calendar_id"].map(cal_to_name)
        # Drop calendars not in our active counsellor list (excluded counsellors).
        appt_detail = appt_detail.dropna(subset=["counsellor_city"])

    # Executive_1 source classification, keyed by contact_id over a wide window so
    # every appointment contact is covered — drives the Source / Platform /
    # Lead Created Date columns in the drill-down tables (replaces 'Latest Source').
    _csrc = run_df("vw_exec1_lead_detail", {"since": "2024-01-01", "until": until.isoformat()})
    _src_map   = dict(zip(_csrc["contact_id"], _csrc["refined_source"]))   if not _csrc.empty else {}
    _plat_map  = dict(zip(_csrc["contact_id"], _csrc["social_platform"]))  if not _csrc.empty else {}
    _ldt_map   = dict(zip(_csrc["contact_id"], _csrc["lead_date"]))        if not _csrc.empty else {}
    _email_map = dict(zip(_csrc["contact_id"], _csrc["email"]))            if not _csrc.empty else {}
    _pipe_map  = dict(zip(_csrc["contact_id"], _csrc["pipeline"]))         if not _csrc.empty else {}
    _stage_map = dict(zip(_csrc["contact_id"], _csrc["stage"]))            if not _csrc.empty else {}
    # actual calendar name (e.g. "Nasir Nawaz - MARA Certified - Online")
    _cal_name_map = dict(get_con().execute(
        "SELECT calendar_id, calendar_name FROM dim_calendars").fetchall())

    def _exec_src(frame):
        """(Source, Platform, Lead Created Date) series for an appointment frame,
        using the same refined_source logic as Executive_1. Platform (the granular
        social platform) only shows for social sources."""
        s  = frame["contact_id"].map(_src_map)
        p  = frame["contact_id"].map(_plat_map).where(
            s.isin(["Social media", "Paid Social"]), "—")
        ld = pd.to_datetime(frame["contact_id"].map(_ldt_map), errors="coerce")
        return (s.fillna("—").replace("", "—"),
                p.fillna("—").replace("", "—"),
                ld.dt.strftime("%Y-%m-%d").fillna("—"))

    # Per-calendar payment aggregation from GHL /payments/transactions.
    # fact_appointments.amount_paid is mostly empty (consultations are paid
    # via separate invoices), so we count succeeded payments per contact
    # who had an appointment with this counsellor in the window.
    pay_df = run_df("vw_counsellor_payments",
                    {"since": since.isoformat(), "until": until.isoformat()})
    pay_by_cal = {row["calendar_id"]: row for _, row in pay_df.iterrows()} \
                  if not pay_df.empty else {}
    pay_df_pri = run_df("vw_counsellor_payments",
                        {"since": prior_since.isoformat(),
                         "until": prior_until.isoformat()})
    pay_by_cal_pri = {row["calendar_id"]: row for _, row in pay_df_pri.iterrows()} \
                      if not pay_df_pri.empty else {}

    # Per-calendar conversion counts (booked + converted contacts).
    # Converted = contact has at least one opp in:
    #   - L2C-Edu COE Received OR CLT-Onshore COE Received (COE-converted)
    #   - L2C-VISA MARA Appointment Booked (VOE-converted)
    conv_df = run_df("vw_counsellor_conversions",
                     {"since": since.isoformat(), "until": until.isoformat()})
    if not conv_df.empty:
        conv_df["counsellor"] = conv_df["calendar_id"].map(cal_to_name)
        conv_df = conv_df.dropna(subset=["counsellor"])
        conv_by_counsellor = conv_df.groupby("counsellor").agg(
            booked_contacts=("booked_contacts", "sum"),
            converted_contacts=("converted_contacts", "sum"),
        ).to_dict("index")
    else:
        conv_by_counsellor = {}

    # Historical show-rate benchmark (75th percentile across all active
    # counsellor calendars, last 90 days, weekday appointments only).
    bench_df = run_df("vw_counsellor_show_rate_90d", {})
    if not bench_df.empty:
        bench_df["counsellor_city"] = bench_df["calendar_id"].map(cal_to_city)
        bench_df = bench_df.dropna(subset=["counsellor_city"])
        # Aggregate per counsellor across their calendars (some counsellors
        # have 2 calendars). Show rate = sum(showed) / sum(appts).
        bench_df["counsellor"] = bench_df["calendar_id"].map(cal_to_name)
        bench_agg = bench_df.groupby("counsellor").agg(
            appts_90d=("appts_90d", "sum"),
            showed_90d=("showed_90d", "sum"),
        ).reset_index()
        bench_agg["show_rate_90d"] = bench_agg["showed_90d"] / bench_agg["appts_90d"]
        # Team benchmark = 75th percentile of per-counsellor 90d show rates.
        rates = bench_agg["show_rate_90d"].dropna()
        benchmark_show_rate = float(rates.quantile(0.75)) if len(rates) else None
    else:
        bench_agg = pd.DataFrame(columns=["counsellor","show_rate_90d"])
        benchmark_show_rate = None

    def _build_row(c, by_cal, weekdays, pay_lookup=None):
        cals = c["calendar_ids"]
        appts     = sum(int(by_cal[cid]["appointments"])  for cid in cals if cid in by_cal)
        confirmed = sum(int(by_cal[cid]["confirmed"])     for cid in cals if cid in by_cal)
        showed    = sum(int(by_cal[cid]["showed"])        for cid in cals if cid in by_cal)
        noshow    = sum(int(by_cal[cid]["noshow"])        for cid in cals if cid in by_cal)
        cancelled = sum(int(by_cal[cid]["cancelled"])     for cid in cals if cid in by_cal)
        # Real payment data — succeeded transactions from GHL /payments API.
        # pay_lookup is the per-window payment dict (current vs prior).
        pay_lookup = pay_lookup if pay_lookup is not None else {}
        paid_count = sum(int(pay_lookup.get(cid, {}).get("paid_count") or 0)
                         for cid in cals)
        paid_total = sum(float(pay_lookup.get(cid, {}).get("paid_total") or 0)
                         for cid in cals)
        # One slot pool per counsellor: 13/day total, regardless of how many
        # calendars (online + onsite) they have. Online and onsite share the
        # same pool — they don't add up to 26/day.
        slots = SLOTS_PER_DAY * weekdays
        return {
            "Counsellors":     c["name"],
            "Type":            c.get("type", ""),
            "City":            c.get("city", "Sydney"),
            "Appointments":    appts,
            "Confirmed":       confirmed,
            "Showed":          showed,
            "No Show":         noshow,
            "Cancelled":       cancelled,
            # Paid / Total Payment / Payment Pending now show for ALL counsellors —
            # payment data comes from GHL /payments/transactions (succeeded, in window)
            # for any contact who booked with this counsellor. Free counsellors can
            # still have non-zero payments (visa fees, paid follow-up consults, etc.).
            "Paid":            paid_count,
            "Total Payment":   paid_total,
            "Payment Pending": max(0, appts - paid_count),
            "Available Slots": slots,
            "Booking rate":    (appts / slots) if slots else None,
            "_is_paid":        c["is_paid"],
        }

    rows_cur_all = [_build_row(c, by_cal_cur, weekdays_in_range, pay_by_cal)
                    for c in COUNSELLORS]
    rows_pri_all = [_build_row(c, by_cal_pri, weekdays_prior, pay_by_cal_pri)
                    for c in COUNSELLORS]

    # ---- Paid / Total Payment from LIVE STRIPE (per user) -----------------
    # Source = Stripe succeeded charges, attributed to a counsellor via the
    # charge DESCRIPTION ("Nasir Nawaz - MARA Certified", "Kajal - Education
    # Consultant", ...). A charge is EXCLUDED when its matching appointment
    # (same counsellor + contact, nearest date) is a FOLLOW-UP — detected from
    # the appointment title ("Follow up Appointment with ..."). When Stripe is
    # configured these override the GHL-payment numbers entirely.
    import sys as _sys
    from pathlib import Path as _Path
    from collections import defaultdict as _dd
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import stripe_revenue as _srev

    _CAL2COUNS = {}
    for _c in COUNSELLORS:
        _k = _c["name"].split(" - ")[0].split()[0].lower()
        for _cid in _c["calendar_ids"]:
            _CAL2COUNS[_cid] = _k

    def _ckey(nm):
        return nm.split(" - ")[0].split()[0].lower() if nm else ""

    def _stripe_paid(s_from, s_to):
        """{counsellor_key: {paid_count, paid_total}} from the CANONICAL paid
        consultations (shared with Funnels_1 so both tabs agree). Charges are
        already matched to a non-follow-up paid-calendar appointment; counsellor
        is the matched calendar's counsellor, keyed like `_ckey` (first word,
        lowercased)."""
        d = paid_consults_detail(s_from.isoformat(), s_to.isoformat())
        if d.empty:
            return {}
        d = d.copy()
        d["ckey"] = d["counsellor"].str.split().str[0].str.lower()
        g = d.groupby("ckey").agg(paid_count=("net", "size"), paid_total=("net", "sum"))
        return {k: {"paid_count": int(r.paid_count), "paid_total": float(r.paid_total)}
                for k, r in g.iterrows()}

    def _paid_consult_table(s_from, s_to, city=None):
        """Paid-consultation detail (per contact), built from the CANONICAL paid
        consultations (shared with Funnels_1). Columns: Email · Source · Pipeline
        · Stage · Paid · Total Payment · Calendar Name · Appt Created · Appt
        Scheduled · Lead Created."""
        d = paid_consults_detail(s_from.isoformat(), s_to.isoformat())
        if d.empty:
            return pd.DataFrame()
        agg = (d.groupby("contact_id")
               .agg(paid_count=("net", "size"), total_payment=("net", "sum"),
                    calendar_id=("calendar_id", "first"),
                    appt_created=("appt_created", "min"),
                    appt_scheduled=("appt_scheduled", "min")).reset_index())
        agg["city"] = agg["calendar_id"].map(cal_to_city)
        if city in ("Melbourne", "Sydney"):
            agg = agg[agg["city"] == city]
        if agg.empty:
            return pd.DataFrame()
        agg = agg.sort_values("total_payment", ascending=False)
        _ld = pd.to_datetime(agg["contact_id"].map(_ldt_map), errors="coerce")
        return pd.DataFrame({
            "Email":       agg["contact_id"].map(_email_map).fillna("(no email)").values,
            "Source":      agg["contact_id"].map(_src_map).fillna("—").replace("", "—").values,
            "Pipeline":    agg["contact_id"].map(_pipe_map).fillna("—").replace("", "—").values,
            "Stage":       agg["contact_id"].map(_stage_map).fillna("—").replace("", "—").values,
            "Paid":        agg["paid_count"].astype(int).values,
            "Total Payment": agg["total_payment"].map(lambda v: f"${v:,.0f}").values,
            "Calendar Name": agg["calendar_id"].map(_cal_name_map).fillna("—").values,
            "Appt Created Date":   pd.to_datetime(agg["appt_created"]).dt.strftime("%Y-%m-%d").values,
            "Appt Scheduled Date": pd.to_datetime(agg["appt_scheduled"]).dt.strftime("%Y-%m-%d").values,
            "Lead Created Date":   _ld.dt.strftime("%Y-%m-%d").fillna("—").values,
        })

    if _srev.enabled():
        _sp_cur = _stripe_paid(since, until)
        _sp_pri = _stripe_paid(prior_since, prior_until)
        if _sp_cur is not None:
            for _r in rows_cur_all:
                _v = _sp_cur.get(_ckey(_r["Counsellors"]), {"paid_count": 0, "paid_total": 0.0})
                _r["Paid"] = _v["paid_count"]
                _r["Total Payment"] = _v["paid_total"]
                _r["Payment Pending"] = max(0, _r["Appointments"] - _r["Paid"])
        if _sp_pri is not None:
            for _r in rows_pri_all:
                _v = _sp_pri.get(_ckey(_r["Counsellors"]), {"paid_count": 0, "paid_total": 0.0})
                _r["Paid"] = _v["paid_count"]
                _r["Total Payment"] = _v["paid_total"]
                _r["Payment Pending"] = max(0, _r["Appointments"] - _r["Paid"])

    def _sum(rows_, key):
        return sum((r[key] or 0) for r in rows_)

    def _by_city(rows_, city_name):
        return [r for r in rows_ if r["City"] == city_name]

    # Global city filter — Mel/Syd attribution is by counsellor OFFICE.
    # All / Melbourne / Sydney filter the counsellor set. Others / Unidentified
    # don't apply here (those are contact-side city groups), so we treat them as All.
    if city == "Melbourne":
        rows_cur = _by_city(rows_cur_all, "Melbourne")
        rows_pri = _by_city(rows_pri_all, "Melbourne")
        couns_filter_label = "Melbourne"
    elif city == "Sydney":
        rows_cur = _by_city(rows_cur_all, "Sydney")
        rows_pri = _by_city(rows_pri_all, "Sydney")
        couns_filter_label = "Sydney"
    else:
        rows_cur = rows_cur_all
        rows_pri = rows_pri_all
        couns_filter_label = "All"

    # Current totals (filtered)
    cur_slots   = _sum(rows_cur, "Available Slots")
    cur_appts   = _sum(rows_cur, "Appointments")
    cur_showed  = _sum(rows_cur, "Showed")
    cur_noshow  = _sum(rows_cur, "No Show")
    cur_paid_n   = sum((r["Paid"] or 0) for r in rows_cur)
    cur_paid_amt = sum((r["Total Payment"] or 0) for r in rows_cur)

    # Prior totals (for deltas)
    pri_slots    = _sum(rows_pri, "Available Slots")
    pri_appts    = _sum(rows_pri, "Appointments")
    pri_showed   = _sum(rows_pri, "Showed")
    pri_noshow   = _sum(rows_pri, "No Show")
    pri_paid_n   = sum((r["Paid"] or 0) for r in rows_pri)
    pri_paid_amt = sum((r["Total Payment"] or 0) for r in rows_pri)

    # Best Performer = highest show-rate counsellor (current)
    def _best(rows_):
        cands = [r for r in rows_ if r["Appointments"] > 0]
        if not cands:
            return None, 0.0
        b = max(cands, key=lambda r: r["Showed"] / r["Appointments"])
        return b, b["Showed"] / b["Appointments"]
    best_row, best_rate = _best(rows_cur)
    best_label = best_row["Counsellors"].split(" - ")[0] if best_row else "—"
    best_caption = f"{best_rate*100:.1f}% show" if best_row else ""
    pri_best_row, pri_best_rate = _best(rows_pri)

    def _delta(cur_v, pri_v, higher_is_better=True):
        try:
            if cur_v is None or pri_v is None or float(pri_v) == 0:
                return ""
            pct = (float(cur_v) - float(pri_v)) / float(pri_v) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            return ""
        if pct == 0:
            return ":gray[— vs last]"
        up = pct > 0
        is_good = up if higher_is_better else (not up)
        return f":{'green' if is_good else 'red'}[{'▲' if up else '▼'} {abs(pct):.0f}% vs last]"

    fill_pct = (cur_appts / cur_slots * 100) if cur_slots else 0
    show_pct = (cur_showed / cur_appts * 100) if cur_appts else 0

    COUNS_VAL = {
        "Slots Available": f"{cur_slots:,}",
        "Slots Booked":    f"{cur_appts:,}",
        "Showed":          f"{cur_showed:,}",
        "No Show":         f"{cur_noshow:,}",
        "Paid Consults":   f"${cur_paid_amt:,.0f}",
        "Best Performer":  best_label,
    }
    COUNS_DELTA = {
        "Slots Available": _delta(cur_slots,    pri_slots),
        "Slots Booked":    _delta(cur_appts,    pri_appts),
        "Showed":          _delta(cur_showed,   pri_showed),
        "No Show":         _delta(cur_noshow,   pri_noshow, higher_is_better=False),
        "Paid Consults":   _delta(cur_paid_amt, pri_paid_amt),
        "Best Performer":  best_caption,
    }
    COUNS_METRICS = ["Slots Available", "Slots Booked", "Showed",
                     "No Show", "Paid Consults", "Best Performer"]

    # ---- Drill-down modal ----
    @st.dialog(" ", width="large")
    def _couns_detail_modal():
        m = st.session_state.get("couns_card", "")
        st.markdown(f"### {m} — drill-down")

        # City filter (top of modal)
        city_pick = st.segmented_control(
            "View by city",
            ["All", "Melbourne", "Sydney"],
            default="All",
            key="couns_modal_city",
        ) or "All"

        # ---- BEST PERFORMER: ranked counsellor table ----
        if m == "Best Performer":
            rs = rows_cur if city_pick == "All" else _by_city(rows_cur, city_pick)
            rank = []
            for r in rs:
                ap = r["Appointments"]
                sl = r["Available Slots"]
                rank.append({
                    "Counsellor":   r["Counsellors"].split(" - ")[0],
                    "City":         r["City"],
                    "Slots Booked": ap,
                    "Slots Avail.": sl,
                    "Booking Rate": (ap / sl) if sl else None,
                    "Showed":       r["Showed"],
                    "Show Rate":    (r["Showed"] / ap) if ap else None,
                })
            df = pd.DataFrame(rank)
            df["_score"] = df["Show Rate"].fillna(0) * 0.6 + df["Booking Rate"].fillna(0) * 0.4
            df = df.sort_values(["_score", "Slots Booked"], ascending=False).drop(columns=["_score"])
            df.insert(0, "Rank", range(1, len(df) + 1))
            df["Booking Rate"] = df["Booking Rate"].map(
                lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—")
            df["Show Rate"]    = df["Show Rate"].map(
                lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—")
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.caption("Ranked by Show Rate × 0.6 + Booking Rate × 0.4. "
                       "Highest show rate among counsellors with at least one booking wins.")
            return

        # ---- All other scorecards: email list ----
        if appt_detail.empty:
            st.info("No appointments in this window.")
            return
        df = appt_detail.copy()
        if city_pick != "All":
            df = df[df["counsellor_city"] == city_pick]

        # Filter rows per active metric
        if m == "Slots Booked":
            df = df  # all appointments
            heading = "All bookings"
        elif m == "Showed":
            df = df[df["canonical_outcome"].str.lower() == "show"]
            heading = "Appointments that SHOWED"
        elif m == "No Show":
            df = df[df["canonical_outcome"].str.lower() == "noshow"]
            heading = "Appointments that NO-SHOWED"
        elif m == "Paid Consults":
            # Stripe-matched paid consultations on the paid calendars (Nasir /
            # Gurbir / Turab), follow-ups excluded.
            pct = _paid_consult_table(since, until, None if city_pick == "All" else city_pick)
            if pct.empty:
                st.info("No paid consultations (Stripe-matched, non-follow-up) in this window.")
                return
            st.markdown(f"**Paid consultations — {len(pct)} contacts** "
                        "(Nasir · Gurbir · Turab · Stripe-matched, follow-ups excluded)")
            st.dataframe(pct, hide_index=True, use_container_width=True, height=440)
            _dl1 = pct.to_csv(index=False).encode("utf-8")
            st.download_button("Download (CSV)", _dl1,
                               file_name=f"couns_paid_consults_{since.isoformat()}_{until.isoformat()}.csv",
                               mime="text/csv", key="couns_paid_dl")
            st.caption("Stripe succeeded charges matched (by contact) to a non-follow-up "
                       "appointment on a paid calendar. **Paid** = number of charges; "
                       "**Total Payment** = sum of net (AUD).")
            return
        elif m == "Slots Available":
            n_counsellors = sum(1 for c in COUNSELLORS
                                if city_pick == "All" or c["city"] == city_pick)
            st.metric("Slots Available",
                      f"{SLOTS_PER_DAY * weekdays_in_range * n_counsellors:,}",
                      f"{SLOTS_PER_DAY}/day × {weekdays_in_range} weekday(s) × {n_counsellors} counsellor(s)")
            st.caption("Slots are pure capacity (Sat/Sun excluded) — no email list applies. "
                       "Each counsellor has one slot pool of 13/day total, shared between "
                       "their online + onsite calendars. "
                       "Click Slots Booked, Showed, No Show, or Paid Consults for per-email detail.")
            return
        else:
            heading = m

        if df.empty:
            st.info(f"No appointments match: {heading}.")
            return

        st.markdown(f"**{heading}** — {len(df)} rows")
        _src, _plat, _ld = _exec_src(df)
        out = pd.DataFrame({
            "Email":        df["email"].values,
            "Name":         df["contact_name"].values,
            "Counsellor":   df["counsellor"].values,
            "City":         df["counsellor_city"].values,
            "Status":       df["appointment_status"].values,
            "Outcome":      df["canonical_outcome"].values,
            "Payment Succeeded": df["payment_succeeded"].map(lambda v: "✓" if v else "—").values,
            "Amount":       df["amount_paid"].map(
                lambda v: f"${float(v):,.0f}" if pd.notna(v) and float(v) > 0 else "—").values,
            # Source = same refined_source logic as Executive_1
            "Source":       _src.values,
            # detailed source = social platform (shown only for social sources)
            "Platform":     _plat.values,
            "Lead Created Date": _ld.values,
            "Appointment Created": pd.to_datetime(df["appt_created_at"]).dt.strftime("%Y-%m-%d %H:%M").values,
            "Meeting Time": pd.to_datetime(df["start_time"]).dt.strftime("%Y-%m-%d %H:%M").values,
        })
        out = out.sort_values(["City", "Counsellor", "Meeting Time"])
        st.dataframe(out, hide_index=True, use_container_width=True)
        st.caption(
            "Appointment Created = when the booking was made. Meeting Time = "
            "when the appointment is scheduled to happen. Lead Created Date = when "
            "the contact entered the CRM. **Source** uses the same classification "
            "as Executive_1; **Platform** is the granular social platform (social "
            "sources only). Mel/Syd split = counsellor's office (Gurbir + Navneet = "
            "Melbourne; others = Sydney) — independent of the global City filter."
        )

    # ---- 6 clickable scorecards — same UX as SEO tab:
    # click an INACTIVE scorecard → highlights it + updates chart;
    # click the ACTIVE scorecard again → opens the Mel/Syd drill-down modal.
    def _couns_scorecard(col, label_text, scorecard_label, key_suffix):
        is_active = (scorecard_label == couns_active)
        value = COUNS_VAL[scorecard_label]
        delta = COUNS_DELTA.get(scorecard_label, "")
        lines = [label_text, value]
        if delta:
            lines.append(delta)
        if col.button(
            "\n\n".join(lines),
            key=f"couns_{scorecard_label}_{key_suffix}",
            use_container_width=True,
            type=("primary" if is_active else "secondary"),
        ):
            if is_active:
                _couns_detail_modal()
            else:
                st.session_state["couns_card"] = scorecard_label
                st.rerun()

    r1 = st.columns(3)
    _couns_scorecard(r1[0], "SLOTS AVAILABLE", "Slots Available", "sc1")
    _couns_scorecard(r1[1], "SLOTS BOOKED",    "Slots Booked",    "sc1")
    _couns_scorecard(r1[2], "SHOWED",          "Showed",          "sc1")
    r2 = st.columns(3)
    _couns_scorecard(r2[0], "NO SHOW",         "No Show",         "sc2")
    _couns_scorecard(r2[1], "PAID CONSULTS",   "Paid Consults",   "sc2")
    _couns_scorecard(r2[2], "BEST PERFORMER",  "Best Performer",  "sc2")

    st.caption(
        f"Slots = {SLOTS_PER_DAY}/day × {weekdays_in_range} weekday(s) × N calendars per counsellor "
        f"(Sat/Sun excluded · online + onsite combined). Click any scorecard for Mel/Syd drill-down."
    )

    # ---- Melbourne / Sydney city cards (clickable inner tiles + trend) ----
    # Columns of vw_counsellors_daily that drive each scorecard.
    COUNS_DAILY_COL = {
        "Slots Booked":   "appointments",
        "Showed":         "showed",
        "No Show":        "noshow",
        "Paid Consults":  "paid_total",
        # Slots Available + Best Performer are derived, handled in chart logic.
    }

    def _city_daily_series(city_label, metric):
        """Return DataFrame[date, value] for the given city + active metric.
        Sat/Sun are EXCLUDED from the series so the chart doesn't dip to 0
        every weekend (we already exclude weekend bookings from the metrics)."""
        # Weekday-only date range for charting (matches the SQL filter).
        all_dates = pd.date_range(since, until, freq="B")  # 'B' = business day
        zero = pd.DataFrame({"date": all_dates, "value": 0})
        if daily_df.empty:
            return zero
        sub = daily_df[daily_df["counsellor_city"] == city_label]
        if metric == "Slots Available":
            # One pool of 13/day per counsellor (online + onsite share it).
            # Weekday-only series — no weekend zero bars.
            n_counsellors = sum(1 for c in COUNSELLORS if c["city"] == city_label)
            ser = pd.DataFrame({"date": all_dates})
            ser["value"] = SLOTS_PER_DAY * n_counsellors
            return ser
        if metric == "Best Performer":
            # Best counsellor's daily appointment count over time.
            bc, _ = _best([r for r in rows_cur if r["City"] == city_label])
            if bc is None or sub.empty:
                return zero
            # _best returns a *built* row (no calendar_ids). Look up the
            # source COUNSELLORS entry by name to get its calendar_ids.
            src = next((c for c in COUNSELLORS
                        if c["name"] == bc["Counsellors"]), None)
            if src is None:
                return zero
            cals = set(src["calendar_ids"])
            s = (sub[sub["calendar_id"].isin(cals)]
                 .groupby("date", as_index=False)["appointments"]
                 .sum()
                 .rename(columns={"appointments": "value"}))
            return s if not s.empty else zero
        col = COUNS_DAILY_COL.get(metric, "appointments")
        if sub.empty:
            return zero
        s = (sub.groupby("date", as_index=False)[col]
             .sum()
             .rename(columns={col: "value"}))
        return s if not s.empty else zero

    # Color for the (single) trend chart based on the active city filter.
    if couns_filter_label == "Melbourne":
        unified_chart_color = "#3b82f6"
    elif couns_filter_label == "Sydney":
        unified_chart_color = "#10b981"
    else:
        unified_chart_color = "#6366f1"

    def _couns_city_card(col, city_label, chart_color):
        rs = _by_city(rows_cur_all, city_label)
        n_counsellors = len(rs)
        slots    = _sum(rs, "Available Slots")
        appts    = _sum(rs, "Appointments")
        showed   = _sum(rs, "Showed")
        nshow    = _sum(rs, "No Show")
        paid_n   = sum((r["Paid"] or 0) for r in rs if r["_is_paid"])
        paid_amt = sum((r["Total Payment"] or 0) for r in rs if r["_is_paid"])
        bc, bc_rate = _best(rs)
        bc_label = bc["Counsellors"].split(" - ")[0] if bc else "—"

        with col:
            st.markdown(
                f"<div class='acct-head-row'>"
                f"<div class='title'>{city_label} "
                f"({'Victoria' if city_label == 'Melbourne' else 'NSW'})</div>"
                f"<div class='pill'>{n_counsellors} counsellor"
                f"{'s' if n_counsellors != 1 else ''} · "
                f"{appts:,} booked</div></div>",
                unsafe_allow_html=True,
            )
            # Mel/Syd inner tiles — clickable to CHANGE TREND only (no modal).
            # Modal drill-down lives exclusively on the top 6 scorecards above.
            # Same card-button styling so they look consistent with the top row.
            suf = f"city_{city_label.lower()}"

            def _tile(c, label, scorecard_label, value_override,
                      caption_override=""):
                is_active = (scorecard_label == couns_active)
                lines = [label, value_override]
                if caption_override:
                    lines.append(caption_override)
                if c.button(
                    "\n\n".join(lines),
                    key=f"couns_{scorecard_label}_{suf}",
                    use_container_width=True,
                    type=("primary" if is_active else "secondary"),
                ):
                    # Inner tiles ONLY change the trend; they never open the modal,
                    # even when the active card is re-clicked. (Top scorecards do.)
                    st.session_state["couns_card"] = scorecard_label
                    st.rerun()

            t1 = st.columns(3)
            _tile(t1[0], "SLOTS AVAILABLE", "Slots Available", f"{slots:,}")
            _tile(t1[1], "SLOTS BOOKED",    "Slots Booked",    f"{appts:,}")
            _tile(t1[2], "SHOWED",          "Showed",          f"{showed:,}")
            t2 = st.columns(3)
            _tile(t2[0], "NO SHOW",         "No Show",         f"{nshow:,}")
            _tile(t2[1], "PAID CONSULTS",   "Paid Consults",   f"${paid_amt:,.0f}",
                  caption_override=f"{paid_n} succeeded")
            _tile(t2[2], "BEST",            "Best Performer",  bc_label,
                  caption_override=(f"{bc_rate*100:.1f}% show" if bc else ""))

            # ---- View toggle: Table (default) or Trend chart ----
            view_key = f"couns_view_{city_label.lower()}"
            view_mode = st.segmented_control(
                "View",
                ["Table", "Trend"],
                default="Table",
                key=view_key,
                label_visibility="collapsed",
            ) or "Table"

            if view_mode == "Trend":
                # ---- Daily trend chart for the active metric, this city only ----
                import altair as alt
                ser = _city_daily_series(city_label, couns_active)
                ser = ser.copy()
                ser["date"] = pd.to_datetime(ser["date"])
                y_fmt = "$,.0f" if couns_active == "Paid Consults" else ",.0f"
                st.caption(f"{couns_active} — daily trend ({city_label} only)")
                chart = (
                    alt.Chart(ser)
                    .mark_area(
                        interpolate="monotone", color=chart_color, opacity=0.22,
                        line={"color": chart_color, "strokeWidth": 2.5},
                    )
                    .encode(
                        x=alt.X("date:T", title=None,
                                axis=alt.Axis(format="%b %d", tickCount=6,
                                              labelFontSize=11, grid=False,
                                              domain=False, ticks=False)),
                        y=alt.Y("value:Q", title=None,
                                axis=alt.Axis(format=y_fmt, labelFontSize=11,
                                              grid=True, gridColor="#e5e7eb",
                                              domain=False, ticks=False)),
                        tooltip=[alt.Tooltip("date:T", format="%Y-%m-%d (%a)"),
                                 alt.Tooltip("value:Q", format=",.0f")],
                    )
                    .properties(height=210)
                    .configure_view(strokeWidth=0)
                )
                st.altair_chart(chart, use_container_width=True)

            # ---- Table view (default): one row per appointment matching the
            # active metric. Columns: Date · Email · Status · Counsellor ·
            # Source · Platform · Lead Created Date (Executive_1 classification,
            # joined by contact_id — replaces the old live 'Latest Source').
            elif appt_detail.empty:
                st.info("No appointments in this window.")
            else:
                city_rows = appt_detail[appt_detail["counsellor_city"] == city_label]
                if couns_active == "Slots Booked":
                    tbl_rows = city_rows
                elif couns_active == "Showed":
                    tbl_rows = city_rows[
                        city_rows["canonical_outcome"].str.lower() == "show"]
                elif couns_active == "No Show":
                    tbl_rows = city_rows[
                        city_rows["canonical_outcome"].str.lower() == "noshow"]
                elif couns_active == "Paid Consults":
                    tbl_rows = "PAID"   # Stripe-matched table rendered below
                elif couns_active == "Best Performer":
                    # Only rows for the best counsellor in this city
                    bc, _ = _best([r for r in rows_cur if r["City"] == city_label])
                    if bc is not None:
                        src = next((c for c in COUNSELLORS
                                    if c["name"] == bc["Counsellors"]), None)
                        cals = set(src["calendar_ids"]) if src else set()
                        tbl_rows = city_rows[city_rows["calendar_id"].isin(cals)]
                    else:
                        tbl_rows = city_rows.iloc[0:0]
                else:  # Slots Available — no per-appointment view applies
                    tbl_rows = None

                if tbl_rows is None:
                    n_counsellors = sum(1 for c in COUNSELLORS
                                        if c["city"] == city_label)
                    st.info(f"Slots Available is pure capacity — "
                            f"{SLOTS_PER_DAY}/day × {weekdays_in_range} weekday(s) × "
                            f"{n_counsellors} counsellor(s) = "
                            f"{SLOTS_PER_DAY * weekdays_in_range * n_counsellors:,}. "
                            "No per-appointment view applies.")
                elif isinstance(tbl_rows, str):   # Paid Consults — Stripe-matched
                    pct = _paid_consult_table(since, until, city_label)
                    if pct.empty:
                        st.info(f"No paid consultations (Stripe-matched, non-follow-up) in {city_label}.")
                    else:
                        st.dataframe(pct, hide_index=True, use_container_width=True, height=320)
                elif tbl_rows.empty:
                    st.info(f"No appointments match '{couns_active}' in {city_label}.")
                else:
                    _s, _p, _ld = _exec_src(tbl_rows)
                    out = pd.DataFrame({
                        "Date": pd.to_datetime(tbl_rows["start_time"]).dt.strftime("%Y-%m-%d %H:%M").values,
                        "Email": tbl_rows["email"].values,
                        "Status": tbl_rows["appointment_status"].values,
                        "Counsellor": tbl_rows["counsellor"].values,
                        "Source": _s.values,
                        "Platform": _p.values,
                        "Lead Created Date": _ld.values,
                    })
                    out = out.sort_values(["Date", "Counsellor"], ascending=[False, True])
                    st.dataframe(out, hide_index=True, use_container_width=True,
                                 height=320)

            # Mini per-counsellor table for this city — with show-rate
            # benchmark column (75th percentile of last 90 days across all
            # active counsellors) and a vs-benchmark arrow.
            def _vs_bench(rate):
                if rate is None or benchmark_show_rate is None:
                    return "—"
                diff_pts = (rate - benchmark_show_rate) * 100
                arrow = "▲" if diff_pts > 0 else ("▼" if diff_pts < 0 else "—")
                return f"{arrow} {abs(diff_pts):.1f} pts"
            mini_rows = []
            for r in rs:
                rate = (r["Showed"] / r["Appointments"]) if r["Appointments"] else None
                mini_rows.append({
                    "Counsellor": r["Counsellors"].split(" - ")[0],
                    "Type":       r["Type"],
                    "Booked":     r["Appointments"],
                    "Showed":     r["Showed"],
                    "No Show":    r["No Show"],
                    "Slots":      r["Available Slots"],
                    "Show Rate":  (f"{rate*100:.1f}%" if rate is not None else "—"),
                    "Benchmark":  (f"{benchmark_show_rate*100:.1f}%"
                                   if benchmark_show_rate is not None else "—"),
                    "vs Benchmark": _vs_bench(rate),
                })
            mini = pd.DataFrame(mini_rows)
            st.dataframe(mini, hide_index=True, use_container_width=True)

    # ---- Single unified Trend / Table view (filtered by global City filter) ----
    pill_text = (f"{len(rows_cur)} counsellor"
                 f"{'s' if len(rows_cur) != 1 else ''} · "
                 f"{cur_appts:,} booked")
    region = "All cities" if couns_filter_label == "All" else (
        f"{couns_filter_label} ({'Victoria' if couns_filter_label == 'Melbourne' else 'NSW'})")
    st.markdown(
        f"<div class='acct-head-row'>"
        f"<div class='title'>{region}</div>"
        f"<div class='pill'>{pill_text}</div></div>",
        unsafe_allow_html=True,
    )

    # Drive purely from session_state to avoid default+key desync.
    if "couns_unified_view" not in st.session_state:
        st.session_state["couns_unified_view"] = "Trend"
    unified_view = st.segmented_control(
        "View",
        ["Trend", "Table"],
        key="couns_unified_view",
        label_visibility="collapsed",
    )
    if not unified_view:
        unified_view = st.session_state.get("couns_unified_view", "Trend")

    if unified_view == "Trend":
        # Daily trend for the active scorecard, filtered to the active city.
        import altair as alt
        def _trend_for_city(city_label_for_chart):
            return _city_daily_series(city_label_for_chart, couns_active)
        if couns_filter_label == "All":
            mel_ser = _trend_for_city("Melbourne").assign(city="Melbourne")
            syd_ser = _trend_for_city("Sydney").assign(city="Sydney")
            ser = pd.concat([mel_ser, syd_ser], ignore_index=True)
            ser = ser.groupby("date", as_index=False)["value"].sum()
        else:
            ser = _trend_for_city(couns_filter_label).copy()
        ser["date"] = pd.to_datetime(ser["date"])
        y_fmt = "$,.0f" if couns_active == "Paid Consults" else ",.0f"
        st.caption(f"{couns_active} — daily trend ({couns_filter_label})")
        chart = (
            alt.Chart(ser)
            .mark_area(
                interpolate="monotone", color=unified_chart_color, opacity=0.22,
                line={"color": unified_chart_color, "strokeWidth": 2.5},
            )
            .encode(
                x=alt.X("date:T", title=None,
                        axis=alt.Axis(format="%b %d", tickCount=8,
                                      labelFontSize=11, grid=False,
                                      domain=False, ticks=False)),
                y=alt.Y("value:Q", title=None,
                        axis=alt.Axis(format=y_fmt, labelFontSize=11,
                                      grid=True, gridColor="#e5e7eb",
                                      domain=False, ticks=False)),
                tooltip=[alt.Tooltip("date:T", format="%Y-%m-%d (%a)"),
                         alt.Tooltip("value:Q", format=",.0f")],
            )
            .properties(height=260)
            .configure_view(strokeWidth=0)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        # Table view — appointment-level rows for the active metric + city.
        st.caption(f"{couns_active} — {couns_filter_label}")
        if appt_detail.empty:
            st.info("No appointments in this window.")
        else:
            base_rows = appt_detail
            if couns_filter_label != "All":
                base_rows = base_rows[base_rows["counsellor_city"] == couns_filter_label]

            if couns_active == "Slots Booked":
                tbl_rows = base_rows
            elif couns_active == "Showed":
                tbl_rows = base_rows[
                    base_rows["canonical_outcome"].str.lower() == "show"]
            elif couns_active == "No Show":
                tbl_rows = base_rows[
                    base_rows["canonical_outcome"].str.lower() == "noshow"]
            elif couns_active == "Paid Consults":
                tbl_rows = "PAID"   # Stripe-matched table rendered below
            elif couns_active == "Best Performer":
                bc, _ = _best(rows_cur)
                if bc is not None:
                    src = next((c for c in COUNSELLORS
                                if c["name"] == bc["Counsellors"]), None)
                    cals = set(src["calendar_ids"]) if src else set()
                    tbl_rows = base_rows[base_rows["calendar_id"].isin(cals)]
                else:
                    tbl_rows = base_rows.iloc[0:0]
            else:  # Slots Available
                tbl_rows = None

            if tbl_rows is None:
                n_c = len(rows_cur)
                st.info(f"Slots Available is pure capacity — "
                        f"{SLOTS_PER_DAY}/day × {weekdays_in_range} weekday(s) × "
                        f"{n_c} counsellor(s) = "
                        f"{SLOTS_PER_DAY * weekdays_in_range * n_c:,}. "
                        "No per-appointment view applies.")
            elif isinstance(tbl_rows, str):   # Paid Consults — Stripe-matched
                _pc_city = couns_filter_label if couns_filter_label in ("Melbourne", "Sydney") else None
                pct = _paid_consult_table(since, until, _pc_city)
                if pct.empty:
                    st.info(f"No paid consultations (Stripe-matched, non-follow-up) in {couns_filter_label}.")
                else:
                    st.dataframe(pct, hide_index=True, use_container_width=True, height=360)
            elif tbl_rows.empty:
                st.info(f"No appointments match '{couns_active}' in {couns_filter_label}.")
            else:
                _s, _p, _ld = _exec_src(tbl_rows)
                out = pd.DataFrame({
                    "Date": pd.to_datetime(tbl_rows["start_time"]).dt.strftime("%Y-%m-%d %H:%M").values,
                    "Email": tbl_rows["email"].values,
                    "Status": tbl_rows["appointment_status"].values,
                    "Counsellor": tbl_rows["counsellor"].values,
                    "City": tbl_rows["counsellor_city"].values,
                    "Source": _s.values,
                    "Platform": _p.values,
                    "Lead Created Date": _ld.values,
                })
                out = out.sort_values(["Date", "Counsellor"], ascending=[False, True])
                st.dataframe(out, hide_index=True, use_container_width=True,
                             height=360)

    # ---- Counsellor Performance Matrix (single combined table) ----
    st.markdown(f"### Counsellor Performance Matrix — {couns_filter_label}")
    # Inherits the global City filter at the top of the page — no separate
    # control here. rows_cur is already filtered to the picked city.
    matrix_city = couns_filter_label

    def _type_simple(c):
        """Derive Type from the counsellor's name suffix:
        - 'X - MARA Certified'        -> 'MARA'
        - 'X - Career Counsellor'     -> 'Career Counsellor'
        - 'X - Education Consultant'  -> 'Education'
        Override: Navneet Kaur is a Career Counsellor by name but is treated
        as Education per Marketing Lead (she runs free education consults).
        """
        name = c.get("name", "")
        if "Navneet Kaur" in name:
            return "Education"
        if "MARA" in name:
            return "MARA"
        if "Career Counsellor" in name:
            return "Career Counsellor"
        if "Education" in name:
            return "Education"
        return "MARA" if c.get("is_paid") else "Education"

    # rows_cur is already filtered to the global City pick — use it directly.
    filtered_rows = rows_cur

    matrix_rows = []
    for r in filtered_rows:
        cname  = r["Counsellors"].split(" - ")[0]
        # Find the source COUNSELLORS entry for the type lookup
        src = next((cc for cc in COUNSELLORS if cc["name"] == r["Counsellors"]), None)
        type_simple = _type_simple(src) if src else r["Type"]
        appts   = r["Appointments"]
        showed  = r["Showed"]
        noshow  = r["No Show"]
        cancel  = r["Cancelled"]
        confirm = r["Confirmed"]
        paid    = r["Paid"] or 0
        paytot  = r["Total Payment"] or 0
        slots   = r["Available Slots"]
        conv_n  = int(conv_by_counsellor.get(cname, {}).get("converted_contacts", 0))
        show_rate_val = (showed / appts) if appts else None
        vs_bench = None
        if show_rate_val is not None and benchmark_show_rate is not None:
            vs_bench = (show_rate_val - benchmark_show_rate) * 100  # in pts
        matrix_rows.append({
            "Counsellor":      cname,
            "Type":            type_simple,
            "Appointments":    appts,
            "Confirmed":       confirm,
            "Showed":          showed,
            "No Show":         noshow,
            "Cancelled":       cancel,
            "Paid":            int(paid),
            "Total Payment":   float(paytot),
            "Available Slots": slots,
            "Booking Rate":    (appts / slots) if slots else None,
            "Show Rate":       show_rate_val,
            "Benchmark":       benchmark_show_rate,
            "vs Benchmark":    vs_bench,
            "Convert":         conv_n,
            "Conv %":          (conv_n / appts) if appts else None,
        })

    # Grand Total row (sums the visible rows)
    if matrix_rows:
        gt = {
            "Counsellor":      "Grand Total",
            "Type":             "—",
            "Appointments":    sum(r["Appointments"]    for r in matrix_rows),
            "Confirmed":       sum(r["Confirmed"]       for r in matrix_rows),
            "Showed":          sum(r["Showed"]          for r in matrix_rows),
            "No Show":         sum(r["No Show"]         for r in matrix_rows),
            "Cancelled":       sum(r["Cancelled"]       for r in matrix_rows),
            "Paid":            sum(r["Paid"]            for r in matrix_rows),
            "Total Payment":   sum(r["Total Payment"]   for r in matrix_rows),
            "Available Slots": sum(r["Available Slots"] for r in matrix_rows),
            "Convert":         sum(r["Convert"]         for r in matrix_rows),
        }
        gt["Booking Rate"] = (gt["Appointments"] / gt["Available Slots"]) if gt["Available Slots"] else None
        gt["Show Rate"]    = (gt["Showed"] / gt["Appointments"]) if gt["Appointments"] else None
        gt["Conv %"]       = (gt["Convert"] / gt["Appointments"]) if gt["Appointments"] else None
        gt["Benchmark"]    = benchmark_show_rate
        gt["vs Benchmark"] = ((gt["Show Rate"] - benchmark_show_rate) * 100
                              if gt["Show Rate"] is not None and benchmark_show_rate is not None
                              else None)
        matrix_rows.append(gt)

    mdf = pd.DataFrame(matrix_rows)
    if not mdf.empty:
        # Payment columns show "—" for Education counsellors (they don't accept
        # paid consults). Grand Total row is a special case — keep its sums.
        def _is_edu_row(r):
            return r.get("Type") == "Education"

        for col in ("Booking Rate", "Show Rate", "Conv %", "Benchmark"):
            mdf[col] = mdf[col].map(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—")
        def _fmt_vs(v):
            if v is None or pd.isna(v): return "—"
            arrow = "▲" if v > 0 else ("▼" if v < 0 else "—")
            return f"{arrow} {abs(v):.1f} pts"
        mdf["vs Benchmark"] = mdf["vs Benchmark"].map(_fmt_vs)

        # Paid + Total Payment show "—" for Education counsellors (only Nasir /
        # Gurbir / Turab take paid consultations). Grand Total keeps its sums.
        def _fmt_total_payment(row):
            if row["Type"] == "Education":
                return "—"
            return f"${row['Total Payment']:,.0f}"
        def _fmt_paid(row):
            if row["Type"] == "Education":
                return "—"
            return f"{int(row['Paid'])}"
        mdf["Total Payment"] = mdf.apply(_fmt_total_payment, axis=1)
        mdf["Paid"]          = mdf.apply(_fmt_paid, axis=1)

        sel = st.dataframe(
            mdf,
            hide_index=True,
            use_container_width=True,
            on_select="rerun",
            selection_mode="single-row",
            key="couns_perf_matrix",
            column_config={
                "Appointments":    st.column_config.NumberColumn(width="small"),
                "Confirmed":       st.column_config.NumberColumn(width="small"),
                "Showed":          st.column_config.NumberColumn(width="small"),
                "No Show":         st.column_config.NumberColumn(width="small"),
                "Cancelled":       st.column_config.NumberColumn(width="small"),
                "Available Slots": st.column_config.NumberColumn(width="small"),
                "Convert":         st.column_config.NumberColumn(width="small"),
            },
        )

        # ---- Row-click drill-down: per-counsellor email/source/payment list ----
        sel_idx = None
        try:
            rows_sel = (sel.selection.get("rows") if sel else None) or []
            if rows_sel:
                sel_idx = int(rows_sel[0])
        except Exception:
            sel_idx = None

        if (sel_idx is not None
                and 0 <= sel_idx < len(mdf)
                and mdf.iloc[sel_idx]["Counsellor"] != "Grand Total"):
            picked_name = mdf.iloc[sel_idx]["Counsellor"]
            # Resolve full counsellor name -> calendars
            src = next((cc for cc in COUNSELLORS
                        if cc["name"].split(" - ")[0] == picked_name), None)
            cals = set(src["calendar_ids"]) if src else set()
            st.markdown(f"### 👤 {picked_name} — appointment-level drill-down")
            if appt_detail.empty:
                st.info("No appointments in this window.")
            else:
                rows_p = appt_detail[appt_detail["calendar_id"].isin(cals)].copy()
                if rows_p.empty:
                    st.info(f"No appointments for {picked_name} in this window.")
                else:
                    # Join opp info per contact for Pipeline + Stage + Status
                    opp_sql = """
                    WITH lo AS (
                      SELECT contact_id, pipeline_id, stage_id, status,
                             ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
                      FROM fact_opportunities
                      WHERE contact_id IN (SELECT UNNEST(?::VARCHAR[]))
                    )
                    SELECT lo.contact_id, p.pipeline_name, s.stage_name, lo.status
                    FROM lo
                    LEFT JOIN dim_pipelines p ON p.pipeline_id = lo.pipeline_id
                    LEFT JOIN dim_stages    s ON s.stage_id    = lo.stage_id
                    WHERE lo.rn = 1
                    """
                    try:
                        cids = rows_p["contact_id"].dropna().unique().tolist()
                        opp_df = get_con().execute(opp_sql, [cids]).fetchdf()
                    except Exception:
                        opp_df = pd.DataFrame(columns=["contact_id", "pipeline_name", "stage_name", "status"])
                    rows_p = rows_p.merge(opp_df, on="contact_id", how="left")

                    # Pull real payment totals per contact (succeeded payments in window)
                    # — appointment.amount_paid is mostly empty in GHL, so we go to
                    # fact_payments which is the source of truth for paid consults.
                    try:
                        cids = rows_p["contact_id"].dropna().unique().tolist()
                        pay_q = """
                        SELECT contact_id,
                               SUM(amount - COALESCE(amount_refunded, 0)) AS total_paid
                        FROM fact_payments
                        WHERE contact_id IN (SELECT UNNEST(?::VARCHAR[]))
                          AND LOWER(status) = 'succeeded'
                          AND CAST(created_at AS DATE) BETWEEN ? AND ?
                        GROUP BY contact_id
                        """
                        pay_df_drill = get_con().execute(
                            pay_q, [cids, since.isoformat(), until.isoformat()]
                        ).fetchdf()
                    except Exception:
                        pay_df_drill = pd.DataFrame(columns=["contact_id", "total_paid"])
                    rows_p = rows_p.merge(pay_df_drill, on="contact_id", how="left")
                    rows_p["total_paid"] = rows_p["total_paid"].fillna(0)

                    _s, _p, _ld = _exec_src(rows_p)
                    out = pd.DataFrame({
                        "Email": rows_p["email"].values,
                        "Name":  rows_p["contact_name"].values,
                        "Source": _s.values,
                        "Platform": _p.values,
                        "Lead Created Date": _ld.values,
                        "Total Payment": rows_p["total_paid"].map(
                            lambda v: f"${float(v):,.0f}" if pd.notna(v) and float(v) > 0 else "—").values,
                        "Pipeline": rows_p["pipeline_name"].fillna("—").values,
                        "Stage":    rows_p["stage_name"].fillna("—").values,
                        "Status":   rows_p["status"].fillna("—").values,
                        "Appointment Status": rows_p["appointment_status"].values,
                        "Meeting Time": pd.to_datetime(rows_p["start_time"]).dt.strftime("%Y-%m-%d %H:%M").values,
                    })
                    out = out.sort_values("Meeting Time", ascending=False)
                    st.dataframe(out, hide_index=True, use_container_width=True, height=360)
                    st.caption(
                        f"{len(out)} appointment{'s' if len(out) != 1 else ''} for "
                        f"**{picked_name}** in this window. **Source** uses the same "
                        "classification as Executive_1; **Platform** is the granular "
                        "social platform. Click another row above to switch; click the "
                        "same row again to clear."
                    )
        else:
            st.caption("💡 Click any counsellor row above to drill into their per-appointment detail (emails, latest source, pipeline, stage, status, payment).")
    else:
        st.info(f"No counsellors in {matrix_city}.")

    with st.expander("ℹ️  About this view — metric definitions & data sources"):
        bench_pct = (f"{benchmark_show_rate*100:.1f}%"
                     if benchmark_show_rate is not None else "—")
        st.markdown(f"""
### 📖 Tab overview
The **Counsellors** tab tracks utilisation, outcomes, and paid revenue for each consultant against actual GHL calendar bookings. Mel/Syd attribution is by counsellor office (not by the contact's city) so each counsellor always rolls up under one city, regardless of where their leads live.

### 🔌 Data sources (where the numbers come from)
| Table | Source endpoint | What it powers |
|---|---|---|
| `fact_appointments` | GHL `/calendars/events` | Appointments, Confirmed, Showed, No Show, Cancelled, Available Slots, Booking/Show Rate |
| `fact_contacts` | GHL `/contacts/` | Email, contact identity, attribution fallback |
| `fact_payments` | GHL `/payments/transactions` (altId/altType=location) | Paid, Payment Pending, Total Payment |
| `fact_opportunities` + `dim_pipelines` + `dim_stages` | GHL `/opportunities/search` + `/pipelines` | Convert + Conv % |
| `fact_form_submissions` + `fact_survey_submissions` | GHL `/forms/submissions` + `/surveys/submissions` | Latest Source (live-computed in drill-down) |

### 🧮 Performance Matrix — column-by-column logic
| Column | Formula / Source | Notes |
|---|---|---|
| **Counsellor** | First-name from the locked `COUNSELLORS` list in app.py | One row per active counsellor |
| **Type** | Parsed from the counsellor's name suffix, with override | `MARA` (Nasir, Gurbir), `Career Counsellor` (Turab), `Education` (Kajal, Wajahad, Saurab, **Navneet** — override per Marketing Lead: she runs free education consults despite the "Career Counsellor" title) |
| **Appointments** | `COUNT(*) FROM fact_appointments WHERE start_time IN window AND DAYOFWEEK NOT IN (0,6)` | Invalid status appointments are dropped **globally** at the SQL view layer — they don't appear in any count, scorecard, table, drill-down, or chart. Sat/Sun excluded too. |
| **Confirmed** | `COUNT(*) FILTER (WHERE LOWER(appointment_status) = 'confirmed')` | Subset of Appointments |
| **Showed** | `COUNT(*) FILTER (WHERE LOWER(appointment_status) = 'showed')` | Subset of Appointments |
| **No Show** | `COUNT(*) FILTER (WHERE LOWER(appointment_status) = 'noshow')` | Subset of Appointments |
| **Cancelled** | `COUNT(*) FILTER (WHERE LOWER(appointment_status) = 'cancelled')` | Subset of Appointments (Invalid is NOT included anywhere) |
| **Paid** | `COUNT(DISTINCT contact_id) FROM fact_payments WHERE status='succeeded' AND (amount - amount_refunded) > 0 AND created_at IN window AND contact had appointment with this counsellor in window` | Real payment data from `/payments/transactions`, not `fact_appointments.amount_paid` (which is empty in GHL) |
| **Payment Pending** | `Appointments - Paid` (floored at 0) — but shown as **"—"** for Education counsellors who don't charge | Approximation of unpaid consultations |
| **Total Payment** | `SUM(amount - amount_refunded) FROM fact_payments WHERE status='succeeded' ...same filter as Paid` — shown as **"—"** for Education counsellors | In AUD |
| **Available Slots** | `{SLOTS_PER_DAY} × weekdays in window × 1` per counsellor | Each counsellor has one slot pool of {SLOTS_PER_DAY}/day total — online + onsite calendars share it (they don't add up to 26/day). Sat/Sun excluded. |
| **Booking Rate** | `Appointments ÷ Available Slots` | How much of the counsellor's capacity is being used |
| **Show Rate** | `Showed ÷ Appointments` | Quality of bookings |
| **Convert** | `COUNT(DISTINCT contact_id)` where contact had appointment with this counsellor AND has an opp at one of these "converted" stages: | Contact-level, not opp-level — a contact with multiple converted opps counts once |
| | • `L2C - Education` → **COE Received** (COE-converted) | |
| | • `CLT - Onshore Admission` → **COE Received** (COE-converted) | |
| | • `L2C - VISA` → **MARA Appointment Booked** (VOE-converted) | |
| **Conv %** | `Convert ÷ Appointments` | Funnel end-to-end conversion |

### 🏙️ Mel/Syd attribution & City filter
- **Melbourne** office: Gurbir Singh, Navneet Kaur
- **Sydney** office: Turab, Nasir Nawaz, Kajal, Wajahad, Saurab
- Everything on this tab — top scorecards, trend chart, drill-down table, and the Performance Matrix — filters by the **global City filter** at the top of the page (`All / Melbourne / Sydney`).
- Mel/Syd here = counsellor **office**, not the contact's city (which is what the global filter cascades by on other tabs).
- "Others" and "Unidentified" don't apply to counsellor offices, so they're treated as "All" on this tab.

### 🧬 Source — drill-down column logic
The drill-down tables now show **Source** (the same `refined_source` classification as **Executive_1** — Paid Social / Social media / Referral / Organic Search / Direct / Walk-in / Phone/SMS / Web Chat / Email / Queries / etc.), the granular **Platform** (social platform, social sources only), and the **Lead Created Date**. These replace the old live-computed "Latest Source" column. The classification is joined by `contact_id` from `vw_exec1_lead_detail` over a wide window so every appointment contact is covered.

### 🖱️ Interactivity
- **Top 6 scorecards** — click an inactive scorecard → highlights it + changes the unified trend chart / table below; click the already-active scorecard again → opens the drill-down modal.
- **Unified Trend/Table toggle** — below the scorecards: Table = appointment-level rows for the active metric + city; Trend = daily Altair area chart.
- **Performance Matrix row click** — click any counsellor row in the Performance Matrix → a per-appointment table appears below with `Email · Name · Source · Platform · Lead Created Date · Total Payment · Pipeline · Stage · Status · Appointment Status · Meeting Time`. Click another row to switch; click the same row again to clear. Grand Total row is not clickable.
- **Drill-down modal contents per scorecard:**
  - **Slots Booked** → all bookings: Email · Name · Counsellor · City · Status · Outcome · Payment Succeeded · Amount · Source · Platform · Lead Created Date · Appointment Created · Meeting Time.
  - **Showed** → filtered to `canonical_outcome = 'show'`.
  - **No Show** → filtered to `canonical_outcome = 'noshow'`.
  - **Paid Consults** → Stripe succeeded charges matched (by contact) to a non-follow-up appointment on a paid calendar (Nasir · Gurbir · Turab): Email · Source · Pipeline · Stage · Paid · Total Payment · Calendar Name · Appt Created · Appt Scheduled · Lead Created.
  - **Best Performer** → ranked counsellor table, sorted by `Show Rate × 0.6 + Booking Rate × 0.4`.
  - **Slots Available** → capacity formula only.

### 🎯 Benchmark (Show Rate column in Mel/Syd mini tables)
Benchmark = **75th percentile of show rates across all active counsellors over the last 90 days (weekday appointments only): {bench_pct}.** A counsellor above the benchmark is in the **top quartile of team performance** for that 90-day period.

- Per-counsellor 90-day show rate = `SUM(showed) ÷ SUM(appts)` across all their calendars
- Then 75th percentile across counsellors
- Filter: ≥3 appointments in 90-day window per calendar (otherwise rate isn't trustworthy)
- "vs Benchmark" column shows percentage-point gap, ▲ green if above, ▼ red if below

### 📊 Comparison deltas
Every top scorecard shows `▲/▼ X% vs last` against the **prior equal-length window** ({prior_since.strftime('%b %d')} – {prior_until.strftime('%b %d')}). For "No Show" the colour is inverted (lower is better).

### ⚠️ Excluded counsellors
Manhal Dandachi, Minhaz, Faheem (per Marketing Lead) — appointments on their calendars don't appear in any counsellor metric.
        """)


# =====================================================================
# META ADS — inline detail renderer (shown below the scorecards)
# =====================================================================

def _grand_total(df: pd.DataFrame, label_col: str, num_cols: list[str],
                 label: str = "Grand Total") -> pd.DataFrame:
    """Append a Grand Total row that sums num_cols; 'label' goes in label_col."""
    if df is None or df.empty:
        return df
    row = {c: "" for c in df.columns}
    row[label_col] = label
    for c in num_cols:
        row[c] = int(df[c].sum())
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


def render_meta_detail():
    """Inline drill-down rendered below the Meta Ads scorecards (no modal)."""
    # ---- Filters: City + date range ----
    fc1, fc2 = st.columns([3, 2])
    with fc1:
        opts = ["All", "Melbourne", "Sydney", "Others", "Unidentified"]
        default_city = city if city in opts else "All"
        drill_city = st.segmented_control(
            "City", opts, default=default_city, key="meta_detail_city") or default_city
    with fc2:
        dr = st.date_input(
            "Date range", value=(since, until),
            max_value=date.today(), key="meta_detail_dates")
        if isinstance(dr, (tuple, list)) and len(dr) == 2:
            d_since, d_until = dr
        else:
            d_since, d_until = since, until

    # Prior window (same length, immediately before) — needed by the aligned view.
    _len = (d_until - d_since).days
    _p_until = d_since - timedelta(days=1)
    _p_since = _p_until - timedelta(days=_len)
    dbinds = {"since": d_since.isoformat(), "until": d_until.isoformat(),
              "prior_since": _p_since.isoformat(), "prior_until": _p_until.isoformat(),
              "city": drill_city}
    st.caption(
        "Date filter applies to **opportunity-created date**. GHL does not expose a per-attribution "
        "timestamp, so this is the closest proxy for when an attributed opportunity came in."
    )

    # ---- GHL ⇄ Meta lead alignment (audit-ratified reconciliation) ----
    st.markdown("**GHL ⇄ Meta lead alignment**")
    al = run_df("vw_aligned_ghl_leads", dbinds)
    al_cur = al[al["tag"] == "current"].iloc[0] if not al.empty else None
    meta_lds_df = run_df("vw_meta_tab_totals", dbinds)
    meta_lds = int(meta_lds_df[meta_lds_df["tag"] == "current"].iloc[0]["leads"]) \
        if not meta_lds_df.empty and (meta_lds_df["tag"] == "current").any() else 0
    if al_cur is not None:
        A = int(al_cur["opps_in_range"]); B = int(al_cur["returning_opps"])
        C = int(al_cur["contacts_no_opp"]); tot_al = int(al_cur["aligned_leads"])
        pct = (tot_al / meta_lds) if meta_lds else None
        sc = st.columns(3)
        sc[0].metric("Meta leads (Results)", fmt_int(meta_lds))
        sc[1].metric("GHL leads (aligned)", fmt_int(tot_al))
        sc[2].metric("Alignment", fmt_pct(pct) if pct is not None else "—")
        recon = pd.DataFrame({
            "Bucket": [
                "A · Opportunities created in range",
                "B · Returning (old opp, form re-filled in range)",
                "C · New contacts with no opportunity",
                "GHL aligned total (A+B+C)",
                "Meta leads (Results)",
                "Alignment %",
            ],
            "Count": [
                fmt_int(A), fmt_int(B), fmt_int(C), fmt_int(tot_al),
                fmt_int(meta_lds), fmt_pct(pct) if pct is not None else "—",
            ],
        })
        st.dataframe(recon, hide_index=True, width="stretch")
        st.caption(
            "Paid Social / Social-media-attributed contacts. **B** counts only contacts with a real "
            "form-fill event in range (page visits / campaign views are excluded). Buckets A/B/C are "
            "mutually exclusive (opp-in-range / opp-before-range / no-opp)."
        )

        # Returning-lead detail: old source → new (re-filled) source
        st.markdown("**Returning leads — old opportunity source → new (re-filled) source**")
        rl = run_df("vw_drill_returning_leads", dbinds)
        if rl.empty:
            st.info("No returning leads (re-filled a form in range) for this city / period.")
        else:
            rl = rl.rename(columns={
                "email": "Email", "old_opp_created": "Opp Created",
                "old_source": "Old Source", "new_form_date": "Re-filled On",
                "new_source": "New Source"})
            st.dataframe(rl, hide_index=True, width="stretch")
            st.caption("Each contact had an opportunity created before the range and re-submitted a "
                       "Meta form inside it — so the opportunity source is stale and should be updated.")

    st.divider()

    # ---- Summary strip ----
    tot = run_df("vw_drill_opp_totals", dbinds)
    if not tot.empty:
        row = tot.iloc[0]
        s = st.columns(4)
        s[0].metric("Opportunities (range)", fmt_int(row["total_opps"]))
        s[1].metric("Paid Social / Social", fmt_int(row["social_opps"]))
        s[2].metric("Melbourne", fmt_int(row["melbourne_opps"]))
        s[3].metric("Sydney", fmt_int(row["sydney_opps"]))

    t1, t2, t3, t4, t5 = st.tabs(
        ["📣 Campaigns", "🥇 First Attribution", "🎯 Latest Attribution",
         "📝 Forms & Medium", "📊 Opp Status"])

    # --- Tab 1: campaigns + leads + CPL ---
    with t1:
        camp = run_df("vw_drill_meta_campaigns", dbinds)
        if camp.empty:
            st.info("No Meta campaign data for this city / period.")
        else:
            tot_res = int(camp["results"].sum())
            tot_spend = float(camp["spend"].sum())
            blended = (tot_spend / tot_res) if tot_res else None
            out = camp.copy()
            out = out.rename(columns={"account_label": "Account", "campaign_name": "Campaign",
                                      "results": "Results", "result_event": "Result Event"})
            out["Spend"] = out["spend"].map(lambda v: f"${v:,.0f}")
            out["CPL"] = out["cpl"].map(lambda v: f"${v:,.2f}" if pd.notna(v) else "—")
            out = out[["Account", "Campaign", "Results", "Result Event", "Spend", "CPL"]]
            gt = pd.DataFrame([{"Account": "", "Campaign": "Grand Total", "Results": tot_res,
                                "Result Event": "", "Spend": f"${tot_spend:,.0f}",
                                "CPL": f"${blended:,.2f}" if blended else "—"}])
            out = pd.concat([out, gt], ignore_index=True)
            st.dataframe(out, hide_index=True, width="stretch")
            st.caption("**Results** = Meta's 'Results' column (campaign optimization event) — matches Ads "
                       "Manager. CPL = Spend ÷ Results.")

        # ---- Lead-type composition (explains why Meta ≠ GHL) ----
        st.markdown("**Meta lead composition — why Meta can far exceed GHL** "
                    "_(insight snapshot, Mar 1 – May 19)_")
        lt = run_df("vw_drill_meta_lead_types", dbinds)
        if lt.empty:
            st.info("No Meta insight data for this city.")
        else:
            lt = lt.rename(columns={
                "account_label": "Account", "campaign_name": "Campaign",
                "instant_form": "Instant Form", "pixel_lead": "Pixel Lead",
                "pixel_custom": "Pixel Custom", "messenger": "Messenger",
                "total_leads": "Total Leads"})
            lt = _grand_total(lt, "Campaign",
                              ["Instant Form", "Pixel Lead", "Pixel Custom", "Messenger", "Total Leads"])
            st.dataframe(lt, hide_index=True, width="stretch")
            st.caption(
                "**Instant Form** = real lead-form submissions → these create GHL contacts/opps. "
                "**Pixel Custom** = website pixel events → fire multiple times per visitor and do **not** map "
                "1:1 to people. Campaigns dominated by Pixel Custom (e.g. *UM Office Video*: 687 pixel vs 4 form) "
                "show far more 'leads' in Meta than opportunities in GHL. **Messenger** is tracked separately "
                "(not in Total Leads)."
            )

    # --- Tab 2: FIRST attribution ---
    with t2:
        fat = run_df("vw_drill_first_attribution_counts", dbinds)
        if fat.empty:
            st.info("No opportunities in range.")
        else:
            fat = fat.rename(columns={"first_attribution": "First Attribution", "opps": "Opps"})
            fat = _grand_total(fat, "First Attribution", ["Opps"])
            st.dataframe(fat, hide_index=True, width="stretch")
            st.caption("Opportunities in range grouped by the contact's **first** attribution source.")

    # --- Tab 3: LATEST attribution + opportunity sources ---
    with t3:
        st.markdown("**Latest attribution — all opps in range**")
        att = run_df("vw_drill_attribution_counts", dbinds)
        if att.empty:
            st.info("No opportunities in range.")
        else:
            att = att.rename(columns={"latest_attribution": "Latest Attribution", "opps": "Opps"})
            att = _grand_total(att, "Latest Attribution", ["Opps"])
            st.dataframe(att, hide_index=True, width="stretch")
        st.markdown("**GHL opps by campaign** _(source text before ` -- `, social-attributed)_")
        cs = run_df("vw_drill_social_campaign_sources", dbinds)
        if cs.empty:
            st.info("No social-attributed opportunities in range.")
        else:
            cs = cs.rename(columns={"campaign": "Campaign (GHL source)", "opps": "Opps"})
            cs = _grand_total(cs, "Campaign (GHL source)", ["Opps"])
            st.dataframe(cs, hide_index=True, width="stretch")
            st.caption("GHL opportunities grouped by the **campaign** portion of the source string "
                       "(before ` -- `). Compare these to the Meta campaign Results in the Campaigns tab — "
                       "they differ because Meta counts pixel/form events while GHL counts opportunities.")

        st.markdown("**Opportunity sources (full) — where latest attribution = Paid Social / Social media**")
        src = run_df("vw_drill_social_opp_sources", dbinds)
        if src.empty:
            st.info("No social-attributed opportunities in range.")
        else:
            src = src.rename(columns={"opportunity_source": "Opportunity Source",
                                      "latest_attribution": "Latest Attribution", "opps": "Opps"})
            src = _grand_total(src, "Opportunity Source", ["Opps"])
            st.dataframe(src, hide_index=True, width="stretch")
            st.caption("Full source (campaign -- ad). Opportunities may repeat across sources; counts are totals per source.")

        st.markdown("**Which pipelines do social opps land in?** _(reconcile vs Meta leads)_")
        pl = run_df("vw_drill_social_pipelines", dbinds)
        if pl.empty:
            st.info("No social-attributed opportunities in range.")
        else:
            pl = pl.rename(columns={"pipeline": "Pipeline", "opps": "Opps"})
            pl = _grand_total(pl, "Pipeline", ["Opps"])
            st.dataframe(pl, hide_index=True, width="stretch")
            st.caption("Social-attributed opps are **not** restricted to L2C - Education — this shows the full "
                       "spread across pipelines so the total can be reconciled against Meta's lead count.")

    # --- Tab 4: medium / forms breakdown ---
    with t4:
        med = run_df("vw_drill_medium_counts", dbinds)
        if med.empty:
            st.info("No social-attributed opportunities in range.")
        else:
            cols = st.columns(min(5, len(med)))
            for i, (_, mr) in enumerate(med.iterrows()):
                cols[i % len(cols)].metric(str(mr["medium"]).title(), fmt_int(mr["opps"]))
            med = med.rename(columns={"medium": "Medium / Form Type", "opps": "Opps"})
            med = _grand_total(med, "Medium / Form Type", ["Opps"])
            st.dataframe(med, hide_index=True, width="stretch")
            st.caption("Form / Survey / Manual / Calendar / Pending (no form or survey filled) — "
                       "from the contact's latest attribution medium.")

    # --- Tab 5: opportunity status (always show all 4) ---
    with t5:
        stt = run_df("vw_drill_opp_status", dbinds)
        counts = ({str(rr["status"]).lower(): int(rr["opps"]) for _, rr in stt.iterrows()}
                  if not stt.empty else {})
        order = ["open", "won", "lost", "abandoned"]
        cols = st.columns(4)
        for i, sname in enumerate(order):
            cols[i].metric(sname.title(), fmt_int(counts.get(sname, 0)))
        tbl = pd.DataFrame({"Status": [s.title() for s in order],
                            "Opps": [counts.get(s, 0) for s in order]})
        tbl = _grand_total(tbl, "Status", ["Opps"])
        st.dataframe(tbl, hide_index=True, width="stretch")
        st.caption("Status of Paid Social / Social-media-attributed opportunities in range "
                   "(open / won / lost / abandoned — zero-filled when none).")


# =====================================================================
# META ADS TAB
# =====================================================================

def render_meta_ads(kp=""):
    """Render the full Meta Ads tab body. `kp` is a per-tab key suffix so the
    same UI can be mounted in two tabs (Meta Ads + Meta_1) without Streamlit
    widget-key / session-state collisions. kp='' reproduces the original tab."""
    import json as _json

    # ---- State (session_state so the active tab is preserved on click) ----
    # Backward-compat: if a stale ?metric=X was in the URL from the old version,
    # absorb it once into session_state and clear it so the tab no longer resets.
    METRIC_KEYS_REV = {
        "Spend": "spend", "Impressions": "impressions", "Clicks": "clicks", "CTR": "ctr",
        "Leads (Meta)": "leads", "GHL Leads": "ghl_leads", "CPL": "cpl",
        "Bookings": "bookings", "Showed": "showed", "Cost per Booking": "cpb",
    }
    METRIC_KEYS = {v: k for k, v in METRIC_KEYS_REV.items()}
    _qp = st.query_params.get("metric")
    if _qp:
        if _qp in METRIC_KEYS:
            st.session_state[f"meta_card{kp}"] = METRIC_KEYS[_qp]
        try:
            del st.query_params["metric"]
        except Exception:
            pass
    if f"meta_card{kp}" not in st.session_state:
        st.session_state[f"meta_card{kp}"] = "Leads (Meta)"
    active = st.session_state[f"meta_card{kp}"]

    target_cpl = float(st.session_state.get(f"meta_tcpl{kp}", 20.0))
    target_br  = float(st.session_state.get(f"meta_tbr{kp}",  20.0)) / 100.0
    target_cpb = float(st.session_state.get(f"meta_tcpb{kp}", 200.0))

    # ---- Global CSS: style Streamlit buttons as proper scorecards + headers ----
    # (Other tabs have no buttons, so this CSS is effectively scoped to Meta Ads.)
    st.markdown("""
<style>
/* Card-style buttons */
section[data-testid="stMain"] [data-testid="stButton"] > button{
  background:#fff !important; color:#111 !important;
  border:1px solid #e6e8eb !important; border-radius:12px !important;
  padding:14px 16px !important; min-height:118px !important;
  text-align:left !important; align-items:flex-start !important;
  white-space:pre-line !important; line-height:1.45 !important;
  font-weight:400 !important; transition:all .15s !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button:hover{
  border-color:#93c5fd !important; transform:translateY(-1px);
  box-shadow:0 4px 10px rgba(37,99,235,.08) !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button[kind="primary"]{
  background:linear-gradient(135deg,#eaf3ff 0%,#dbeafe 100%) !important;
  border:2px solid #2563eb !important; color:#111 !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button[kind="primary"]:hover{
  box-shadow:0 4px 14px rgba(37,99,235,.18) !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button > div{ width:100% !important; }
section[data-testid="stMain"] [data-testid="stButton"] > button > div > p{
  margin:0 !important; text-align:left !important; width:100% !important;
}
/* Account-card header */
.acct-head-row{
  background:#fff; border:1px solid #e6e8eb; border-bottom:none;
  border-radius:14px 14px 0 0; padding:16px 18px 4px;
  display:flex; justify-content:space-between; align-items:center;
}
.acct-head-row .title{ font-size:17px; font-weight:700; color:#111; }
.acct-head-row .pill{
  background:#eef2ff; color:#3730a3; border-radius:999px;
  padding:4px 12px; font-size:12px; font-weight:700;
}
/* Static (non-clickable) card to match button look — used for "Converted" */
.static-card{
  background:#fff; border:1px solid #e6e8eb; border-radius:12px;
  padding:14px 16px; min-height:90px;
}
.static-card .lbl{ font-size:11px; color:#6b7280; font-weight:700;
  text-transform:uppercase; letter-spacing:.04em; }
.static-card .val{ font-size:22px; font-weight:700; color:#111; margin-top:4px; }
.static-card .note{ font-size:10px; color:#9aa0a6; margin-top:4px; }
/* Warning variant — flags a metric that needs fixing (e.g. CONVERTED) */
.static-card.warn{
  border:2px solid #ef4444;
  box-shadow:0 0 0 3px rgba(239,68,68,0.10);
  background:#fff5f5;
}
.static-card.warn .lbl{ color:#b91c1c; }
.static-card.warn .val{ color:#991b1b; }
.static-card.warn .note{ font-size:10px; color:#b91c1c; margin-top:4px;
  font-style:italic; }
</style>
""", unsafe_allow_html=True)

    # ---- Pull data: current + prior windows (for comparison deltas) ----
    meta_t  = run_view("vw_meta_tab_totals", binds)
    def _cur(d, k): return d.get("current", {}).get(k) if d else None
    def _pri(d, k): return d.get("prior",   {}).get(k) if d else None

    ghl_per = run_df("vw_meta_ghl_leads_per_campaign",
                     {"since": since.isoformat(), "until": until.isoformat()})
    showed_df = run_df("vw_meta_showed_per_campaign",
                       {"since": since.isoformat(), "until": until.isoformat()})
    ghl_per_pri = run_df("vw_meta_ghl_leads_per_campaign",
                         {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
    showed_df_pri = run_df("vw_meta_showed_per_campaign",
                           {"since": prior_since.isoformat(), "until": prior_until.isoformat()})

    # Meta-side metrics (from fact_meta_daily — always Meta only)
    c_spend = _cur(meta_t, "spend") or 0
    c_impr  = _cur(meta_t, "impressions") or 0
    c_clk   = _cur(meta_t, "clicks") or 0
    c_ctr   = _cur(meta_t, "ctr")
    c_lds   = _cur(meta_t, "leads") or 0
    c_cpl   = _cur(meta_t, "cpl")
    p_spend = _pri(meta_t, "spend") or 0
    p_impr  = _pri(meta_t, "impressions") or 0
    p_clk   = _pri(meta_t, "clicks") or 0
    p_ctr   = _pri(meta_t, "ctr")
    p_lds   = _pri(meta_t, "leads") or 0
    p_cpl   = _pri(meta_t, "cpl")
    # GHL-side metrics (Bookings / Showed / GHL Leads / Cost per Booking) are
    # populated AFTER perf is built below — restricted to Meta campaigns only
    # so the top scorecards = Mel + Syd account-card sums.

    def _delta_md(cur_v, pri_v, higher_is_better=True, fmt="pct"):
        """Return Streamlit-coloured markdown text like ':green[▲ 12% vs last]'.

        fmt='pct' → percentage change; fmt='pts' → absolute change in points
        (for rates already expressed as fractions). Accepts `fmt` so this and
        the Executive-tab _delta_md share one signature — both become module
        globals, so whichever is defined last must handle every call shape.
        """
        if cur_v is None or pri_v is None:
            return ""
        try:
            if fmt == "pts":
                diff = (float(cur_v) - float(pri_v)) * 100
                if diff == 0:
                    return ":gray[— vs last]"
                up = diff > 0
                is_good = up if higher_is_better else (not up)
                return f":{'green' if is_good else 'red'}[{'▲' if up else '▼'} {abs(diff):.1f} pts]"
            if float(pri_v) == 0:
                return ""
            pct = (float(cur_v) - float(pri_v)) / float(pri_v) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            return ""
        if pct == 0:
            return ":gray[— vs last]"
        up = pct > 0
        is_good = up if higher_is_better else (not up)
        arrow = "▲" if up else "▼"
        color = "green" if is_good else "red"
        return f":{color}[{arrow} {abs(pct):.0f}% vs last]"

    # Payments per campaign (from the cached JSON pulled by the report tool)
    pay_path = ROOT.parent / "_all_payments.json"
    paid_contacts_by_camp, paid_amt_by_camp = {}, {}
    cls_map = dict(get_con().execute(
        "SELECT contact_id, latest_source_campaign FROM fact_contact_latest_source").fetchall())
    if pay_path.exists():
        try:
            for t in _json.loads(pay_path.read_text(encoding="utf-8")):
                ts = (t.get("createdAt") or "")[:10]
                if not (since.isoformat() <= ts <= until.isoformat()):
                    continue
                net = float(t.get("amount") or 0) - float(t.get("amountRefunded") or 0)
                if t.get("status") == "succeeded" and net > 0:
                    cid = t.get("contactId")
                    camp = cls_map.get(cid, "(no latest source)")
                    paid_contacts_by_camp.setdefault(camp, set()).add(cid)
                    paid_amt_by_camp[camp] = paid_amt_by_camp.get(camp, 0.0) + net
        except Exception:
            pass

    # ---- Per-campaign perf (computed BEFORE scorecards so the dialog can close over it) ----
    meta_per = run_df("vw_meta_per_campaign", {
        "since": since.isoformat(), "until": until.isoformat(), "account": "All"})
    if not meta_per.empty:
        perf = meta_per.merge(
            ghl_per.rename(columns={"campaign": "campaign_name"}),
            on="campaign_name", how="left").fillna(0)
        perf["paid"]            = perf["campaign_name"].map(lambda c: len(paid_contacts_by_camp.get(c, set())))
        perf["paid_amount"]     = perf["campaign_name"].map(lambda c: paid_amt_by_camp.get(c, 0.0))
        perf["booking_rate"]    = perf.apply(lambda r: r["bookings"] / r["ghl_leads"] if r["ghl_leads"] else None, axis=1)
        perf["paid_cons_rate"]  = perf.apply(lambda r: r["paid"] / r["ghl_leads"] if r["ghl_leads"] else None, axis=1)
        perf["cost_per_booking"]= perf.apply(lambda r: r["spend"] / r["bookings"] if r["bookings"] else None, axis=1)
        def _status(r):
            cpl, cpb, br, bk, sp = r["cpl"], r["cost_per_booking"], r["booking_rate"] or 0, r["bookings"], r["spend"]
            if (sp or 0) >= 50 and (bk or 0) == 0:               return "Kill"
            if cpb and cpb > target_cpb:                          return "Optimize"
            if br < target_br:                                    return "Optimize"
            if cpl and cpl <= target_cpl and br >= target_br:
                return "Scale" if cpl < target_cpl * 0.75 else "Keep"
            return "Keep"
        perf["status"] = perf.apply(_status, axis=1)
    else:
        perf = pd.DataFrame()

    # Prior-period perf — same structure, used for the comparison deltas on
    # GHL Leads / Bookings / Showed / Cost per Booking. Restricted to Meta
    # campaigns active in the prior window.
    meta_per_pri = run_df("vw_meta_per_campaign", {
        "since": prior_since.isoformat(), "until": prior_until.isoformat(), "account": "All"})
    if not meta_per_pri.empty:
        if ghl_per_pri.empty:
            perf_pri = meta_per_pri.copy()
            for c in ("ghl_leads", "ghl_contacts", "bookings",
                      "open_count", "won_count", "lost_count", "abandoned_count"):
                perf_pri[c] = 0
        else:
            perf_pri = meta_per_pri.merge(
                ghl_per_pri.rename(columns={"campaign": "campaign_name"}),
                on="campaign_name", how="left").fillna(0)
    else:
        perf_pri = pd.DataFrame()

    # Bookings (new logic) = fresh-leads cohort (New Lead stage + filled a form
    # in window) who booked a calendar, split by calendar city (Gurbir/Navneet
    # calendars = Melbourne; every other calendar = Sydney).
    bk_city = run_df("vw_meta_bookings_by_city",
                     {"since": since.isoformat(), "until": until.isoformat()})
    bk_city_map = (dict(zip(bk_city["city"], bk_city["bookings"]))
                   if not bk_city.empty else {})
    bk_city_pri = run_df("vw_meta_bookings_by_city",
                         {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
    bk_city_map_pri = (dict(zip(bk_city_pri["city"], bk_city_pri["bookings"]))
                       if not bk_city_pri.empty else {})

    # ---- Meta-scoped top-scorecard sums (so totals = Mel + Syd account sums) ----
    # GHL Leads = ALL created/revived meta_paid contacts (every campaign, incl.
    # '(no campaign)') so it equals the Forecast tab's Meta Leads — not restricted
    # to currently-active campaigns.
    c_ghl_lds = int(ghl_per["ghl_leads"].sum()) if not ghl_per.empty else 0
    c_book    = int(bk_city_map.get("Melbourne", 0)) + int(bk_city_map.get("Sydney", 0))
    c_showed  = (int(showed_df[showed_df["campaign"].isin(perf["campaign_name"])]["showed"].sum())
                 if not perf.empty and not showed_df.empty else 0)
    c_cpb     = (float(c_spend) / c_book) if c_book else None

    p_ghl_lds = int(ghl_per_pri["ghl_leads"].sum()) if not ghl_per_pri.empty else 0
    p_book    = int(bk_city_map_pri.get("Melbourne", 0)) + int(bk_city_map_pri.get("Sydney", 0))
    p_showed  = (int(showed_df_pri[showed_df_pri["campaign"].isin(perf_pri["campaign_name"])]["showed"].sum())
                 if not perf_pri.empty and not showed_df_pri.empty else 0)
    p_cpb     = (float(p_spend) / p_book) if p_book else None

    METRIC_COL = {"Spend": "spend", "Impressions": "impressions", "Clicks": "clicks",
                  "CTR": "ctr", "Leads (Meta)": "leads", "CPL": "cpl"}

    def _add_account_subtotals(df, group_col, account_col, numeric_cols):
        """Append Melbourne / Sydney subtotals + Grand Total rows to a dataframe."""
        out = df.copy()
        rows = []
        for acct in ("Melbourne", "Sydney"):
            sub = out[out[account_col] == acct]
            if not sub.empty:
                r = {c: "" for c in out.columns}
                r[group_col]   = f"{acct.upper()} SUBTOTAL"
                r[account_col] = acct
                for nc in numeric_cols:
                    r[nc] = int(sub[nc].sum())
                rows.append(r)
        r = {c: "" for c in out.columns}
        r[group_col] = "GRAND TOTAL"
        for nc in numeric_cols:
            r[nc] = int(out[nc].sum())
        rows.append(r)
        return pd.concat([out, pd.DataFrame(rows)], ignore_index=True)

    def _int_fmt(x):
        return f"{int(x):,}" if str(x) not in ("", "nan") else ""

    # ---- Modal dialog: opens on top scorecard click. Shows detail tables with
    #      an account view selector (All / Melbourne / Sydney) and subtotals.
    @st.dialog(" ", width="large")
    def _show_detail_modal():
        metric = st.session_state.get(f"meta_card{kp}", "")
        st.markdown(f"### {metric} — drill-down")

        acct = st.segmented_control(
            "View by account",
            ["All", "Melbourne", "Sydney"],
            default=st.session_state.get(f"meta_dialog_acct{kp}", "All"),
            key=f"meta_dialog_acct{kp}",
        ) or "All"

        if perf.empty:
            st.info("No Meta data in this window.")
            return
        perf_v = perf if acct == "All" else perf[perf["account_label"] == acct]
        show_subtot = (acct == "All")

        def _finalize(df, group_col, account_col, num_cols, acct_label):
            if show_subtot:
                return _add_account_subtotals(df, group_col, account_col, num_cols)
            tot = {c: "" for c in df.columns}
            tot[group_col] = "TOTAL"
            if account_col in df.columns:
                tot[account_col] = acct_label
            for c in num_cols:
                tot[c] = int(df[c].sum())
            return pd.concat([df, pd.DataFrame([tot])], ignore_index=True)

        if metric in ("Leads (Meta)", "GHL Leads"):
            ls = perf_v[["campaign_name", "account_label", "meta_leads",
                         "ghl_leads", "ghl_contacts"]].copy()
            ls = ls.sort_values(["account_label", "ghl_leads"], ascending=[True, False])
            ls.columns = ["Campaign", "Account", "Meta Leads", "Opportunities", "Contacts"]
            ls = _finalize(ls, "Campaign", "Account",
                           ["Meta Leads", "Opportunities", "Contacts"], acct)
            for c in ["Meta Leads", "Opportunities", "Contacts"]:
                ls[c] = ls[c].apply(_int_fmt)
            st.markdown("**Lead Source — opportunities + contacts (per campaign)**")
            st.caption("💡 Click any campaign row below to see all its opportunities (Email · Pipeline · Stage · Status · Total Payment).")
            ls_sel = st.dataframe(
                ls, hide_index=True, use_container_width=True,
                on_select="rerun", selection_mode="single-row",
                key=f"meta_ghl_campaigns_{metric}{kp}",
            )

            # Resolve clicked campaign name (skip TOTAL/subtotal rows)
            picked_campaign = None
            try:
                rs = (ls_sel.selection.get("rows") if ls_sel else None) or []
                if rs:
                    idx = int(rs[0])
                    if 0 <= idx < len(ls):
                        name = ls.iloc[idx]["Campaign"]
                        if name and not str(name).startswith("TOTAL") and "SUBTOTAL" not in str(name).upper():
                            picked_campaign = name
            except Exception:
                picked_campaign = None

            if picked_campaign:
                st.markdown(f"#### 🎯 {picked_campaign} — opportunities")
                # Find all opportunities whose contact's Latest Source campaign
                # matches the picked one, joined to pipeline + stage + payments.
                opp_q = """
                WITH camp_contacts AS (
                    SELECT contact_id FROM fact_contact_latest_source
                    WHERE latest_source_campaign = ?
                ),
                pay AS (
                    SELECT contact_id,
                           SUM(amount - COALESCE(amount_refunded, 0)) AS total_paid
                    FROM fact_payments
                    WHERE LOWER(status) = 'succeeded'
                    GROUP BY contact_id
                )
                SELECT
                    c.email,
                    o.opportunity_id,
                    p.pipeline_name,
                    s.stage_name,
                    o.status,
                    COALESCE(pay.total_paid, 0) AS total_paid,
                    o.created_at
                FROM fact_opportunities o
                JOIN fact_contacts c       ON c.contact_id   = o.contact_id
                LEFT JOIN dim_pipelines p  ON p.pipeline_id  = o.pipeline_id
                LEFT JOIN dim_stages    s  ON s.stage_id     = o.stage_id
                LEFT JOIN pay              ON pay.contact_id = o.contact_id
                WHERE o.contact_id IN (SELECT contact_id FROM camp_contacts)
                ORDER BY o.created_at DESC
                """
                try:
                    opp_drill = get_con().execute(opp_q, [picked_campaign]).fetchdf()
                except Exception as e:
                    st.error(f"opps query failed: {e}")
                    opp_drill = pd.DataFrame()
                if opp_drill.empty:
                    st.info(f"No opportunities found for '{picked_campaign}'.")
                else:
                    out = opp_drill[[
                        "email", "pipeline_name", "stage_name", "status",
                        "total_paid", "created_at",
                    ]].copy()
                    out["pipeline_name"] = out["pipeline_name"].fillna("—")
                    out["stage_name"]    = out["stage_name"].fillna("—")
                    out["status"]        = out["status"].fillna("—")
                    out["total_paid"]    = out["total_paid"].map(
                        lambda v: f"${float(v):,.0f}" if pd.notna(v) and float(v) > 0 else "—")
                    out["created_at"]    = pd.to_datetime(out["created_at"]).dt.strftime("%Y-%m-%d")
                    out.columns = ["Email", "Pipeline", "Stage", "Status",
                                   "Total Payment", "Opp Created"]
                    st.dataframe(out, hide_index=True, use_container_width=True, height=360)
                    st.caption(
                        f"{len(out)} opportunit{'ies' if len(out) != 1 else 'y'} for **{picked_campaign}**. "
                        "Includes ALL opps for these contacts (not just in window) so you can see the full funnel."
                    )

            op = perf_v[["campaign_name", "account_label", "meta_leads", "ghl_leads",
                         "open_count", "won_count", "lost_count", "abandoned_count"]].copy()
            op = op.sort_values(["account_label", "ghl_leads"], ascending=[True, False])
            op.columns = ["Campaign", "Account", "Meta Leads", "GHL Leads",
                          "Open", "Won", "Lost", "Abandoned"]
            num = ["Meta Leads", "GHL Leads", "Open", "Won", "Lost", "Abandoned"]
            op = _finalize(op, "Campaign", "Account", num, acct)
            for c in num:
                op[c] = op[c].apply(_int_fmt)
            st.markdown("**Opportunity status — by campaign**")
            st.dataframe(op, hide_index=True, use_container_width=True)

        elif metric == "Spend":
            s_ = perf_v.sort_values("spend", ascending=False)[
                ["campaign_name", "account_label", "spend", "paid", "paid_amount"]].copy()
            s_ = _finalize(s_, "campaign_name", "account_label",
                           ["spend", "paid", "paid_amount"], acct)
            s_["spend"]       = s_["spend"].apply(lambda x: f"${int(x):,}" if str(x) not in ("","nan") else "")
            s_["paid_amount"] = s_["paid_amount"].apply(lambda x: f"${int(x):,}" if str(x) not in ("","nan") else "")
            s_["paid"]        = s_["paid"].apply(_int_fmt)
            s_.columns = ["Campaign", "Account", "Spend", "Paid", "Paid Amount"]
            st.dataframe(s_, hide_index=True, use_container_width=True)

        elif metric in ("Impressions", "Clicks", "CTR", "CPL"):
            col_ = METRIC_COL[metric]
            s_ = perf_v.sort_values(col_, ascending=False)[
                ["campaign_name", "account_label", col_]].copy()
            s_.columns = ["Campaign", "Account", metric]
            if metric in ("Impressions", "Clicks"):
                s_ = _finalize(s_, "Campaign", "Account", [metric], acct)
                s_[metric] = s_[metric].apply(_int_fmt)
            elif metric == "CTR":
                s_[metric] = s_[metric].map(
                    lambda v: f"{v*100:.2f}%" if isinstance(v, (int, float)) and not pd.isna(v) else "—")
            elif metric == "CPL":
                s_[metric] = s_[metric].map(
                    lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) and not pd.isna(v) else "—")
            st.dataframe(s_, hide_index=True, use_container_width=True)

        elif metric == "Bookings":
            # New logic: the fresh-leads cohort (New Lead stage + filled a form
            # in window) who booked a calendar. City = calendar (Gurbir/Navneet
            # = Melbourne). The 'View by account' control above filters by city.
            bd = run_df("vw_meta_bookings_detail",
                        {"since": since.isoformat(), "until": until.isoformat()})
            if bd.empty:
                st.info("No bookings for this cohort in the selected window.")
            else:
                bd_v = bd if acct == "All" else bd[bd["city"] == acct]
                # ---- Bookings by campaign (summary) ----
                summ = (bd_v.groupby("campaign").size().reset_index(name="Bookings")
                            .sort_values("Bookings", ascending=False))
                summ.columns = ["Campaign", "Bookings"]
                st.markdown("**Bookings by campaign** — cohort leads who booked a calendar")
                st.dataframe(summ, hide_index=True, use_container_width=True)
                # ---- Campaign selector (+ All campaigns) -> emails ----
                camps = ["All campaigns"] + summ["Campaign"].tolist()
                pick = st.selectbox("Show booked emails for", camps,
                                    key=f"meta_book_campaign{kp}")
                em = bd_v if pick == "All campaigns" else bd_v[bd_v["campaign"] == pick]
                out = em[["email", "campaign", "city", "pipeline", "stage", "status"]].copy()
                out.columns = ["Email", "Campaign", "City", "Pipeline", "Stage", "Status"]
                out = out.sort_values(["Campaign", "Email"])
                st.markdown(f"#### Booked leads — {pick}")
                st.dataframe(out, hide_index=True, use_container_width=True, height=360)
                st.caption(
                    f"{len(out)} booked lead(s)"
                    f"{'' if acct == 'All' else f' in {acct}'}. "
                    "City = calendar (Gurbir/Navneet = Melbourne; all other calendars = Sydney). "
                    "Pipeline/Stage/Status = the contact's latest opportunity."
                )

        elif metric == "Showed":
            # Restrict to Meta campaigns only (filter out 'Direct traffic',
            # 'Social media', '(no latest source)', etc.) by joining to perf.
            if not showed_df.empty and not perf.empty:
                meta_acct = perf.set_index("campaign_name")["account_label"].to_dict()
                s_ = showed_df[showed_df["campaign"].isin(meta_acct.keys())].copy()
                s_["account_label"] = s_["campaign"].map(meta_acct)
                if acct != "All":
                    s_ = s_[s_["account_label"] == acct]
                s_ = s_[["campaign", "account_label", "showed", "noshowed", "appts"]]
                s_.columns = ["Campaign", "Account", "Showed", "No-show", "Appts"]
                s_ = s_.sort_values("Showed", ascending=False)
                s_ = _finalize(s_, "Campaign", "Account",
                               ["Showed", "No-show", "Appts"], acct)
                for c in ["Showed", "No-show", "Appts"]:
                    s_[c] = s_[c].apply(_int_fmt)
                st.dataframe(s_, hide_index=True, use_container_width=True)

        elif metric == "Cost per Booking":
            s_ = perf_v.sort_values("bookings", ascending=False)[
                ["campaign_name", "account_label", "spend", "bookings",
                 "cost_per_booking", "status"]].copy()
            s_.columns = ["Campaign", "Account", "Spend", "Bookings", "Cost/Booking", "Status"]
            s_ = _finalize(s_, "Campaign", "Account", ["Spend", "Bookings"], acct)
            s_["Spend"]    = s_["Spend"].apply(lambda x: f"${int(x):,}" if str(x) not in ("","nan") else "")
            s_["Bookings"] = s_["Bookings"].apply(_int_fmt)
            s_["Cost/Booking"] = s_["Cost/Booking"].apply(
                lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) and not pd.isna(v) else "—")
            st.dataframe(s_, hide_index=True, use_container_width=True)

    # ---- 10 top scorecards — clicking opens the modal drill-down ----
    VAL = {
        "Spend": fmt_money_full(c_spend),
        "Impressions": fmt_int(c_impr),
        "Clicks": fmt_int(c_clk),
        "CTR": fmt_pct_2dp(c_ctr),
        "Leads (Meta)": fmt_int(c_lds),
        "GHL Leads": fmt_int(c_ghl_lds),
        "CPL": fmt_money_full(c_cpl) if c_cpl else "—",
        "Bookings": fmt_int(c_book),
        "Showed": fmt_int(c_showed),
        "Cost per Booking": fmt_money_full(c_cpb) if c_cpb else "—",
    }
    DELTAS = {
        "Spend":            _delta_md(c_spend,   p_spend,   higher_is_better=True),
        "Impressions":      _delta_md(c_impr,    p_impr,    higher_is_better=True),
        "Clicks":           _delta_md(c_clk,     p_clk,     higher_is_better=True),
        "CTR":              _delta_md(c_ctr,     p_ctr,     higher_is_better=True),
        "Leads (Meta)":     _delta_md(c_lds,     p_lds,     higher_is_better=True),
        "GHL Leads":        _delta_md(c_ghl_lds, p_ghl_lds, higher_is_better=True),
        "CPL":              _delta_md(c_cpl,     p_cpl,     higher_is_better=False),
        "Bookings":         _delta_md(c_book,    p_book,    higher_is_better=True),
        "Showed":           _delta_md(c_showed,  p_showed,  higher_is_better=True),
        "Cost per Booking": _delta_md(c_cpb,     p_cpb,     higher_is_better=False),
    }
    METRICS = ["Spend", "Impressions", "Clicks", "CTR", "Leads (Meta)",
               "GHL Leads", "CPL", "Bookings", "Showed", "Cost per Booking"]

    def _scorecard_button(col, label_text, value_text, scorecard_label, key_suffix,
                          delta_text="", open_modal=False, pre_account=None):
        """Scorecard-styled button. If open_modal=True the click opens the drill-down dialog;
        otherwise it just updates the active metric (for account-card metric tiles)."""
        is_active = (scorecard_label == active)
        lines = [label_text, value_text]
        if delta_text:
            lines.append(delta_text)
        button_label = "\n\n".join(lines)
        if col.button(
            button_label,
            key=f"mb_{scorecard_label}_{key_suffix}{kp}",
            use_container_width=True,
            type=("primary" if is_active else "secondary"),
        ):
            st.session_state[f"meta_card{kp}"] = scorecard_label
            if pre_account:
                st.session_state[f"meta_dialog_acct{kp}"] = pre_account
            if open_modal:
                _show_detail_modal()
            else:
                st.rerun()

    row1 = st.columns(5)
    for i in range(5):
        m = METRICS[i]
        _scorecard_button(row1[i], m.upper(), VAL[m], m, "sc1",
                          delta_text=DELTAS.get(m, ""), open_modal=False)
    row2 = st.columns(5)
    for i in range(5):
        m = METRICS[i+5]
        _scorecard_button(row2[i], m.upper(), VAL[m], m, "sc2",
                          delta_text=DELTAS.get(m, ""), open_modal=False)

    # ---- Trend / Table view, driven by the ACTIVE top scorecard ----
    import altair as alt
    st.markdown("---")

    def _render_meta_table(metric):
        """Per-metric campaign table (columns per the Marketing Lead's spec)."""
        if metric == "GHL Leads":
            gl = run_df("vw_exec_unified_leads",
                        {"since": since.isoformat(), "until": until.isoformat()})
            gl = gl[gl["source_bucket"] == "Meta Paid"] if not gl.empty else gl
            if gl is None or gl.empty:
                st.info("No Meta-attributed GHL leads in this window.")
                return
            t = gl[["email", "latest_source", "pipeline", "stage"]].copy()
            t["latest_source"] = t["latest_source"].replace("", "—").fillna("—")
            t["pipeline"]      = t["pipeline"].fillna("—")
            t["stage"]         = t["stage"].fillna("—")
            t.columns = ["Email", "Latest Source", "Pipeline", "Stage"]
            st.dataframe(t.sort_values("Latest Source"), hide_index=True,
                         use_container_width=True, height=420)
            st.caption(f"{len(t):,} Meta-attributed GHL leads (latest form/survey in window). "
                       "Latest Source = live 8-step logic.")
            return

        if perf.empty:
            st.info("No Meta data in this window.")
            return

        if metric == "Spend":
            t = perf.sort_values("spend", ascending=False)[["campaign_name", "account_label", "spend"]].copy()
            t["spend"] = t["spend"].map(lambda x: f"${x:,.0f}")
            t.columns = ["Campaign", "Account", "Spend"]
        elif metric == "Impressions":
            t = perf.sort_values("impressions", ascending=False)[["campaign_name", "account_label", "impressions"]].copy()
            t["impressions"] = t["impressions"].map(lambda x: f"{int(x):,}")
            t.columns = ["Campaign", "Account", "Impressions"]
        elif metric == "Clicks":
            t = perf.sort_values("clicks", ascending=False)[["campaign_name", "account_label", "clicks"]].copy()
            t["clicks"] = t["clicks"].map(lambda x: f"{int(x):,}")
            t.columns = ["Campaign", "Account", "Clicks"]
        elif metric == "CTR":
            t = perf.sort_values("ctr", ascending=False)[["campaign_name", "account_label", "ctr"]].copy()
            t["ctr"] = t["ctr"].map(lambda v: f"{v*100:.2f}%" if pd.notna(v) else "—")
            t.columns = ["Campaign", "Account", "CTR"]
        elif metric == "Leads (Meta)":
            t = perf.sort_values("meta_leads", ascending=False)[["campaign_name", "account_label", "meta_leads"]].copy()
            t["meta_leads"] = t["meta_leads"].map(lambda x: f"{int(x):,}")
            t.columns = ["Campaign", "Account", "Leads"]
        elif metric == "CPL":
            t = perf[perf["cpl"].notna()].sort_values("cpl")[["campaign_name", "account_label", "cpl"]].copy()
            t["cpl"] = t["cpl"].map(lambda v: f"${v:,.2f}")
            t.columns = ["Campaign", "Account", "CPL"]
        elif metric in ("Bookings", "Cost per Booking"):
            t = perf.sort_values("bookings", ascending=False)[
                ["campaign_name", "spend", "bookings", "cost_per_booking", "booking_rate"]].copy()
            t["spend"]            = t["spend"].map(lambda x: f"${x:,.0f}")
            t["bookings"]         = t["bookings"].map(lambda x: f"{int(x):,}")
            t["cost_per_booking"] = t["cost_per_booking"].map(lambda v: f"${v:,.2f}" if pd.notna(v) else "—")
            t["booking_rate"]     = t["booking_rate"].map(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—")
            t.columns = ["Campaign", "Spend", "Bookings", "Cost per Booking", "Booking Rate"]
        elif metric == "Showed":
            sd = showed_df[showed_df["campaign"].isin(perf["campaign_name"])].copy() \
                if not showed_df.empty else pd.DataFrame()
            m = perf[["campaign_name", "ghl_leads", "booking_rate"]].rename(columns={"campaign_name": "campaign"})
            t = sd.merge(m, on="campaign", how="left") if not sd.empty else pd.DataFrame()
            if t.empty:
                st.info("No showed appointments for Meta campaigns in this window.")
                return
            t["show_rate"]    = t.apply(lambda r: r["showed"] / r["appts"] if r["appts"] else None, axis=1)
            t = t.sort_values("showed", ascending=False)[["campaign", "ghl_leads", "booking_rate", "show_rate"]]
            t["ghl_leads"]    = t["ghl_leads"].map(lambda x: f"{int(x):,}" if pd.notna(x) else "—")
            t["booking_rate"] = t["booking_rate"].map(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—")
            t["show_rate"]    = t["show_rate"].map(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—")
            t.columns = ["Campaign", "Leads", "Booking Rate", "Show Rate"]
        else:
            st.info(f"No table defined for {metric}.")
            return
        st.dataframe(t, hide_index=True, use_container_width=True,
                     height=min(560, 60 + 30 * len(t)))

    h1, h2 = st.columns([3, 2])
    with h1:
        st.markdown(f"#### {active}")
    with h2:
        # Drive purely from session_state (default= + key= together desyncs).
        if f"meta_view{kp}" not in st.session_state:
            st.session_state[f"meta_view{kp}"] = "Table"
        meta_view = st.segmented_control(
            "view", ["Trend", "Table"], key=f"meta_view{kp}",
            label_visibility="collapsed",
        ) or "Table"

    if meta_view == "Trend":
        col_name = METRIC_COL.get(active)
        trend = run_df("vw_meta_daily_trend",
                       {"since": since.isoformat(), "until": until.isoformat()})
        if col_name and not trend.empty:
            g = (trend.groupby("date")
                      .agg(spend=("spend", "sum"), leads=("leads", "sum"),
                           impressions=("impressions", "sum"), clicks=("clicks", "sum"))
                      .reset_index())
            g["ctr"] = g.apply(lambda r: r["clicks"] / r["impressions"] if r["impressions"] else None, axis=1)
            g["cpl"] = g.apply(lambda r: r["spend"] / r["leads"] if r["leads"] else None, axis=1)
            g["date"] = pd.to_datetime(g["date"])
            chart = (
                alt.Chart(g)
                .mark_area(interpolate="monotone", color="#3b82f6", opacity=0.20,
                           line={"color": "#3b82f6", "strokeWidth": 2.5})
                .encode(
                    x=alt.X("date:T", title=None,
                            axis=alt.Axis(format="%b %d", tickCount=8, labelFontSize=11,
                                          grid=False, domain=False, ticks=False)),
                    y=alt.Y(f"{col_name}:Q", title=None,
                            axis=alt.Axis(labelFontSize=11, grid=True, gridColor="#e5e7eb",
                                          domain=False, ticks=False)),
                    tooltip=[alt.Tooltip("date:T", title="Date", format="%b %d"),
                             alt.Tooltip(f"{col_name}:Q", title=active)],
                )
                .properties(height=300)
                .configure_view(strokeWidth=0)
            )
            st.altair_chart(chart, use_container_width=True)
            st.caption(f"{active} — daily trend across all Meta campaigns.")
        else:
            st.info(f"**{active}** has no daily Meta series (it's GHL-derived). Switch to **Table**.")
    else:
        _render_meta_table(active)

    # ---- Campaign Performance table ----
    st.markdown("### Campaign Performance — sorted by spend")
    if perf.empty:
        st.info("No Meta data in this window.")
    else:
        v = perf.sort_values("spend", ascending=False).copy()
        # Force integer formatting on counts (no .0 decimals from earlier fillna)
        v["meta_leads"]       = v["meta_leads"].astype("int64").map(lambda x: f"{x:,}")
        v["ghl_leads"]        = v["ghl_leads"].astype("int64").map(lambda x: f"{x:,}")
        v["bookings"]         = v["bookings"].astype("int64").map(lambda x: f"{x:,}")
        v["paid"]             = v["paid"].astype("int64").map(lambda x: f"{x:,}")
        v["Spend"]            = v["spend"].map(lambda x: f"${x:,.0f}")
        v["CPL"]              = v["cpl"].map(lambda x: f"${x:,.2f}" if pd.notna(x) else "—")
        v["Cost/Booking"]     = v["cost_per_booking"].map(lambda x: f"${x:,.2f}" if pd.notna(x) else "—")
        v["Booking Rate"]     = v["booking_rate"].map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
        v["Paid Cons. Rate"]  = v["paid_cons_rate"].map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
        cols_ = ["campaign_name", "account_label", "Spend", "meta_leads", "CPL",
                 "ghl_leads", "bookings", "paid", "Booking Rate", "Paid Cons. Rate",
                 "Cost/Booking", "status"]
        v = v[cols_].rename(columns={
            "campaign_name": "Campaign", "account_label": "Account",
            "meta_leads": "Meta Leads", "ghl_leads": "GHL Leads",
            "bookings": "Bookings", "paid": "Paid", "status": "Status"})
        STATUS_STYLES = {
            "Scale":    "background-color:#d1fae5; color:#065f46; font-weight:700; border-radius:999px; padding:2px 10px;",
            "Keep":     "background-color:#dbeafe; color:#1e40af; font-weight:700; border-radius:999px; padding:2px 10px;",
            "Optimize": "background-color:#fef3c7; color:#92400e; font-weight:700; border-radius:999px; padding:2px 10px;",
            "Kill":     "background-color:#fee2e2; color:#991b1b; font-weight:700; border-radius:999px; padding:2px 10px;",
        }
        styled = v.style.map(lambda x: STATUS_STYLES.get(x, ""), subset=["Status"])
        st.dataframe(styled, hide_index=True, use_container_width=True)

    # ---- Status thresholds (moved BELOW the table — user-adjustable, dynamic) ----
    st.markdown("---")
    st.markdown("### Status thresholds")
    th = st.columns([1, 1, 1, 3])
    th[0].number_input("Target CPL ($)", value=20.0, min_value=0.0, step=1.0, key=f"meta_tcpl{kp}")
    th[1].number_input("Target Booking Rate (%)", value=20.0, min_value=0.0, max_value=100.0, step=1.0, key=f"meta_tbr{kp}")
    th[2].number_input("Target Cost / Booking ($)", value=200.0, min_value=0.0, step=10.0, key=f"meta_tcpb{kp}")
    st.caption(
        "Adjust thresholds and the **Status** column above (Scale / Keep / Optimize / Kill) recalculates "
        "live. Logic: **Kill** = spend ≥ $50 with 0 bookings · **Optimize** = CPB > target *or* booking rate "
        "< target · **Scale** = CPL < 0.75× target *and* booking rate ≥ target · **Keep** otherwise."
    )

    # ---- Documentation (opens as a modal like the scorecards) ----
    @st.dialog("Meta Ads tab — How these numbers work", width="large")
    def _show_docs_modal():
        st.markdown("""
### 📖 Tab overview
The **Meta Ads** tab joins three live data sources to give one view of paid-Meta performance:

| Source | What we use it for |
|---|---|
| **Meta Ads API** (`fact_meta_daily`) | Spend, Impressions, Clicks, CTR, Meta Leads (Results), CPL |
| **GHL Opportunities + Forms** (`fact_opportunities`, `fact_form_submissions`) | GHL Leads, Bookings, Opp Status, Lead Source |
| **GHL Appointments + Payments** (`fact_appointments`, `/payments/transactions`) | Showed, Converted, Paid Cons. Rate |

All metrics on this tab are **restricted to Meta campaigns** (campaigns that have Meta delivery in the window) — so top scorecards = Melbourne + Sydney account-card sums.

---
### 🧮 How each metric is calculated

**Spend, Impressions, Clicks, CTR**
`SUM(...)` from `fact_meta_daily` for the window. CTR = Clicks ÷ Impressions.

**Leads (Meta)**
`SUM(result_count)` from `fact_meta_daily` — matches Meta Ads Manager's "Results" column.

**CPL**
`Meta Spend ÷ Leads (Meta)`.

**GHL Leads**
Distinct opportunities (deduped by `opportunity_id`) where EITHER:
- `opp.created_at` ∈ window, **OR**
- The contact submitted a form ∈ window (returning-lead signal).

Grouped by the contact's **Latest Source** custom field; **restricted to Meta campaigns**.

**Bookings**
Subset of GHL Leads whose pipeline + stage is in:
- **L2C - Education**: Appointment Booked · Post Consultation · No Show · Initial Requested · Initial Received · COE Received
- **L2C - VISA**: all 5 stages **except** *High Potential Clients*
- **CLT - Onshore Admission**: all 10 stages

**Showed**
Count of `fact_appointments` where `canonical_outcome = 'show'` and `date_added` ∈ window, restricted to contacts whose Latest Source is a Meta campaign.

**Cost per Booking (CPB)**
`Meta Spend ÷ Bookings` (both Meta-scoped, so the denominator matches).

**Converted** *(Mel/Syd cards — currently red-outlined as a provisional figure)*
Distinct paying contacts (succeeded transactions, `amount − amountRefunded > 0`) in the window, attributed via the contact's Latest Source.

**Booking Rate** = Bookings ÷ GHL Leads
**Paid Cons. Rate** = Paid (Converted) ÷ GHL Leads

---
### 🖱️ Interactivity — what you can click

| Element | What happens |
|---|---|
| **Top 10 scorecards** | Click → opens a drill-down dialog with per-campaign tables + an **All / Melbourne / Sydney** account selector and Mel/Syd/Grand subtotals |
| **Leads / CPL / CTR / Booked / Showed** inside Mel/Syd cards | Click → switches the daily-trend chart on both account cards to that metric |
| **CONVERTED** card | Currently static (red outline — provisional, to be reconciled with payments later) |
| **Campaign Performance** table | Sorted by spend; Status column is colour-badged (Scale/Keep/Optimize/Kill) |
| **Status thresholds** | Live inputs — adjust and the Status column above recalculates immediately |

---
### 📊 Comparison deltas (▲ / ▼ vs last)
Every top scorecard shows `▲ X% vs last` or `▼ X%` compared to the **prior equal-length window** (e.g. if the current window is 24 days, the comparison is the 24 days immediately before).
- Green arrow = improvement (up for Spend / Leads / Bookings; down for CPL / CPB).
- Red arrow = regression.

---
### 🟢🔵🟡🔴 Status classification (live)
Auto-assigned per campaign in the Campaign Performance table:
- **Scale** — CPL < 0.75 × target *and* booking rate ≥ target → expand budget
- **Keep** — CPL ≤ target *and* booking rate ≥ target → maintain
- **Optimize** — CPB > target *or* booking rate < target → tweak creative / audience
- **Kill** — Spend ≥ $50 *and* zero bookings → pause

Three live inputs drive the classification:
- Target CPL (default $20)
- Target Booking Rate (default 20%)
- Target Cost per Booking (default $200)

---
### 💵 Currency
All money values are **AUD**. Meta accounts are AUD-denominated; GHL `transactions` carry `currency: "AUD"`. No FX conversion is applied.

---
### 🔍 Drill-down dialog tables

When you click a scorecard, the dialog shows:

**Leads (Meta) / GHL Leads**
- **Lead Source** — per Meta campaign: Account · Meta Leads · Opportunities · Contacts
- **Opportunity Status** — per Meta campaign: Open · Won · Lost · Abandoned (with Meta Leads & GHL Leads totals)

**Spend** — Campaign · Account · Spend · Paid · Paid Amount

**Impressions / Clicks** — Campaign · Account · metric value (with Mel/Syd subtotals)

**CTR / CPL** — Campaign · Account · metric value (no subtotals — these are ratios, not summable)

**Bookings** — Campaign · Account · Meta Leads · GHL Leads · Bookings · Booking Rate

**Showed** — Campaign · Account · Showed · No-show · Appts (Meta campaigns only)

**Cost per Booking** — Campaign · Account · Spend · Bookings · Cost/Booking · Status

---
### 📅 Date / period
Driven by the **Period** picker at the top of the dashboard (Current month / Last 30 days / Last 7 days / Custom).
The comparison line uses the same length of days immediately before the current window.

---
### ⚠️ Known caveats
- **CONVERTED** (Mel/Syd) is provisional pending a reconciliation pass with `fact_payments` ingestion (flagged in red).
- **Daily-trend chart** is only available for Meta-side metrics (Spend / Impressions / Clicks / CTR / Leads / CPL). GHL-derived metrics (Bookings / Showed / GHL Leads / Cost per Booking) show a "not available" caption — a daily GHL-fact view can be added on request.
- The Latest Source custom field on contacts is maintained by the hourly ETL `sync_latest_source` step. New form fills propagate within ~1 hour.
""")

    if st.button("📘 Documentation — how these numbers are calculated",
                 key=f"meta_docs_btn{kp}", use_container_width=True):
        _show_docs_modal()


@st.cache_data(ttl=900, show_spinner=False)
def fetch_meta1_campaign_df(since_s, until_s):
    """Live per-campaign Meta insights (both ad accounts) for a window.
    Read-only Meta Ads API — money stays in account currency (USD); the tab
    converts to AUD. Cached 15 min so scorecard/modal clicks don't re-hit Meta."""
    import os as _os
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import connectors.meta as _meta
    except Exception:
        return pd.DataFrame()
    LEADSET = {"lead", "offsite_conversion.fb_pixel_lead", "offsite_conversion.fb_pixel_custom"}
    accts = [("Melbourne", _os.getenv("META_MELBOURNE_AD_ACCOUNT_ID")),
             ("Sydney",    _os.getenv("META_SYDNEY_AD_ACCOUNT_ID"))]
    rows = []
    for label, acct in accts:
        if not acct:
            continue
        try:
            raw = _meta.fetch_campaign_insights(acct, since_s, until_s)
        except Exception:
            continue
        for r in raw:
            acts = r.get("actions") or []
            amap = {a.get("action_type"): float(a.get("value") or 0)
                    for a in acts if a.get("action_type")}
            rows.append({
                "account": label,
                "campaign": (r.get("campaign_name") or "(unnamed)"),
                "spend": float(r.get("spend") or 0),
                "impressions": int(float(r.get("impressions") or 0)),
                "reach": int(float(r.get("reach") or 0)),
                "clicks": int(float(r.get("clicks") or 0)),
                "link_clicks": int(float(r.get("inline_link_clicks") or 0)),
                "page_engagement": int(amap.get("page_engagement", 0)),
                "messaging_started": int(amap.get("onsite_conversion.messaging_conversation_started_7d", 0)),
                "meta_leads": int(sum(v for k, v in amap.items() if k in LEADSET)),
            })
    return pd.DataFrame(rows)


@st.cache_data(ttl=1800, show_spinner=False)
def _meta1_lifetime_perf(until_s, fx):
    """Lifetime per-campaign performance (all-time Meta spend + all-time GHL
    Paid-Social leads/bookings/shows), keyed by normalised campaign key. Used to
    score INACTIVE campaigns by how they performed historically."""
    def ck(s):
        s = str(s or "").lower()
        for a, b in (("%7c", "|"), ("%2f", "/"), ("%2b", "+"), ("%20", " "),
                     ("%26", "&"), ("+", " ")):
            s = s.replace(a, b)
        return re.sub(r"[^a-z0-9]", "", s)
    e = run_df("vw_exec1_lead_detail", {"since": "2024-01-01", "until": until_s})
    e = e[e["refined_source"] == "Paid Social"].copy()
    e["_k"] = e["campaign"].fillna("").map(ck)
    g = e.groupby("_k").agg(leads=("contact_id", "count"),
                            booked=("appt_booked", "sum"), showed=("appt_showed", "sum"))
    md = get_con().execute("SELECT campaign_name, COALESCE(SUM(spend),0) sp FROM "
                           "fact_meta_daily WHERE campaign_name IS NOT NULL GROUP BY 1").fetchdf()
    msp = {}
    for _, rr in md.iterrows():
        k = ck(rr["campaign_name"]); msp[k] = msp.get(k, 0) + float(rr["sp"])
    life = {}
    for k, row in g.iterrows():
        sp = msp.get(k, 0) * fx; ld = int(row.leads); bk = int(row.booked); sh = int(row.showed)
        life[k] = {"spend_aud": sp, "leads": ld, "booked": bk,
                   "cpl": sp / ld if ld else None, "cpa": sp / bk if bk else None,
                   "booking_rate": bk / ld if ld else None, "show_rate": sh / bk if bk else None}
    return life


def render_meta1_tab():
    """Meta_1 — campaign performance from the LIVE Meta Ads API joined to the
    Executive_1 Paid-Social GHL cohort (created+revived). 13 clickable scorecards
    with per-campaign drill-down modals (city selector + email-level leads)."""
    fx = usd_to_aud()
    st.markdown("<div class='panel-title'>Meta_1 — campaign performance"
                "<span class='hint'>live Meta Ads API + GHL Paid-Social cohort</span></div>",
                unsafe_allow_html=True)

    m_cur = fetch_meta1_campaign_df(since.isoformat(), until.isoformat())
    m_pri = fetch_meta1_campaign_df(prior_since.isoformat(), prior_until.isoformat())
    if m_cur.empty:
        st.warning("No live Meta data (check META_ACCESS_TOKEN / account IDs in .env). "
                   "GHL-side metrics still shown.")

    e1  = run_df("vw_exec1_lead_detail", {"since": since.isoformat(), "until": until.isoformat()})
    e1p = run_df("vw_exec1_lead_detail", {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
    cv  = run_df("vw_exec1_conversions", {"since": since.isoformat(), "until": until.isoformat()})
    cvp = run_df("vw_exec1_conversions", {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
    # Meta_1 Conversions stays COE-only (L2C-Edu / CLT-Onshore), excluding POC.
    conv_ids = set(cv[cv["conv_type"] == "COE"]["contact_id"]) if not cv.empty else set()
    conv_ids_p = set(cvp[cvp["conv_type"] == "COE"]["contact_id"]) if not cvp.empty else set()
    # Normalised campaign key — Meta names vs GHL utm_campaign differ in
    # encoding/whitespace, so match on a lowercased alphanumeric-only key.
    def _ckey(s):
        s = str(s or "").lower()
        for a, b in (("%7c", "|"), ("%2f", "/"), ("%2b", "+"), ("%20", " "),
                     ("%26", "&"), ("+", " ")):
            s = s.replace(a, b)
        return re.sub(r"[^a-z0-9]", "", s)

    if not m_cur.empty:
        m_cur = m_cur.assign(_k=m_cur["campaign"].map(_ckey))
    if not m_pri.empty:
        m_pri = m_pri.assign(_k=m_pri["campaign"].map(_ckey))
    camp_acct = dict(zip(m_cur["_k"], m_cur["account"])) if not m_cur.empty else {}
    meta_keys = set(m_cur["_k"]) if not m_cur.empty else set()
    meta_keys_p = set(m_pri["_k"]) if not m_pri.empty else set()
    # All-time campaign → city (Melbourne / Sydney) from the warehouse, so a lead
    # whose campaign didn't deliver in THIS window is still attributed to its ad
    # account. account_label is already 'Melbourne' / 'Sydney'.
    try:
        _cc_rows = get_con().execute(
            "SELECT DISTINCT campaign_name, account_label FROM ("
            "  SELECT campaign_name, account_label FROM fact_meta_daily "
            "  UNION ALL SELECT campaign_name, account_label FROM fact_meta_insights) "
            "WHERE COALESCE(campaign_name,'') <> ''").fetchall()
        camp_city = {_ckey(n): lbl for n, lbl in _cc_rows if lbl}
    except Exception:
        camp_city = {}

    def _ps(df, cids, mkeys=None):
        # Meta cohort = exactly Executive_1's Paid Social (the view now classifies
        # utm_campaign-matched leads as Paid Social), so the two tabs match.
        d = df[df["refined_source"] == "Paid Social"].copy()
        d["campaign"] = d["campaign"].fillna("(no campaign)").replace("", "(no campaign)")
        d["_k"] = d["campaign"].map(_ckey)
        d["is_conv"] = d["contact_id"].isin(cids).astype(int)
        # city from the campaign's ad account: all-time warehouse map first
        # (covers campaigns not delivering this window), then the live map.
        d["account"] = (d["_k"].map(camp_city)
                        .fillna(d["_k"].map(camp_acct)).fillna("—"))
        return d
    ps, psp = _ps(e1, conv_ids), _ps(e1p, conv_ids_p)

    # Global City filter (top of page): Melbourne / Sydney restrict the Meta tab
    # to that ad account and its leads (account is the campaign's ad account).
    # All / Others / Unidentified show everything (no Meta-side city for those).
    if city in ("Melbourne", "Sydney"):
        if not m_cur.empty:
            m_cur = m_cur[m_cur["account"] == city]
        if not m_pri.empty:
            m_pri = m_pri[m_pri["account"] == city]
        ps  = ps[ps["account"] == city].copy()
        psp = psp[psp["account"] == city].copy()

    def _agg(m, p):
        return dict(
            spend=(float(m["spend"].sum()) * fx if not m.empty else 0.0),
            impr=(int(m["impressions"].sum()) if not m.empty else 0),
            reach=(int(m["reach"].sum()) if not m.empty else 0),
            lclk=(int(m["link_clicks"].sum()) if not m.empty else 0),
            peng=(int(m["page_engagement"].sum()) if not m.empty else 0),
            msg=(int(m["messaging_started"].sum()) if not m.empty else 0),
            leads=len(p), booked=int(p["appt_booked"].sum()) if len(p) else 0,
            showed=int(p["appt_showed"].sum()) if len(p) else 0,
            conv=int(p["is_conv"].sum()) if len(p) else 0)
    C, P = _agg(m_cur, ps), _agg(m_pri, psp)

    def _rate(n, d): return (n / d) if d else 0.0
    def _div(n, d): return (n / d) if d else None
    cpl, p_cpl = _div(C["spend"], C["leads"]), _div(P["spend"], P["leads"])
    cplc, p_cplc = _div(C["spend"], C["lclk"]), _div(P["spend"], P["lclk"])
    cpm = (_div(C["spend"], C["impr"]) or 0) * 1000
    p_cpm = (_div(P["spend"], P["impr"]) or 0) * 1000
    lctr, p_lctr = _rate(C["lclk"], C["impr"]) * 100, _rate(P["lclk"], P["impr"]) * 100
    br, p_br = _rate(C["booked"], C["leads"]), _rate(P["booked"], P["leads"])
    shr, p_shr = _rate(C["showed"], C["booked"]), _rate(P["showed"], P["booked"])
    cpa, p_cpa = _div(C["spend"], C["booked"]), _div(P["spend"], P["booked"])

    # ---- per-campaign combined frame (joined on the normalised key) ----
    if not m_cur.empty:
        mc = (m_cur.groupby("_k").agg(
                campaign=("campaign", "first"), account=("account", "first"),
                spend=("spend", "sum"), impressions=("impressions", "sum"),
                reach=("reach", "sum"), clicks=("clicks", "sum"),
                link_clicks=("link_clicks", "sum"),
                page_engagement=("page_engagement", "sum"),
                messaging_started=("messaging_started", "sum"),
                meta_leads=("meta_leads", "sum")).reset_index())
    else:
        mc = pd.DataFrame(columns=["_k", "campaign", "account", "spend", "impressions", "reach",
                                   "clicks", "link_clicks", "page_engagement",
                                   "messaging_started", "meta_leads"])
    gh = (ps.groupby("_k").agg(g_campaign=("campaign", "first"),
            leads=("contact_id", "count"), booked=("appt_booked", "sum"),
            showed=("appt_showed", "sum"), conv=("is_conv", "sum")).reset_index())
    camp = mc.merge(gh, on="_k", how="outer")
    camp["campaign"] = camp["campaign"].fillna(camp.get("g_campaign"))
    for c in ["spend", "impressions", "reach", "clicks", "link_clicks", "page_engagement",
              "messaging_started", "meta_leads", "leads", "booked", "showed", "conv"]:
        camp[c] = pd.to_numeric(camp.get(c), errors="coerce").fillna(0)
    # Social-channel DM inquiries (Facebook / Instagram / WhatsApp / TikTok) —
    # NOT leads; shown as a "Queries" row inside the Messaging Conversations
    # Started drill-down (built below), never in the Leads / other tables.
    socq = e1[(e1["refined_source"] == "Queries")
              & (e1["dm_channel"].isin(["Facebook", "Instagram", "WhatsApp", "TikTok"]))]
    camp["account"] = camp["account"].fillna(camp["_k"].map(camp_acct)).fillna("—")
    camp["spend_aud"] = camp["spend"] * fx
    camp["cpl"] = camp.apply(lambda r: r.spend_aud / r.leads if r.leads else None, axis=1)
    camp["booking_rate"] = camp.apply(lambda r: r.booked / r.leads if r.leads else None, axis=1)
    camp["show_rate"] = camp.apply(lambda r: r.showed / r.booked if r.booked else None, axis=1)
    camp["cpa"] = camp.apply(lambda r: r.spend_aud / r.booked if r.booked else None, axis=1)
    camp["cpm"] = camp.apply(lambda r: r.spend_aud / r.impressions * 1000 if r.impressions else None, axis=1)
    camp["link_ctr"] = camp.apply(lambda r: r.link_clicks / r.impressions * 100 if r.impressions else None, axis=1)
    camp["cplc"] = camp.apply(lambda r: r.spend_aud / r.link_clicks if r.link_clicks else None, axis=1)
    # Campaign status in the window: Active = Meta delivery (spend/impr) > 0;
    # Inactive = a campaign with GHL leads but no delivery this period; Other =
    # Queries / "(no campaign)" (not a campaign).
    def _cstatus(r):
        if r["campaign"] in ("Queries", "(no campaign)"):
            return "Other"
        return "Active" if (r["spend"] > 0 or r["impressions"] > 0) else "Inactive"
    camp["status"] = camp.apply(_cstatus, axis=1)
    active_keys_cur = set(camp.loc[camp["status"] == "Active", "_k"])

    # standalone "Queries" row (only used in the Messaging Conversations drill)
    if len(socq):
        _ql = len(socq); _qb = int(socq["appt_booked"].sum()); _qs = int(socq["appt_showed"].sum())
        qrow_df = pd.DataFrame([{
            "_k": "__queries__", "campaign": "Queries", "account": "—",
            "spend": 0, "impressions": 0, "reach": 0, "clicks": 0, "link_clicks": 0,
            "page_engagement": 0, "messaging_started": 0, "meta_leads": 0,
            "leads": _ql, "booked": _qb, "showed": _qs, "conv": 0, "spend_aud": 0.0,
            "cpl": None, "booking_rate": (_qb / _ql if _ql else None),
            "show_rate": (_qs / _qb if _qb else None), "cpa": None, "cpm": None,
            "link_ctr": None, "cplc": None, "status": "Other",
        }])
    else:
        qrow_df = pd.DataFrame()

    # prior-period per-campaign booking / show rates (keyed on _k) so each row's
    # Booked / Showed cell can show ▲/▼ vs the previous period (like Executive_1).
    if not psp.empty:
        _pg = psp.groupby("_k").agg(_pl=("contact_id", "count"),
                                    _pb=("appt_booked", "sum"),
                                    _psh=("appt_showed", "sum"))
        _prbr = {k: (r["_pb"] / r["_pl"] if r["_pl"] else None) for k, r in _pg.iterrows()}
        _prsr = {k: (r["_psh"] / r["_pb"] if r["_pb"] else None) for k, r in _pg.iterrows()}
    else:
        _prbr, _prsr = {}, {}

    # ---- formatters + table builders ----
    def _money(v): return f"${v:,.0f}" if pd.notna(v) else "—"
    def _money2(v): return f"${v:,.2f}" if pd.notna(v) else "—"
    def _pct(v): return f"{v*100:.0f}%" if pd.notna(v) else "—"
    def _pctn(v): return f"{v:.2f}%" if pd.notna(v) else "—"

    def _bs_cell(cnt, rate, prior):
        """count · rate% · ▲/▼<pts> vs prior period (like Executive_1 Table 1)."""
        if pd.isna(rate):
            return f"{int(cnt)} · —"
        base = f"{int(cnt)} · {rate * 100:.0f}%"
        if prior is None or pd.isna(prior):
            return base
        diff = (rate - prior) * 100
        if abs(diff) < 0.5:
            return base
        return f"{base} {'▲' if diff > 0 else '▼'}{abs(diff):.0f}"

    def _bs_color(v):
        s = str(v)
        if "▲" in s:
            return "color:#15803d; font-weight:600"
        if "▼" in s:
            return "color:#dc2626; font-weight:600"
        return ""

    def _bs_style(df):
        subset = [c for c in ("Booked", "Showed") if c in df.columns]
        return df.style.map(_bs_color, subset=subset) if subset else df

    def _ctab(d, cols):
        bk = [_bs_cell(b, br, _prbr.get(k))
              for b, br, k in zip(d["booked"], d["booking_rate"], d["_k"])]
        sh = [_bs_cell(s, sr, _prsr.get(k))
              for s, sr, k in zip(d["showed"], d["show_rate"], d["_k"])]
        F = {
            "Campaign": d["campaign"].values, "Account": d["account"].values,
            "Spend": d["spend_aud"].map(_money).values,
            "GHL Leads": d["leads"].astype(int).values,
            "Meta Leads": d["meta_leads"].astype(int).values,
            "CPL": d["cpl"].map(_money).values,
            "Booked": bk, "Showed": sh,
            "Booking Rate": d["booking_rate"].map(_pct).values,
            "Show Rate": d["show_rate"].map(_pct).values,
            "Cost per Appt": d["cpa"].map(_money).values,
            "Conversions": d["conv"].astype(int).values,
            "Bookings": d["booked"].astype(int).values,
            "Impressions": d["impressions"].astype(int).values,
            "Link Clicks": d["link_clicks"].astype(int).values,
            "CTR (link)": d["link_ctr"].map(_pctn).values,
            "Reach": d["reach"].astype(int).values,
            "Page Engagement": d["page_engagement"].astype(int).values,
            "CPM": d["cpm"].map(_money2).values,
            "Cost/Link Click": d["cplc"].map(_money2).values,
            "Messaging Started": d["messaging_started"].astype(int).values,
        }
        # the first column lists campaigns + "(no campaign)" Facebook leads +
        # a "Queries" row, so it is labelled "Source" rather than "Campaign".
        return pd.DataFrame({c: F[c] for c in cols}).rename(columns={"Campaign": "Source"})

    def _emails(df):
        dd = df.copy()
        appt = dd.apply(lambda r: "Showed" if r["appt_showed"] == 1
                        else ("Booked" if r["appt_booked"] == 1 else "—"), axis=1)
        cal = dd.apply(lambda r: r["calendar_name"]
                       if (r["appt_booked"] == 1 and pd.notna(r["calendar_name"])) else "—", axis=1)
        return pd.DataFrame({
            "Email": dd["email"].fillna("(no email)").values,
            "Lead Created Date": pd.to_datetime(dd["lead_date"]).dt.strftime("%Y-%m-%d").values,
            "Appt Created Date": dd["appt_booked_date"].map(
                lambda v: pd.to_datetime(v).strftime("%Y-%m-%d") if pd.notna(v) else "—").values,
            "Appointment Status": appt.values,
            "Calendar Name": cal.values,
            "Pipeline": dd["pipeline"].fillna("—").values,
            "Stage": dd["stage"].fillna("—").values,
            "Name": dd["contact_name"].fillna("—").replace("", "—").values,
            "Phone": dd["phone"].fillna("—").replace("", "—").values,
        })

    def _by_city(d, citysel):
        return d if citysel == "All" else d[d["account"] == citysel]

    def _grand_total(tA, d):
        """Append a TOTAL row (rates recomputed from the summed values)."""
        sp = float(d["spend_aud"].sum()); ld = int(d["leads"].sum()); bk = int(d["booked"].sum())
        sh = int(d["showed"].sum()); im = int(d["impressions"].sum()); lc = int(d["link_clicks"].sum())
        rc = int(d["reach"].sum()); pe = int(d["page_engagement"].sum()); cv = int(d["conv"].sum())
        ml = int(d["meta_leads"].sum()); mg = int(d["messaging_started"].sum())
        V = {"Source": "TOTAL", "Account": "—", "Spend": _money(sp), "GHL Leads": ld,
             "Meta Leads": ml, "CPL": _money(sp / ld) if ld else "—",
             "Booked": f"{bk} · {bk / ld * 100:.0f}%" if ld else f"{bk} · —",
             "Showed": f"{sh} · {sh / bk * 100:.0f}%" if bk else f"{sh} · —",
             "Booking Rate": _pct(bk / ld) if ld else "—", "Show Rate": _pct(sh / bk) if bk else "—",
             "Cost per Appt": _money(sp / bk) if bk else "—", "Conversions": cv, "Bookings": bk,
             "Impressions": im, "Link Clicks": lc,
             "CTR (link)": _pctn(lc / im * 100) if im else "—", "Reach": rc, "Page Engagement": pe,
             "CPM": _money2(sp / im * 1000) if im else "—",
             "Cost/Link Click": _money2(sp / lc) if lc else "—", "Messaging Started": mg}
        row = {c: V.get(c, "") for c in tA.columns}
        return pd.concat([tA, pd.DataFrame([row])], ignore_index=True)

    def _dl1(df, suffix):
        st.download_button("Download (CSV)", df.to_csv(index=False).encode("utf-8"),
                           file_name=f"meta1_{suffix}_{since.isoformat()}_{until.isoformat()}.csv",
                           mime="text/csv", key=f"meta1dl_{suffix}")

    @st.dialog(" ", width="large")
    def _m1_modal():
        card = st.session_state.get("meta1_card", "Spend")
        st.markdown(f"### {card} — Drill Down")
        if "meta1_city" not in st.session_state:
            st.session_state["meta1_city"] = "All"
        if "meta1_status" not in st.session_state:
            st.session_state["meta1_status"] = "All"
        cc1, cc2 = st.columns([1, 1.2])
        with cc1:
            citysel = st.segmented_control("View by account", ["All", "Melbourne", "Sydney"],
                                           key="meta1_city") or "All"
        with cc2:
            statussel = st.segmented_control(
                "Campaign status", ["All", "Active", "Inactive", "Other"],
                key="meta1_status") or "All"
        cv2 = _by_city(camp, citysel)
        # The "Queries" row (social-DM inquiries) belongs only to the Messaging
        # Conversations Started drill — not the Leads / other source tables.
        if card == "Messaging Conversations Started" and not qrow_df.empty:
            cv2 = pd.concat([cv2, qrow_df], ignore_index=True)
        if statussel != "All":
            cv2 = cv2[cv2["status"] == statussel]
        sfx = card.replace(" ", "_").replace("(", "").replace(")", "")

        EXPLICIT = {
            "Spend": ("spend_aud", ["Campaign", "Account", "Spend", "GHL Leads", "CPL",
                                    "Booked", "Showed", "Cost per Appt", "Conversions"]),
            "Cost per Appointment": ("cpa", ["Campaign", "Account", "Spend", "Booked",
                                             "Showed", "Cost per Appt"]),
            "Impressions": ("impressions", ["Campaign", "Impressions", "Link Clicks", "CTR (link)",
                            "Reach", "Page Engagement", "Meta Leads", "Booked", "Showed", "Conversions"]),
            "Link Clicks": ("link_clicks", ["Campaign", "Link Clicks", "CTR (link)", "Reach",
                            "Page Engagement", "Meta Leads", "Booked", "Showed", "Conversions"]),
            "CPM": ("cpm", ["Campaign", "CPM", "Impressions", "Link Clicks", "CTR (link)", "Reach",
                    "Page Engagement", "Meta Leads", "Booked", "Showed", "Conversions"]),
            "CPL": ("cpl", ["Campaign", "CPL", "CPM", "Impressions", "Link Clicks", "CTR (link)", "Reach",
                    "Page Engagement", "Meta Leads", "Booked", "Showed", "Conversions"]),
            "Leads": ("leads", ["Campaign", "Account", "GHL Leads", "Meta Leads", "Spend", "CPL",
                                "Impressions", "Link Clicks", "CTR (link)", "CPM", "Reach",
                                "Page Engagement", "Booked", "Showed", "Conversions"]),
            "CTR (link)": ("link_ctr", ["Campaign", "CTR (link)", "Link Clicks", "Impressions",
                                        "Reach", "Page Engagement", "Meta Leads"]),
            "Cost per Link Click": ("cplc", ["Campaign", "Cost/Link Click", "Link Clicks", "Spend",
                                             "Impressions", "CTR (link)"]),
            "Messaging Conversations Started": ("messaging_started",
                ["Campaign", "Messaging Started", "Spend", "Impressions", "Link Clicks"]),
            "Conversions": ("conv", ["Campaign", "Conversions", "GHL Leads", "Booked",
                                     "Showed", "Spend", "Cost per Appt"]),
        }

        if card in ("Booking Rate", "Show Rate"):
            sort_col, cols, asc = "spend_aud", [
                "Campaign", "CPM", "Impressions", "Link Clicks", "CTR (link)", "Reach",
                "Page Engagement", "Meta Leads", "Booked", "Showed", "Conversions"], False
        else:
            sort_col, cols = EXPLICIT.get(
                card, ("spend_aud", ["Campaign", "Account", "Spend", "GHL Leads", "CPL",
                                     "Booked", "Showed", "Conversions"]))
            asc = card in ("CPL", "CPM", "Cost per Appointment", "Cost per Link Click")
        d = cv2.sort_values(sort_col, ascending=asc, na_position="last")
        tA = _grand_total(_ctab(d, cols), cv2)
        st.markdown("**By source — click a row to see its leads**")
        selr = st.dataframe(_bs_style(tA), hide_index=True, use_container_width=True, height=420,
                            on_select="rerun", selection_mode="single-row", key="meta1_src_sel")
        _dl1(tA, sfx)
        pick = None
        try:
            rws = (selr.selection.get("rows") if selr else None) or []
            if rws:
                pick = tA.iloc[int(rws[0])]["Source"]
        except Exception:
            pick = None
        if pick and pick != "TOTAL":
            if pick == "Queries":
                dsub = socq
            else:
                dsub = _by_city(ps, citysel)
                dsub = dsub[dsub["_k"] == _ckey(pick)]
            st.markdown(f"**{pick} — {len(dsub):,} leads**")
            em = _emails(dsub)
            st.dataframe(em, hide_index=True, use_container_width=True, height=380)
            _dl1(em, f"{sfx}_emails")

    # ---- scorecards (click -> modal) ----
    def _sc(col, name, value, sub, delta):
        lines = [name.upper(), value] + [x for x in (sub, delta) if x]
        with col:
            if st.button("\n\n".join(lines), key=f"meta1sc_{name}", use_container_width=True):
                st.session_state["meta1_card"] = name
                _m1_modal()

    r1 = st.columns(5)
    _sc(r1[0], "Spend", _money(C["spend"]), "Meta · AUD",
        _delta_md(C["spend"], P["spend"], higher_is_better=True, fmt="pct"))
    _sc(r1[1], "Leads", f"{C['leads']:,}", "Meta-attributed · created+revived",
        _delta_md(C["leads"], P["leads"], higher_is_better=True, fmt="pct"))
    _sc(r1[2], "CPL", _money(cpl) if cpl else "—", "spend ÷ leads",
        _delta_md(cpl, p_cpl, higher_is_better=False, fmt="pct"))
    _sc(r1[3], "Link Clicks", f"{C['lclk']:,}", "inline link clicks",
        _delta_md(C["lclk"], P["lclk"], higher_is_better=True, fmt="pct"))
    _sc(r1[4], "Cost per Link Click", _money2(cplc) if cplc else "—", "spend ÷ link clicks",
        _delta_md(cplc, p_cplc, higher_is_better=False, fmt="pct"))

    r2 = st.columns(5)
    _sc(r2[0], "Messaging Conversations Started", f"{C['msg']:,}", "Meta",
        _delta_md(C["msg"], P["msg"], higher_is_better=True, fmt="pct"))
    _sc(r2[1], "Booking Rate", f"{br*100:.0f}%", f"{C['booked']:,} of {C['leads']:,}",
        _delta_md(br, p_br, higher_is_better=True, fmt="pts"))
    _sc(r2[2], "Show Rate", f"{shr*100:.0f}%", f"{C['showed']:,} of {C['booked']:,}",
        _delta_md(shr, p_shr, higher_is_better=True, fmt="pts"))
    _sc(r2[3], "Cost per Appointment", _money(cpa) if cpa else "—", f"÷ {C['booked']:,} appts",
        _delta_md(cpa, p_cpa, higher_is_better=False, fmt="pct"))
    _sc(r2[4], "Conversions", f"{C['conv']:,}", "COE / Initial / Won",
        _delta_md(C["conv"], P["conv"], higher_is_better=True, fmt="pct"))

    st.caption("Live Meta Ads metrics (Spend USD→AUD) joined to **Meta-attributed** GHL leads "
               "(created+revived: Paid-Social classified **or** utm_campaign matching a live Meta "
               "campaign). Impressions / CTR(link) / CPM / Reach / Page Engagement are inside the "
               "**Leads** drill-down. Click any scorecard for the per-campaign view with an "
               "**All / Melbourne / Sydney** selector.")

    # ===== Campaign performance — kill / scale / optimize (through Show Rate) =====
    st.markdown("---")
    st.markdown("### 📊 Campaign performance — kill / scale / optimize")
    pf1, pf2 = st.columns([1.2, 2])
    with pf1:
        if "meta1_perf_status" not in st.session_state:
            st.session_state["meta1_perf_status"] = "Active"
        perf_status = st.segmented_control(
            "Campaign status", ["All", "Active", "Inactive", "Other"],
            key="meta1_perf_status") or "All"
    with st.expander("⚙️ Targets (drive the Status column)"):
        tg = st.columns(4)
        tgt_cpl = tg[0].number_input("Target CPL ($)", value=30.0, min_value=0.0, step=5.0, key="m1_tcpl")
        tgt_br = tg[1].number_input("Target Booking Rate (%)", value=20.0, min_value=0.0,
                                    max_value=100.0, step=1.0, key="m1_tbr") / 100.0
        tgt_shr = tg[2].number_input("Target Show Rate (%)", value=50.0, min_value=0.0,
                                     max_value=100.0, step=1.0, key="m1_tshr") / 100.0
        tgt_cpa = tg[3].number_input("Target Cost / Appt ($)", value=200.0, min_value=0.0,
                                     step=10.0, key="m1_tcpa")
    MIN_SPEND = 50.0

    # avg days to book (lead created -> appointment booked) per campaign — how
    # long pre-sales takes to convince the lead to book.
    _dtb = ps[ps["appt_booked"] == 1].copy()
    if not _dtb.empty:
        _dtb["d2b"] = (pd.to_datetime(_dtb["appt_booked_date"])
                       - pd.to_datetime(_dtb["lead_date"])).dt.days.clip(lower=0)
        _d2b_map = _dtb.groupby("_k")["d2b"].mean().to_dict()
    else:
        _d2b_map = {}

    life = _meta1_lifetime_perf(until.isoformat(), fx)

    def _classify(spend, booked, cpl, cpa, br, shr):
        if spend >= MIN_SPEND and booked == 0:
            return "Kill"
        if (cpa is not None and pd.notna(cpa) and cpa > tgt_cpa) \
                or (br is not None and br < tgt_br) \
                or (shr is not None and shr < tgt_shr):
            return "Optimize"
        if (cpl is not None and pd.notna(cpl) and cpl <= tgt_cpl) \
                and (br is None or br >= tgt_br) and (shr is None or shr >= tgt_shr):
            return "Scale" if cpl < tgt_cpl * 0.75 else "Keep"
        return "Keep"

    def _perf_status(r):
        if r["campaign"] in ("Queries", "(no campaign)"):
            return "—"
        if r["status"] == "Active":
            return _classify(
                r["spend_aud"], r["booked"], r["cpl"], r["cpa"],
                r["booking_rate"] if pd.notna(r["booking_rate"]) else None,
                r["show_rate"] if pd.notna(r["show_rate"]) else None)
        # Inactive -> score on lifetime (previous) performance.
        lm = life.get(r["_k"])
        if not lm or lm["leads"] == 0:
            return "No history"
        return _classify(lm["spend_aud"], lm["booked"], lm["cpl"], lm["cpa"],
                         lm["booking_rate"], lm["show_rate"])

    perf = camp.copy()
    if perf_status != "All":
        perf = perf[perf["status"] == perf_status]
    perf = perf.sort_values("spend_aud", ascending=False)
    perf["d2b"] = perf["_k"].map(_d2b_map)
    perf["Status"] = perf.apply(_perf_status, axis=1)
    perf_disp = pd.DataFrame({
        "Campaign": perf["campaign"].values,
        "Account": perf["account"].values,
        "Spend": perf["spend_aud"].map(_money).values,
        "Leads": perf["leads"].astype(int).values,
        "CPL": perf["cpl"].map(_money).values,
        "Booked": perf["booked"].astype(int).values,
        "Booking Rate": perf["booking_rate"].map(_pct).values,
        "Showed": perf["showed"].astype(int).values,
        "Show Rate": perf["show_rate"].map(_pct).values,
        "Cost/Appt": perf["cpa"].map(_money).values,
        "Avg Days to Book": perf["d2b"].map(lambda v: f"{v:.0f}d" if pd.notna(v) else "—").values,
        "Status": perf["Status"].values,
    })
    _SS = {
        "Scale":    "background-color:#d1fae5; color:#065f46; font-weight:700; border-radius:999px;",
        "Keep":     "background-color:#dbeafe; color:#1e40af; font-weight:700; border-radius:999px;",
        "Optimize": "background-color:#fef3c7; color:#92400e; font-weight:700; border-radius:999px;",
        "Kill":     "background-color:#fee2e2; color:#991b1b; font-weight:700; border-radius:999px;",
        "No history": "background-color:#f3f4f6; color:#6b7280; border-radius:999px;",
    }
    _styler = perf_disp.style.map(lambda v: _SS.get(v, ""), subset=["Status"])
    st.dataframe(_styler, hide_index=True, use_container_width=True,
                 height=min(560, 60 + 36 * len(perf_disp)))
    st.caption(
        f"**Status logic**: **Kill** = spend ≥ ${MIN_SPEND:.0f} with 0 bookings · "
        f"**Optimize** = Cost/Appt > ${tgt_cpa:.0f} *or* booking rate < {tgt_br*100:.0f}% *or* show rate "
        f"< {tgt_shr*100:.0f}% · **Scale** = CPL < ${tgt_cpl*0.75:.0f} with rates on target · **Keep** = on "
        "target. **Inactive** campaigns are scored on their **lifetime** (previous) performance so you can "
        "see which ones worked. Conversions excluded (they take months) — judged through **Show Rate**. "
        "**Avg Days to Book** = mean lead-created → appointment-booked.")

    # ===== Analysis — lead quality vs quantity (vs last period) =====
    st.markdown("---")
    st.markdown("### 💡 Analysis — lead quality vs quantity")

    # Analysis is computed over ACTIVE campaigns only (those delivering in the
    # selected period). Inactive / no-campaign / query leads are residual and
    # excluded so the rates reflect what actually ran.
    _akp = set(m_pri["_k"]) if not m_pri.empty else set()
    ps_an = ps[ps["_k"].isin(active_keys_cur)] if active_keys_cur else ps.iloc[0:0]
    ps_anp = psp[psp["_k"].isin(_akp)] if _akp else psp.iloc[0:0]
    C, P = _agg(m_cur, ps_an), _agg(m_pri, ps_anp)
    cpl, p_cpl = _div(C["spend"], C["leads"]), _div(P["spend"], P["leads"])
    cpa, p_cpa = _div(C["spend"], C["booked"]), _div(P["spend"], P["booked"])
    br, p_br = _rate(C["booked"], C["leads"]), _rate(P["booked"], P["leads"])
    shr, p_shr = _rate(C["showed"], C["booked"]), _rate(P["showed"], P["booked"])
    st.caption(f"Over **active campaigns** only ({len(ps_an):,} of {len(ps):,} leads — "
               "inactive / no-campaign / query leads excluded).")

    def _pc(cur, pri):
        return ((cur - pri) / pri * 100) if pri else None

    dl_leads = _pc(C["leads"], P["leads"])
    dl_book  = _pc(C["booked"], P["booked"])
    dl_show  = _pc(C["showed"], P["showed"])
    dl_conv  = _pc(C["conv"], P["conv"])
    dl_spend = _pc(C["spend"], P["spend"])
    dl_cpl   = _pc(cpl, p_cpl) if (cpl and p_cpl) else None
    dl_cpa   = _pc(cpa, p_cpa) if (cpa and p_cpa) else None
    d_br = (br - p_br) * 100
    d_shr = (shr - p_shr) * 100
    ins = []   # (level, text) — good / warn / info

    # ---- headline: quality vs quantity ----
    if dl_leads is not None and dl_book is not None:
        leads_dn = dl_leads < -5
        leads_up = dl_leads > 5
        book_up = dl_book > 5
        book_dn = dl_book < -5
        cpa_dn = (dl_cpa is not None and dl_cpa < -5)
        if leads_dn and (book_up or dl_book > -5) and (cpa_dn or d_br > 1):
            ins.append(("good",
                f"**Lead quality vs quantity.** Leads fell **{abs(dl_leads):.0f}%** "
                f"({P['leads']}→{C['leads']}) yet bookings "
                f"{'rose **%.0f%%**' % dl_book if book_up else 'held'} and booking rate moved "
                f"**{d_br:+.0f} pts** to **{br*100:.0f}%**"
                + (f", CPA dropped **{abs(dl_cpa):.0f}%** to **${cpa:,.0f}**" if cpa_dn else "")
                + ". That's the classic signature of **fewer but higher-intent leads** (or a better "
                "booking process) — quality looks up at the top; any problem is **downstream**."))
        elif leads_up and (book_dn or d_br < -1):
            ins.append(("warn",
                f"**Volume up, intent down.** Leads rose **{dl_leads:.0f}%** but booking rate fell "
                f"**{abs(d_br):.0f} pts** to **{br*100:.0f}%** — more leads of **lower intent / quality**. "
                "Tighten targeting / creative rather than chasing more volume."))
        elif leads_dn and book_dn:
            ins.append(("warn",
                f"**Volume problem.** Leads down **{abs(dl_leads):.0f}%** and bookings down "
                f"**{abs(dl_book):.0f}%** with booking rate roughly flat ({br*100:.0f}%). The constraint is "
                "**lead supply**, not conversion — scale spend / reach on the winning campaigns."))
        else:
            ins.append(("info",
                f"Leads {('+' if (dl_leads or 0) >= 0 else '')}{dl_leads:.0f}%, bookings "
                f"{('+' if (dl_book or 0) >= 0 else '')}{dl_book:.0f}%, booking rate {br*100:.0f}% "
                f"({d_br:+.0f} pts). Mix is broadly stable vs last period."))

    # ---- downstream: where is the leak? ----
    if d_br > 1 and d_shr < -1:
        ins.append(("warn",
            f"**Downstream leak — shows.** Booking rate is up {d_br:.0f} pts but **show rate fell "
            f"{abs(d_shr):.0f} pts** to {shr*100:.0f}% — the drop-off is **booking→show** (no-shows). "
            "Reminder-SMS / confirmation cadence is the lever."))
    elif d_shr > 1 and dl_conv is not None and dl_conv < -5:
        ins.append(("warn",
            f"**Downstream leak — conversions.** Show rate up {d_shr:.0f} pts but conversions down "
            f"{abs(dl_conv):.0f}% — the leak is **post-consultation** (offer / follow-up), not the ad."))
    elif d_shr < -1:
        ins.append(("warn",
            f"**Show rate slipped {abs(d_shr):.0f} pts** to {shr*100:.0f}% — watch booking→show."))
    elif d_shr > 1 and d_br > 1:
        ins.append(("good",
            f"Funnel strengthening end-to-end: booking rate {d_br:+.0f} pts and show rate {d_shr:+.0f} pts."))

    # ---- cost efficiency ----
    # Show the working: spend ÷ bookings and the two date windows being compared.
    def _d(_x): return f"{_x.strftime('%b')} {_x.day}"
    _dr = f"{_d(since)}–{_d(until)} vs {_d(prior_since)}–{_d(prior_until)}"
    _cpa_calc = (f"${C['spend']:,.0f} ÷ {int(C['booked'])} "
                 f"booking{'s' if C['booked'] != 1 else ''}")
    if dl_cpa is not None and dl_cpa < -5:
        ins.append(("good", f"**Cheaper appointments.** CPA fell {abs(dl_cpa):.0f}% to **${cpa:,.0f}** "
                            f"({_cpa_calc}, {_dr}) — each booking costs less even if leads are down."))
    elif dl_cpa is not None and dl_cpa > 5:
        ins.append(("warn", f"**Pricier appointments.** CPA rose {dl_cpa:.0f}% to **${cpa:,.0f}** "
                            f"({_cpa_calc}, {_dr})."))
    if dl_spend is not None and dl_leads is not None and dl_spend > 5 and dl_leads < -5:
        ins.append(("warn", f"Spend up {dl_spend:.0f}% while leads fell {abs(dl_leads):.0f}% — "
                            f"CPL rose to **${cpl:,.0f}**. Check for fatigue / rising auction costs."))

    # ---- per-campaign callouts ----
    cflo = camp[camp["booked"] >= 2].copy()
    if not cflo.empty:
        best = cflo.sort_values("cpa", na_position="last").iloc[0]
        if pd.notna(best["cpa"]):
            ins.append(("info",
                f"Best campaign by cost: **{str(best['campaign'])[:42]}** — {int(best['booked'])} bookings "
                f"at **${best['cpa']:,.0f}** CPA ({(best['booking_rate'] or 0)*100:.0f}% booking rate)."))
    waste = camp[(camp["spend_aud"] >= 50) & (camp["booked"] == 0)]
    if not waste.empty:
        ins.append(("warn",
            f"**{len(waste)} campaign(s) spent ≥$50 with 0 bookings** "
            f"(${waste['spend_aud'].sum():,.0f} total) — review or pause."))

    _ICL = {"good": ("rgba(77,166,255,0.12)", "#4DA6FF"),
            "info": ("rgba(122,82,204,0.10)", "#7A52CC"),
            "warn": ("rgba(255,77,102,0.10)", "#FF4D66")}
    if not ins:
        st.caption("Not enough prior-period data to compare.")
    def _card(lvl, txt):
        bg, bcol = _ICL.get(lvl, _ICL["info"])
        html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", txt)
        st.markdown(
            f"<div style='background:{bg};border-left:4px solid {bcol};border-radius:8px;"
            f"padding:10px 14px;margin-bottom:8px;color:#1f2937;font-size:14px;'>{html}</div>",
            unsafe_allow_html=True)

    if not ins:
        st.caption("Not enough prior-period data to compare.")
    for lvl, txt in ins:
        _card(lvl, txt)

    # ===== Lead quality — pre-sales follow-up funnel =====
    # Opps enter New Lead -> Pre Sales (1) -> Pre Sales (2); ~5 calls per stage
    # (≈10). Leads that stall or are LOST in these stages = no response / not
    # interested = a quality / targeting signal for the performance marketer.
    st.markdown("### 🎯 Lead quality — pre-sales follow-up")

    def _is_early(stage):
        s = str(stage or "").strip().lower()
        return (s.startswith("new lead") or s.startswith("pre sales")
                or s.startswith("cold") or s in ("new facebook lead", "new meta lead"))

    def _q_bucket(r):
        pip, status = r["pipeline"], str(r["status"] or "").lower()
        early = _is_early(r["stage"])
        if r["appt_booked"] == 1:
            return "Booked / progressed"
        if not pip:
            return "No opportunity"
        if early and status in ("lost", "abandoned"):
            return "Lost in pre-sales"
        if early:
            return "Stuck in pre-sales (calling)"
        if status in ("lost", "abandoned"):
            return "Lost (later stage)"
        return "Progressed"

    if ps_an.empty:
        st.caption("No active-campaign leads in this window.")
    else:
        bk = ps_an.apply(_q_bucket, axis=1)
        vc = bk.value_counts()
        n = len(ps_an)
        with_opp = int((bk != "No opportunity").sum())
        lost_ps = int((bk == "Lost in pre-sales").sum())
        stuck_ps = int((bk == "Stuck in pre-sales (calling)").sum())
        progressed = int(bk.isin(["Booked / progressed", "Progressed"]).sum())
        ps_problem = lost_ps + stuck_ps
        ps_rate = (ps_problem / with_opp) if with_opp else 0
        q_rate = (progressed / with_opp) if with_opp else 0

        order = ["Booked / progressed", "Progressed", "Stuck in pre-sales (calling)",
                 "Lost in pre-sales", "Lost (later stage)", "No opportunity"]
        qt = pd.DataFrame([{
            "Stage outcome": b, "Leads": int(vc.get(b, 0)),
            "% of leads": f"{vc.get(b, 0)/n*100:.0f}%",
        } for b in order if vc.get(b, 0)])
        c1, c2 = st.columns([1, 1])
        with c1:
            st.dataframe(qt, hide_index=True, use_container_width=True, height=240)
        with c2:
            st.metric("Stall / lost in pre-sales", f"{ps_rate*100:.0f}%",
                      help="Lost or still being called in New Lead / Pre Sales (1) / Pre Sales (2), "
                           "as a share of leads with an opportunity.")
            st.metric("Progressed past follow-up", f"{q_rate*100:.0f}%")

        if with_opp:
            lvl = "warn" if ps_rate > 0.40 else "good"
            tail = ("High pre-sales loss alongside healthy lead volume is the classic **targeting / "
                    "lead-quality** signature — the ads deliver contacts that don't engage through "
                    "follow-up (no response after ~10 calls, or 'not interested')."
                    if ps_rate > 0.40 else
                    "Pre-sales conversion is healthy — most leads engage with follow-up.")
            _card(lvl,
                  f"**{ps_rate*100:.0f}% of active-campaign leads stall or are lost in pre-sales** — "
                  f"**{stuck_ps}** still being called (New Lead → Pre Sales 1 → Pre Sales 2) and "
                  f"**{lost_ps} lost / not interested**; only **{q_rate*100:.0f}%** progressed past "
                  f"follow-up. {tail}")

        # worst campaigns by pre-sales progression (min volume 3)
        psd = ps_an.copy()
        psd["bk"] = bk.values
        cq = (psd.groupby("campaign").agg(
                leads=("contact_id", "count"),
                prog=("bk", lambda s: s.isin(["Booked / progressed", "Progressed"]).sum()),
                lost=("bk", lambda s: (s == "Lost in pre-sales").sum())).reset_index())
        cq = cq[cq["leads"] >= 3].copy()
        if not cq.empty:
            cq["prog_rate"] = cq["prog"] / cq["leads"]
            w = cq.sort_values("prog_rate").iloc[0]
            if w["prog_rate"] < 0.40:
                _card("warn",
                      f"Worst campaign for follow-up: **{str(w['campaign'])[:42]}** — only "
                      f"**{w['prog_rate']*100:.0f}%** of its {int(w['leads'])} leads progress past "
                      f"pre-sales ({int(w['lost'])} lost). A lead-quality red flag for this campaign.")
            b = cq.sort_values("prog_rate", ascending=False).iloc[0]
            if b["prog_rate"] >= 0.55:
                _card("good",
                      f"Best campaign for follow-up: **{str(b['campaign'])[:42]}** — "
                      f"**{b['prog_rate']*100:.0f}%** of {int(b['leads'])} leads progress past pre-sales. "
                      "Higher-intent traffic — a candidate to scale.")
        st.caption("Early stages = New Lead · Pre Sales (1) · Pre Sales (2) · Cold/Nurturing "
                   "(≈5 calls per stage). 'Progressed' = booked an appointment or moved past pre-sales.")


with tab_meta1:
    render_meta1_tab()


# =====================================================================
# SEO & TRAFFIC TAB
# =====================================================================

with tab_seo:
    # Re-inject card CSS so buttons in this tab render as card-style scorecards
    # (same look as the Meta Ads tab — CSS is identical, kept local to ensure
    # it's applied regardless of which tab Streamlit renders first).
    st.markdown("""
<style>
section[data-testid="stMain"] [data-testid="stButton"] > button{
  background:#fff !important; color:#111 !important;
  border:1px solid #e6e8eb !important; border-radius:12px !important;
  padding:14px 16px !important; min-height:118px !important;
  text-align:left !important; align-items:flex-start !important;
  white-space:pre-line !important; line-height:1.45 !important;
  font-weight:400 !important; transition:all .15s !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button:hover{
  border-color:#93c5fd !important; transform:translateY(-1px);
  box-shadow:0 4px 10px rgba(37,99,235,.08) !important;
}
section[data-testid="stMain"] [data-testid="stButton"] > button[kind="primary"]{
  background:linear-gradient(135deg,#eaf3ff 0%,#dbeafe 100%) !important;
  border:2px solid #2563eb !important; color:#111 !important;
}
.acct-head-row{
  background:#fff; border:1px solid #e6e8eb; border-bottom:none;
  border-radius:14px 14px 0 0; padding:16px 18px 4px;
  display:flex; justify-content:space-between; align-items:center;
}
.acct-head-row .title{ font-size:17px; font-weight:700; color:#111; }
.acct-head-row .pill{
  background:#eef2ff; color:#3730a3; border-radius:999px;
  padding:4px 12px; font-size:12px; font-weight:700;
}
.static-card{
  background:#fff; border:1px solid #e6e8eb; border-radius:12px;
  padding:14px 16px; min-height:118px;
}
.static-card .lbl{ font-size:11px; color:#6b7280; font-weight:700;
  text-transform:uppercase; letter-spacing:.04em; }
.static-card .val{ font-size:22px; font-weight:700; color:#111; margin-top:4px; }
</style>
""", unsafe_allow_html=True)

    # ---- State ----
    SEO_KEYS = {
        "sessions": "Sessions", "engaged": "Engaged Sess.", "conv": "GA4 Conv.",
        "gsc_clicks": "GSC Clicks", "gsc_impr": "GSC Impressions",
        "pos": "Avg Position", "leads": "Website Leads", "bookings": "Bookings",
        "showed": "Showed", "book_rate": "Booking Rate",
    }
    SEO_REV = {v: k for k, v in SEO_KEYS.items()}
    qp = st.query_params.get("seo_metric")
    if qp:
        if qp in SEO_KEYS:
            st.session_state["seo_card"] = SEO_KEYS[qp]
        try:
            del st.query_params["seo_metric"]
        except Exception:
            pass
    if "seo_card" not in st.session_state:
        st.session_state["seo_card"] = "Sessions"
    seo_active = st.session_state["seo_card"]

    # ---- Data: current + prior ----
    ga4 = run_view("vw_ga4_tab_totals", binds)
    gsc = run_view("vw_gsc_tab_totals", binds)
    def _scur(d, k): return d.get("current", {}).get(k) if d else None
    def _spri(d, k): return d.get("prior",   {}).get(k) if d else None

    # Website Leads = Executive_1 'Organic Search' cohort (SAME classification as
    # the Executive_1 tab, via vw_exec1_lead_detail). Bookings = of those, how
    # many booked an appointment; Showed = of those, how many showed. Mel/Syd is
    # attributed via the contact's latest appointment office.
    _seo_cal_city = {cid: c["city"] for c in COUNSELLORS for cid in c["calendar_ids"]}

    def _seo_organic(s_from, s_to):
        d = run_df("vw_exec1_lead_detail", {"since": s_from, "until": s_to})
        if d.empty:
            return d
        return d[d["refined_source"] == "Organic Search"].copy()

    def _seo_attrib_city(df):
        if df.empty:
            return df.assign(city_group="Unassigned")
        ids = df["contact_id"].dropna().unique().tolist()
        cmap = {}
        if ids:
            la = get_con().execute(
                "SELECT contact_id, calendar_id FROM ("
                " SELECT contact_id, calendar_id, ROW_NUMBER() OVER "
                "   (PARTITION BY contact_id ORDER BY start_time DESC) rn "
                " FROM fact_appointments WHERE contact_id IN (SELECT UNNEST(?::VARCHAR[])) "
                "   AND LOWER(COALESCE(appointment_status,'')) <> 'invalid') WHERE rn = 1",
                [ids]).fetchdf()
            cmap = dict(zip(la["contact_id"], la["calendar_id"]))
        d = df.copy()
        d["city_group"] = d["contact_id"].map(lambda c: _seo_cal_city.get(cmap.get(c), "Unassigned"))
        return d

    def _seo_percity(df):
        cols = ["city_group", "website_leads", "bookings", "showed", "noshow"]
        if df.empty:
            return pd.DataFrame(columns=cols)
        df = df.copy()
        df["_noshow"] = ((df["appt_booked"] == 1) & (df["appt_showed"] == 0)).astype(int)
        g = df.groupby("city_group").agg(
            website_leads=("contact_id", "count"),
            bookings=("appt_booked", "sum"),
            showed=("appt_showed", "sum"),
            noshow=("_noshow", "sum")).reset_index()
        return g

    os_cur = _seo_attrib_city(_seo_organic(since.isoformat(), until.isoformat()))
    os_pri = _seo_attrib_city(_seo_organic(prior_since.isoformat(), prior_until.isoformat()))
    # "Form" / detailed source = the GHL 'Latest Source' custom field value
    # (e.g. "Sydney City Page"), kept in fact_contact_latest_source.
    _lsv_map = dict(get_con().execute(
        "SELECT contact_id, latest_source_value FROM fact_contact_latest_source").fetchall())
    wl_cur = _seo_percity(os_cur)
    wl_pri = _seo_percity(os_pri)
    sh_cur = wl_cur  # legacy alias — frame carries all three metrics
    sh_pri = wl_pri
    # Per-counsellor breakdown (drives Bookings/Showed modal dropdown)
    pc_cur = run_df("vw_seo_website_leads_per_counsellor",
                    {"since": since.isoformat(), "until": until.isoformat()})
    # One row per Organic-Search contact w/ survey + page + referrer + flags
    activity_cur = run_df("vw_seo_lead_activity_breakdown",
                          {"since": since.isoformat(), "until": until.isoformat()})
    # One row per Organic-Search Website Lead contact with the full enrichment
    # (pipeline, stage, owner, opp status, appointment status, latest source —
    # all computed live, including counsellor-name fallback). Drives the
    # unified Trend/Table view's table mode.
    seo_detail = run_df("vw_seo_website_leads_detail",
                        {"since": since.isoformat(), "until": until.isoformat()})
    # Per-submission rows for the Website Leads scorecard — shows each
    # form/survey submission separately with its page URL + form name.
    seo_activities = run_df("vw_seo_website_leads_activities",
                            {"since": since.isoformat(), "until": until.isoformat()})
    top_pages   = run_df("vw_seo_top_pages",
                         {"since": since.isoformat(), "until": until.isoformat()})
    top_queries = run_df("vw_seo_top_queries",
                         {"since": since.isoformat(), "until": until.isoformat()})
    top_pgsc    = run_df("vw_seo_top_pages_gsc",
                         {"since": since.isoformat(), "until": until.isoformat()})
    seo_trend   = run_df("vw_seo_daily_trend",
                         {"since": since.isoformat(), "until": until.isoformat()})
    ga4_per_city = run_df("vw_seo_ga4_per_city",
                          {"since": since.isoformat(), "until": until.isoformat()})

    # Filter wl_cur / wl_pri by global City filter (Mel / Syd / All).
    # GA4 / GSC traffic is site-wide; only the GHL-side metrics (Website
    # Leads, Bookings, Showed, Booking Rate) are city-specific.
    if city in ("Melbourne", "Sydney"):
        wl_cur_f = wl_cur[wl_cur["city_group"] == city] if not wl_cur.empty else wl_cur
        wl_pri_f = wl_pri[wl_pri["city_group"] == city] if not wl_pri.empty else wl_pri
        # Filter GA4-by-city for top scorecards too when city is picked
        gc_cur = ga4_per_city[ga4_per_city["city_group"] == city] \
                 if not ga4_per_city.empty else ga4_per_city
        c_sess = int(gc_cur["sessions"].sum())          if not gc_cur.empty else 0
        c_eng  = int(gc_cur["engaged_sessions"].sum())  if not gc_cur.empty else 0
        c_conv = int(gc_cur["key_events"].sum())        if not gc_cur.empty else 0
    else:
        wl_cur_f, wl_pri_f = wl_cur, wl_pri
        c_sess = _scur(ga4, "sessions") or 0
        c_eng  = _scur(ga4, "engaged_sessions") or 0
        c_conv = _scur(ga4, "key_events") or 0

    # GSC stays site-wide regardless of city pick (no per-city signal available)
    c_gclk = _scur(gsc, "clicks") or 0
    c_gimp = _scur(gsc, "impressions") or 0
    c_pos  = _scur(gsc, "avg_position")
    c_wl   = int(wl_cur_f["website_leads"].sum())  if not wl_cur_f.empty else 0
    c_book = int(wl_cur_f["bookings"].sum())       if not wl_cur_f.empty else 0
    c_show = int(wl_cur_f["showed"].sum())         if not wl_cur_f.empty else 0
    c_br   = (c_book / c_wl) if c_wl else None      # booking rate = booked / leads
    c_sr   = (c_show / c_wl) if c_wl else None      # show rate = showed / leads (per spec)

    p_sess = _spri(ga4, "sessions") or 0
    p_eng  = _spri(ga4, "engaged_sessions") or 0
    p_conv = _spri(ga4, "key_events") or 0
    p_gclk = _spri(gsc, "clicks") or 0
    p_gimp = _spri(gsc, "impressions") or 0
    p_pos  = _spri(gsc, "avg_position")
    p_wl   = int(wl_pri_f["website_leads"].sum())  if not wl_pri_f.empty else 0
    p_book = int(wl_pri_f["bookings"].sum())       if not wl_pri_f.empty else 0
    p_show = int(wl_pri_f["showed"].sum())         if not wl_pri_f.empty else 0
    p_br   = (p_book / p_wl) if p_wl else None
    p_sr   = (p_show / p_wl) if p_wl else None

    def _seo_delta(cur_v, pri_v, higher_is_better=True):
        try:
            if cur_v is None or pri_v is None or float(pri_v) == 0:
                return ""
            pct = (float(cur_v) - float(pri_v)) / float(pri_v) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            return ""
        if pct == 0:
            return ":gray[— vs last]"
        up = pct > 0
        is_good = up if higher_is_better else (not up)
        return f":{'green' if is_good else 'red'}[{'▲' if up else '▼'} {abs(pct):.0f}% vs last]"

    def _seo_pts_delta(cur_rate, pri_rate):
        """Δ in percentage-points between two rates (for Bookings / Showed)."""
        if cur_rate is None or pri_rate is None:
            return ""
        diff = (cur_rate - pri_rate) * 100
        if abs(diff) < 0.5:
            return ":gray[— vs last]"
        up = diff > 0
        return f":{'green' if up else 'red'}[{'▲' if up else '▼'} {abs(diff):.0f} pts vs last]"

    # Bookings / Showed carry the count AND the % in one card (booking rate =
    # booked/leads; show rate = showed/leads), with a ▲/▼ pts vs-last comparison.
    _book_val = f"{c_book:,}  ·  {c_br*100:.0f}%" if c_br is not None else f"{c_book:,}  ·  —"
    _show_val = f"{c_show:,}  ·  {c_sr*100:.0f}%" if c_sr is not None else f"{c_show:,}  ·  —"

    SEO_VAL = {
        "Sessions": fmt_int(c_sess), "Engaged Sess.": fmt_int(c_eng),
        "GA4 Conv.": fmt_int(c_conv), "GSC Clicks": fmt_int(c_gclk),
        "GSC Impressions": fmt_int(c_gimp),
        "Avg Position": fmt_position(c_pos) if c_pos else "—",
        "Website Leads": fmt_int(c_wl), "Bookings": _book_val,
        "Showed": _show_val,
        "Booking Rate": (f"{c_br*100:.1f}%" if c_br is not None else "—"),
    }
    SEO_DELTA = {
        "Sessions":        _seo_delta(c_sess, p_sess),
        "Engaged Sess.":   _seo_delta(c_eng,  p_eng),
        "GA4 Conv.":       _seo_delta(c_conv, p_conv),
        "GSC Clicks":      _seo_delta(c_gclk, p_gclk),
        "GSC Impressions": _seo_delta(c_gimp, p_gimp),
        "Avg Position":    _seo_delta(c_pos,  p_pos, higher_is_better=False),
        "Website Leads":   _seo_delta(c_wl,   p_wl),
        "Bookings":        _seo_pts_delta(c_br, p_br),
        "Showed":          _seo_pts_delta(c_sr, p_sr),
        "Booking Rate":    _seo_delta(c_br,   p_br),
    }
    # GA4 Conv. and Booking Rate are intentionally omitted from the top grid:
    # GA4 Conv. is excluded; the booking % now lives inside the Bookings card.
    SEO_METRICS = ["Sessions", "Engaged Sess.", "GSC Clicks", "GSC Impressions",
                   "Avg Position", "Website Leads", "Bookings", "Showed"]

    def _seo_int_fmt(x):
        return f"{int(x):,}" if str(x) not in ("", "nan") else ""

    def _seo_add_city_subtotals(df, group_col, city_col, numeric_cols):
        out = df.copy()
        rows = []
        for city in ("Melbourne", "Sydney"):
            sub = out[out[city_col] == city]
            if not sub.empty:
                r = {c: "" for c in out.columns}
                r[group_col] = f"{city.upper()} SUBTOTAL"
                r[city_col]  = city
                for nc in numeric_cols:
                    r[nc] = int(sub[nc].sum())
                rows.append(r)
        r = {c: "" for c in out.columns}
        r[group_col] = "GRAND TOTAL"
        for nc in numeric_cols:
            r[nc] = int(out[nc].sum())
        rows.append(r)
        return pd.concat([out, pd.DataFrame(rows)], ignore_index=True)

    # ---- Modal: opens on top-scorecard click ----
    @st.dialog(" ", width="large")
    def _seo_detail_modal():
        m = st.session_state.get("seo_card", "")
        st.markdown(f"### {m} — drill-down")

        # GHL-side (Website Leads / Bookings / Showed / Booking Rate) — new
        # cohort-based logic. Activity tables (survey / page / referrer) come
        # from vw_seo_lead_activity_breakdown; counsellor dropdown for
        # Bookings / Showed uses vw_seo_website_leads_per_counsellor.
        if m in ("Website Leads", "Bookings", "Showed", "Booking Rate"):
            if os_cur.empty:
                st.info("No Organic-Search website leads in this window.")
                return
            # City filter — Mel / Syd / Unassigned / All
            city_opts = ["All", "Melbourne", "Sydney", "Unassigned"]
            city_pick = st.segmented_control(
                "View by city", city_opts,
                default=st.session_state.get("seo_dialog_acct", "All"),
                key="seo_dialog_acct") or "All"
            base = os_cur if city_pick == "All" else os_cur[os_cur["city_group"] == city_pick]
            tl_leads = len(base)
            tl_book  = int(base["appt_booked"].sum()) if tl_leads else 0
            tl_show  = int(base["appt_showed"].sum()) if tl_leads else 0
            r1 = st.columns(3)
            r1[0].metric("Website Leads", f"{tl_leads:,}")
            r1[1].metric("Bookings", f"{tl_book:,}"
                         + (f"  ·  {tl_book / tl_leads * 100:.0f}%" if tl_leads else ""))
            r1[2].metric("Showed", f"{tl_show:,}"
                         + (f"  ·  {tl_show / tl_leads * 100:.0f}%" if tl_leads else ""))

            # rows for the active card: Website Leads = all; Bookings = booked;
            # Showed = showed.
            df = base
            if m == "Bookings":
                df = base[base["appt_booked"] == 1]
            elif m == "Showed":
                df = base[base["appt_showed"] == 1]
            if df.empty:
                st.info("No leads match this filter.")
                return
            _appt = df.apply(lambda r: "Showed" if r["appt_showed"] == 1
                             else ("Booked" if r["appt_booked"] == 1 else "—"), axis=1)
            _cal = df.apply(lambda r: r["calendar_name"]
                            if (r["appt_booked"] == 1 and pd.notna(r["calendar_name"])) else "—", axis=1)
            tbl = pd.DataFrame({
                "Email": df["email"].fillna("(no email)").values,
                "Form": [(fn if (fn and str(fn).strip() and str(fn) != "nan")
                          else (_lsv_map.get(cid) or "—"))
                         for fn, cid in zip(df["form_name"], df["contact_id"])],
                "City": df["city_group"].values,
                "Pipeline": df["pipeline"].fillna("—").values,
                "Stage": df["stage"].fillna("—").values,
                "Appointment": _appt.values,
                "Calendar": _cal.values,
                "Appt Created Date": df["appt_booked_date"].map(
                    lambda v: pd.to_datetime(v).strftime("%Y-%m-%d") if pd.notna(v) else "—").values,
                "Lead Created Date": pd.to_datetime(df["lead_date"]).dt.strftime("%Y-%m-%d").values,
                "Name": df["contact_name"].fillna("—").replace("", "—").values,
                "Phone": df["phone"].fillna("—").replace("", "—").values,
            })
            st.markdown(f"**{m} — {len(tbl):,} rows**")
            st.dataframe(tbl, hide_index=True, use_container_width=True, height=420)
            st.download_button(
                "Download (CSV)", tbl.to_csv(index=False).encode("utf-8"),
                file_name=f"seo_{m.lower().replace(' ', '_')}_{since.isoformat()}_{until.isoformat()}.csv",
                mime="text/csv", key="seo_os_dl")
            st.caption("Cohort = Executive_1 **Organic Search** leads (same classification "
                       "as the Executive_1 tab). Booking % = booked ÷ leads; Show % = "
                       "showed ÷ leads. City via the contact's latest appointment office.")

        # GA4 sessions / engaged / conv — show top landing pages
        elif m in ("Sessions", "Engaged Sess.", "GA4 Conv."):
            if top_pages.empty:
                st.info("No GA4 page data in this window.")
            else:
                tp = top_pages.head(30).copy()
                tp["page_views"]    = tp["page_views"].astype("int64").map(_seo_int_fmt)
                tp["active_users"]  = tp["active_users"].astype("int64").map(_seo_int_fmt)
                tp["form_fills"]    = tp["form_fills"].astype("int64").map(_seo_int_fmt)
                tp["form_contacts"] = tp["form_contacts"].astype("int64").map(_seo_int_fmt)
                tp["conv_rate"]     = tp["conv_rate"].map(
                    lambda v: f"{v*100:.2f}%" if pd.notna(v) else "—")
                tp.columns = ["Landing Page", "Page Views", "Active Users",
                              "Form Fills", "Form Contacts", "Conv. Rate"]
                st.markdown("**Top landing pages — sessions joined to form fills**")
                st.dataframe(tp, hide_index=True, use_container_width=True)
                st.caption("Conv. Rate = Form Fills ÷ Active Users on that page. "
                           "Page paths normalised (lowercased, trailing slash stripped).")

        # GSC clicks / impressions / position — show top queries + GSC pages
        elif m in ("GSC Clicks", "GSC Impressions", "Avg Position"):
            if not top_queries.empty:
                q = top_queries.head(30).copy()
                q["clicks"]      = q["clicks"].astype("int64").map(_seo_int_fmt)
                q["impressions"] = q["impressions"].astype("int64").map(_seo_int_fmt)
                q["avg_position"]= q["avg_position"].map(
                    lambda v: f"{v:.1f}" if pd.notna(v) else "—")
                q.columns = ["Search Query", "Clicks", "Impressions", "Avg Pos."]
                st.markdown("**Top search queries (GSC)**")
                st.dataframe(q, hide_index=True, use_container_width=True)

            if not top_pgsc.empty:
                pg = top_pgsc.head(30).copy()
                pg["clicks"]      = pg["clicks"].astype("int64").map(_seo_int_fmt)
                pg["impressions"] = pg["impressions"].astype("int64").map(_seo_int_fmt)
                pg["avg_position"]= pg["avg_position"].map(
                    lambda v: f"{v:.1f}" if pd.notna(v) else "—")
                pg.columns = ["Landing Page (GSC)", "Clicks", "Impressions", "Avg Pos."]
                st.markdown("**Top landing pages (Google Search)**")
                st.dataframe(pg, hide_index=True, use_container_width=True)

    # ---- 10 SEO scorecards — click-once highlights + updates trend chart;
    # click the already-active scorecard again to open its drill-down modal.
    def _seo_scorecard(col, label, value, scorecard_label, key_suffix, delta_text=""):
        is_active = (scorecard_label == seo_active)
        lines = [label, value]
        if delta_text:
            lines.append(delta_text)
        if col.button(
            "\n\n".join(lines),
            key=f"seo_{scorecard_label}_{key_suffix}",
            use_container_width=True,
            type=("primary" if is_active else "secondary"),
        ):
            if is_active:
                # Re-click of the active scorecard → open modal
                _seo_detail_modal()
            else:
                # First click → activate + update chart
                st.session_state["seo_card"] = scorecard_label
                st.rerun()

    _per_row = 4
    for _ri in range(0, len(SEO_METRICS), _per_row):
        chunk = SEO_METRICS[_ri:_ri + _per_row]
        cols = st.columns(_per_row)
        for j, m in enumerate(chunk):
            _seo_scorecard(cols[j], m.upper(), SEO_VAL[m], m,
                           f"sc{_ri // _per_row + 1}", SEO_DELTA.get(m, ""))

    # ---- Melbourne + Sydney city cards (Website-Form lead funnel + chart) ----
    SEO_TREND_COL = {"Sessions": "sessions", "Engaged Sess.": "engaged_sessions",
                     "GA4 Conv.": "ga4_conv", "GSC Clicks": "gsc_clicks",
                     "GSC Impressions": "gsc_impressions", "Avg Position": "gsc_position"}

    def _seo_city_card(col, city_label, chart_color):
        with col:
            # New attribution: Mel/Syd determined by the contact's latest
            # appointment's calendar → counsellor → office city. Contacts with
            # no appointment fall into the 'No counsellor' bucket and are NOT
            # shown on either card.
            wl_city = wl_cur[wl_cur["city_group"] == city_label] \
                if not wl_cur.empty else pd.DataFrame()
            ld  = int(wl_city["website_leads"].sum())  if not wl_city.empty else 0
            bk  = int(wl_city["bookings"].sum())       if not wl_city.empty else 0
            sh  = int(wl_city["showed"].sum())         if not wl_city.empty else 0
            ns  = int(wl_city["noshow"].sum())         if not wl_city.empty else 0
            br  = (bk / ld) if ld else None
            sr  = (sh / ld) if ld else None      # show rate = showed / leads (per spec)
            # GA4 traffic for this city
            gc = ga4_per_city[ga4_per_city["city_group"] == city_label] if not ga4_per_city.empty else pd.DataFrame()
            city_sessions = int(gc["sessions"].sum()) if not gc.empty else 0
            city_engaged  = int(gc["engaged_sessions"].sum()) if not gc.empty else 0
            city_conv     = int(gc["key_events"].sum()) if not gc.empty else 0

            st.markdown(
                f"<div class='acct-head-row'>"
                f"<div class='title'>{city_label} ({'Victoria' if city_label == 'Melbourne' else 'NSW'})</div>"
                f"<div class='pill'>{fmt_int(city_sessions)} sessions · {fmt_int(ld)} leads</div></div>",
                unsafe_allow_html=True
            )
            suf = f"city_{city_label.lower()}"
            r = st.columns(2)
            _seo_scorecard(r[0], "SESSIONS (GA4)", fmt_int(city_sessions), "Sessions",      suf)
            _seo_scorecard(r[1], "ENGAGED SESS.",  fmt_int(city_engaged),  "Engaged Sess.", suf)
            r2 = st.columns(3)
            _seo_scorecard(r2[0], "WEBSITE LEADS", fmt_int(ld), "Website Leads", suf)
            _seo_scorecard(r2[1], "BOOKINGS",      fmt_int(bk), "Bookings",      suf)
            _seo_scorecard(r2[2], "SHOWED",        fmt_int(sh), "Showed",        suf)
            r3 = st.columns(3)
            _seo_scorecard(r3[0], "BOOKING RATE",
                           f"{br*100:.1f}%" if br is not None else "—", "Booking Rate", suf)
            with r3[1]:
                st.markdown(
                    f"<div class='static-card'><div class='lbl'>SHOW RATE</div>"
                    f"<div class='val'>{sr*100:.1f}%</div></div>"
                    if sr is not None else
                    f"<div class='static-card'><div class='lbl'>SHOW RATE</div>"
                    f"<div class='val'>—</div></div>",
                    unsafe_allow_html=True
                )
            with r3[2]:
                st.markdown(
                    f"<div class='static-card'><div class='lbl'>NO SHOW</div>"
                    f"<div class='val'>{fmt_int(ns)}</div></div>",
                    unsafe_allow_html=True
                )

            col_name = SEO_TREND_COL.get(seo_active)
            import altair as alt
            chart_df = None
            chart_label = ""
            # 1) GA4 metrics → city-filtered daily trend
            if seo_active in ("Sessions", "Engaged Sess.", "GA4 Conv.") and not gc.empty:
                ga4_col = {"Sessions": "sessions",
                           "Engaged Sess.": "engaged_sessions",
                           "GA4 Conv.": "key_events"}[seo_active]
                chart_df = gc[["date", ga4_col]].rename(columns={ga4_col: "value"})
                chart_label = f"{seo_active} — daily trend ({city_label} only)"
            # 2) GSC + GA4-site metrics → site-wide daily trend from vw_seo_daily_trend
            elif col_name and not seo_trend.empty and col_name in seo_trend.columns:
                chart_df = seo_trend[["date", col_name]].rename(columns={col_name: "value"})
                chart_label = f"{seo_active} — daily trend (site-wide)"
            # 3) GHL metrics (Website Leads / Bookings / Showed / Booking Rate)
            #    have no daily fact — fall back to this city's Sessions trend
            #    so the chart always reflects something city-specific.
            elif not gc.empty:
                chart_df = gc[["date", "sessions"]].rename(columns={"sessions": "value"})
                chart_label = f"Sessions — daily trend ({city_label} only)  ·  '{seo_active}' has no daily breakdown"

            if chart_df is not None and not chart_df.empty:
                chart_df["date"] = pd.to_datetime(chart_df["date"])
                st.caption(chart_label)
                chart = (
                    alt.Chart(chart_df)
                    .mark_area(
                        interpolate="monotone", color=chart_color, opacity=0.22,
                        line={"color": chart_color, "strokeWidth": 2.5},
                    )
                    .encode(
                        x=alt.X("date:T", title=None,
                                axis=alt.Axis(format="%b %d", tickCount=6, labelFontSize=11,
                                              grid=False, domain=False, ticks=False)),
                        y=alt.Y("value:Q", title=None,
                                axis=alt.Axis(labelFontSize=11, grid=True,
                                              gridColor="#e5e7eb", domain=False, ticks=False)),
                    )
                    .properties(height=210)
                    .configure_view(strokeWidth=0)
                )
                st.altair_chart(chart, use_container_width=True)
            else:
                st.caption(f"{seo_active} trend not available for this view.")

    # ---- Unified Trend / Table view (global City filter drives selection) ----
    # Same pattern as Counsellors tab: removed the side-by-side Mel/Syd cards.
    # Top scorecards already show site-wide totals; this block lets the user
    # drill into the active metric by city + view mode.
    if city == "Melbourne":
        seo_filter_label = "Melbourne"
        seo_chart_color  = "#3b82f6"
    elif city == "Sydney":
        seo_filter_label = "Sydney"
        seo_chart_color  = "#10b981"
    else:
        seo_filter_label = "All"
        seo_chart_color  = "#6366f1"

    # Filter activity_cur + seo_detail by counsellor city
    if seo_filter_label != "All":
        seo_activity_filt = activity_cur[activity_cur["city_group"] == seo_filter_label] \
                            if not activity_cur.empty else activity_cur
        seo_detail_filt   = seo_detail[seo_detail["city_group"] == seo_filter_label] \
                            if not seo_detail.empty else seo_detail
    else:
        seo_activity_filt = activity_cur
        seo_detail_filt   = seo_detail

    # Organic-Search cohort (Executive_1 logic) for this city — drives the pill
    # and the Table view so they match the top scorecards.
    _os_filt = (os_cur if seo_filter_label == "All"
                else (os_cur[os_cur["city_group"] == seo_filter_label]
                      if not os_cur.empty else os_cur))

    # Header pill
    n_leads_filt = len(_os_filt)
    n_book_filt  = int(_os_filt["appt_booked"].sum()) if n_leads_filt else 0
    region_label = "All cities" if seo_filter_label == "All" else (
        f"{seo_filter_label} ({'Victoria' if seo_filter_label == 'Melbourne' else 'NSW'})")
    st.markdown(
        f"<div class='acct-head-row'>"
        f"<div class='title'>{region_label}</div>"
        f"<div class='pill'>{n_leads_filt:,} website lead"
        f"{'s' if n_leads_filt != 1 else ''} · {n_book_filt:,} booked</div></div>",
        unsafe_allow_html=True,
    )

    # Initialize state ONCE; `default=` + `key=` together can de-sync in Streamlit,
    # so we drive the segmented_control purely from session_state.
    if "seo_unified_view" not in st.session_state:
        st.session_state["seo_unified_view"] = "Trend"
    seo_view_mode = st.segmented_control(
        "View",
        ["Trend", "Table"],
        key="seo_unified_view",
        label_visibility="collapsed",
    )
    if not seo_view_mode:
        seo_view_mode = st.session_state.get("seo_unified_view", "Trend")

    if seo_view_mode == "Trend":
        import altair as alt
        # Build the trend series the same way the old card did, but for the
        # active city (or site-wide if All).
        if seo_filter_label != "All" and not ga4_per_city.empty:
            gc = ga4_per_city[ga4_per_city["city_group"] == seo_filter_label]
        else:
            gc = ga4_per_city  # all rows = site-wide (still aggregated by date below)

        col_name = SEO_TREND_COL.get(seo_active)
        chart_df = None
        chart_label = ""
        if seo_active in ("Sessions", "Engaged Sess.", "GA4 Conv.") and not gc.empty:
            ga4_col = {"Sessions": "sessions",
                       "Engaged Sess.": "engaged_sessions",
                       "GA4 Conv.": "key_events"}[seo_active]
            chart_df = (gc.groupby("date", as_index=False)[ga4_col]
                        .sum()
                        .rename(columns={ga4_col: "value"}))
            chart_label = f"{seo_active} — daily trend ({seo_filter_label})"
        elif col_name and not seo_trend.empty and col_name in seo_trend.columns:
            chart_df = seo_trend[["date", col_name]].rename(columns={col_name: "value"})
            chart_label = f"{seo_active} — daily trend (site-wide)"
        elif not gc.empty:
            chart_df = (gc.groupby("date", as_index=False)["sessions"]
                        .sum()
                        .rename(columns={"sessions": "value"}))
            chart_label = (f"Sessions — daily trend ({seo_filter_label}) · "
                           f"'{seo_active}' has no daily breakdown")

        if chart_df is not None and not chart_df.empty:
            chart_df["date"] = pd.to_datetime(chart_df["date"])
            st.caption(chart_label)
            chart = (
                alt.Chart(chart_df)
                .mark_area(
                    interpolate="monotone", color=seo_chart_color, opacity=0.22,
                    line={"color": seo_chart_color, "strokeWidth": 2.5},
                )
                .encode(
                    x=alt.X("date:T", title=None,
                            axis=alt.Axis(format="%b %d", tickCount=8,
                                          labelFontSize=11, grid=False,
                                          domain=False, ticks=False)),
                    y=alt.Y("value:Q", title=None,
                            axis=alt.Axis(labelFontSize=11, grid=True,
                                          gridColor="#e5e7eb", domain=False, ticks=False)),
                    tooltip=[alt.Tooltip("date:T", format="%Y-%m-%d (%a)"),
                             alt.Tooltip("value:Q", format=",.0f")],
                )
                .properties(height=260)
                .configure_view(strokeWidth=0)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.caption(f"{seo_active} trend not available for this view.")
    else:
        # ---- Table view — content depends on the active scorecard ----
        # GHL metrics (Website Leads / Bookings / Showed / Booking Rate) →
        #   per-lead detail from vw_seo_website_leads_detail
        # GA4 metrics (Sessions / Engaged Sess. / GA4 Conv.) →
        #   per-page detail from vw_seo_top_pages (page views, users, form fills)
        # GSC metrics (GSC Clicks / GSC Impressions / Avg Position) →
        #   per-page detail from vw_seo_top_pages_gsc + top queries
        if seo_active in ("Website Leads", "Bookings", "Showed", "Booking Rate"):
            if _os_filt.empty:
                st.info(f"No Organic-Search website leads in this window for {seo_filter_label}.")
            else:
                df = _os_filt.copy()
                # Filter rows by active metric (Website Leads / Booking Rate = all)
                if seo_active == "Bookings":
                    df = df[df["appt_booked"] == 1]
                elif seo_active == "Showed":
                    df = df[df["appt_showed"] == 1]
                if df.empty:
                    st.info(f"No leads match '{seo_active}' in {seo_filter_label}.")
                else:
                    _appt = df.apply(lambda r: "Showed" if r["appt_showed"] == 1
                                     else ("Booked" if r["appt_booked"] == 1 else "—"), axis=1)
                    out = pd.DataFrame({
                        "Email":    df["email"].fillna("(no email)").values,
                        "Source":   df["refined_source"].fillna("—").values,
                        "Form":     [(fn if (fn and str(fn).strip() and str(fn) != "nan")
                                      else (_lsv_map.get(cid) or "—"))
                                     for fn, cid in zip(df["form_name"], df["contact_id"])],
                        "Pipeline": df["pipeline"].fillna("—").values,
                        "Stage":    df["stage"].fillna("—").values,
                        "Status":   df["status"].fillna("—").values,
                        "Appointment": _appt.values,
                        "City":     df["city_group"].values,
                        "Lead Date": pd.to_datetime(df["lead_date"]).dt.strftime("%Y-%m-%d").values,
                    })
                    out = out.sort_values(["Lead Date", "Email"], ascending=[False, True])
                    st.dataframe(out, hide_index=True, use_container_width=True, height=420)
                    st.caption(
                        f"{len(out)} {seo_active.lower()} row{'s' if len(out) != 1 else ''} "
                        f"in {seo_filter_label}. Cohort = Executive_1 **Organic Search** "
                        "leads (same classification as the Executive_1 tab)."
                    )

        elif seo_active in ("Sessions", "Engaged Sess.", "GA4 Conv."):
            if top_pages.empty:
                st.info("No GA4 page data in this window.")
            else:
                tp = top_pages.head(60).copy()
                # Sort by the metric that matches the active scorecard
                sort_col = ("active_users" if seo_active == "Engaged Sess."
                            else "form_fills" if seo_active == "GA4 Conv."
                            else "page_views")
                tp = tp.sort_values(sort_col, ascending=False)
                tp["page_views"]    = tp["page_views"].astype("int64").map(_seo_int_fmt)
                tp["active_users"]  = tp["active_users"].astype("int64").map(_seo_int_fmt)
                tp["form_fills"]    = tp["form_fills"].astype("int64").map(_seo_int_fmt)
                tp["form_contacts"] = tp["form_contacts"].astype("int64").map(_seo_int_fmt)
                tp["conv_rate"]     = tp["conv_rate"].map(
                    lambda v: f"{v*100:.2f}%" if pd.notna(v) else "—")
                tp.columns = ["Landing Page", "Page Views", "Active Users",
                              "Form Fills", "Form Contacts", "Conv. Rate"]
                st.dataframe(tp, hide_index=True, use_container_width=True, height=420)
                st.caption(
                    f"Top {len(tp)} landing pages by {seo_active}. "
                    "Conv. Rate = Form Fills ÷ Active Users on that page. "
                    "GA4 page data is site-wide — not filtered by city."
                )

        elif seo_active in ("GSC Clicks", "GSC Impressions", "Avg Position"):
            # Show top queries + top GSC pages side-by-side (or stacked).
            if top_queries.empty and top_pgsc.empty:
                st.info("No GSC data in this window.")
            else:
                if not top_pgsc.empty:
                    pg = top_pgsc.head(40).copy()
                    sort_col = ("clicks" if seo_active == "GSC Clicks"
                                else "impressions" if seo_active == "GSC Impressions"
                                else "avg_position")
                    pg = pg.sort_values(sort_col,
                                        ascending=(seo_active == "Avg Position"))
                    pg["clicks"]      = pg["clicks"].astype("int64").map(_seo_int_fmt)
                    pg["impressions"] = pg["impressions"].astype("int64").map(_seo_int_fmt)
                    pg["avg_position"]= pg["avg_position"].map(
                        lambda v: f"{v:.1f}" if pd.notna(v) else "—")
                    pg.columns = ["Landing Page (GSC)", "Clicks", "Impressions", "Avg Pos."]
                    st.markdown("**Top landing pages (Google Search)**")
                    st.dataframe(pg, hide_index=True, use_container_width=True, height=320)
                if not top_queries.empty:
                    q = top_queries.head(40).copy()
                    sort_col = ("clicks" if seo_active == "GSC Clicks"
                                else "impressions" if seo_active == "GSC Impressions"
                                else "avg_position")
                    q = q.sort_values(sort_col,
                                      ascending=(seo_active == "Avg Position"))
                    q["clicks"]      = q["clicks"].astype("int64").map(_seo_int_fmt)
                    q["impressions"] = q["impressions"].astype("int64").map(_seo_int_fmt)
                    q["avg_position"]= q["avg_position"].map(
                        lambda v: f"{v:.1f}" if pd.notna(v) else "—")
                    q.columns = ["Search Query", "Clicks", "Impressions", "Avg Pos."]
                    st.markdown("**Top search queries**")
                    st.dataframe(q, hide_index=True, use_container_width=True, height=320)
                st.caption(
                    "GSC data is site-wide (no city dimension). "
                    "Avg Position is impressions-weighted; lower = better ranking."
                )

        else:
            st.info(f"No table view defined for '{seo_active}'.")

    # ---- Top Pages table (always-on; the SEO equivalent of Campaign Performance) ----
    # Leads-aligned: each Executive_1 Organic-Search lead is attributed to its
    # form's landing page (so Leads sum to the Website Leads scorecard), with a
    # Booking Rate (Bookings ÷ Leads). GA4 Page Views / Active Users joined for
    # traffic context (same normalised page path as vw_seo_top_pages).
    st.markdown("### Top landing pages — traffic × leads × booking rate")
    if os_cur.empty:
        st.info("No Organic-Search website leads in this window.")
    else:
        _ids = os_cur["contact_id"].dropna().unique().tolist()
        _pgmap = {}
        if _ids:
            _subs = get_con().execute(
                "SELECT contact_id, pp FROM ("
                " SELECT contact_id, LOWER(REGEXP_REPLACE(COALESCE(page_path,''),'/$','')) AS pp, "
                "        ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY submitted_at DESC) rn "
                " FROM fact_form_submissions "
                " WHERE contact_id IN (SELECT UNNEST(?::VARCHAR[])) AND COALESCE(page_path,'') <> '') "
                "WHERE rn = 1", [_ids]).fetchdf()
            _pgmap = dict(zip(_subs["contact_id"], _subs["pp"]))
        _co = os_cur.copy()
        _co["_lp"] = _co["contact_id"].map(lambda c: _pgmap.get(c) or "(no landing page)")
        _grp = (_co.groupby("_lp")
                .agg(Leads=("contact_id", "count"), Bookings=("appt_booked", "sum"))
                .reset_index().sort_values("Leads", ascending=False).head(25))
        _gv = dict(zip(top_pages["page_path"], top_pages["page_views"])) if not top_pages.empty else {}
        _gu = dict(zip(top_pages["page_path"], top_pages["active_users"])) if not top_pages.empty else {}
        out = pd.DataFrame({
            "Landing Page": _grp["_lp"].values,
            "Page Views":  [(_seo_int_fmt(_gv[p]) if p in _gv else "—") for p in _grp["_lp"]],
            "Active Users": [(_seo_int_fmt(_gu[p]) if p in _gu else "—") for p in _grp["_lp"]],
            "Leads":    _grp["Leads"].astype(int).values,
            "Bookings": _grp["Bookings"].astype(int).values,
            "Booking Rate": [(f"{b / l * 100:.0f}%" if l else "—")
                             for l, b in zip(_grp["Leads"], _grp["Bookings"])],
        })
        st.dataframe(out, hide_index=True, use_container_width=True)
        st.caption(
            f"**Leads** = Executive_1 **Organic Search** leads (total "
            f"{int(_grp['Leads'].sum()):,}) attributed to each landing page via the "
            "contact's latest form submission; **'(no landing page)'** = leads with no "
            "form page (e.g. survey/attribution-only). **Booking Rate** = Bookings ÷ "
            "Leads. **Page Views / Active Users** are GA4 traffic for that page (site-wide)."
        )

    # ---- Documentation modal ----
    @st.dialog("SEO & Traffic tab — How these numbers work", width="large")
    def _seo_docs_modal():
        st.markdown("""
### 📖 Tab overview
The **SEO & Traffic** tab joins GA4, GSC, GHL forms, GHL surveys, GHL appointments, GHL opportunities, and GHL users to track how organic / website visitors become leads, bookings, and customers.

### 🔌 Data sources
| Table | Source endpoint | What it powers |
|---|---|---|
| `fact_ga4_sessions` / `fact_ga4_pages` / `fact_ga4_events` | GA4 Reporting API | Sessions, Engaged Sess., GA4 Conv., per-page conversion |
| `fact_gsc_queries` | Google Search Console API | GSC Clicks, Impressions, Avg Position, top queries, top GSC pages |
| `fact_form_submissions` | GHL `/forms/submissions` | Website Lead cohort, form-name + page-url + referrer breakdown |
| `fact_survey_submissions` | GHL `/surveys/submissions` | Website Lead cohort (survey path), survey-name breakdown |
| `fact_appointments` | GHL `/calendars/events` | Bookings, Showed, Mel/Syd attribution via counsellor calendar |
| `fact_opportunities` + `dim_pipelines` + `dim_stages` | GHL `/opportunities/search` + `/pipelines` | Pipeline + Stage + Status columns in table view |
| `dim_users` | GHL `/users` | Owner column (assigned_user_id → human name) |
| `fact_contacts` | GHL `/contacts/` | Email, lead-date, attribution fallback |

### 🧮 How each metric is calculated
- **Sessions** = `SUM(sessions)` from `fact_ga4_sessions` in the window. When the global City filter is Mel/Syd, this restricts to that city's GA4 sessions; otherwise it's site-wide.
- **Engaged Sess.** = `SUM(sessions × (1 − bounce_rate))`.
- **GA4 Conv.** = `SUM(event_count)` for the canonical 4 events: `contact_us`, `generate_lead`, `book_consultation_page`, `blogs_to_consultation`.
- **GSC Clicks / Impressions / Avg Position** = `SUM(...)` from `fact_gsc_queries`. Avg Position is impressions-weighted. Always site-wide (GSC has no city dimension).
- **Website Lead cohort** = contacts whose **latest form OR survey submission** has `event_source = 'Organic Search'` AND whose `fact_contacts.date_added` falls in the window. **Lead date = contact created date**, never appointment date. (A May 7 contact who booked May 10 for a May 17 meeting belongs to May 7.)
- **Bookings** (calendar-based) = Website Lead cohort contacts who have at least one appointment in `fact_appointments`.
- **Showed** = subset of Bookings whose latest appointment has `canonical_outcome = 'show'`.
- **Booking Rate** = Bookings ÷ Website Leads.
- **Show Rate** = Showed ÷ Bookings.

### 🏙️ City filter (Mel/Syd attribution)
- Derived from the contact's **latest appointment's calendar** → counsellor → counsellor's office city.
  - **Melbourne**: Navneet Kaur, Gurbir Singh
  - **Sydney**: Turab, Nasir Nawaz, Kajal, Wajahad, Saurab
- Contacts with no appointment → bucket `Unassigned` (shown only when the global filter is "All").
- The **global City filter** at the top of the page (`All / Melbourne / Sydney`) drives the **entire tab**:
  - Top scorecards (Website Leads, Bookings, Showed, Booking Rate, plus GA4 Sessions/Engaged/Conv)
  - The unified Trend/Table view below the scorecards
  - GSC remains site-wide (no per-city data available).
- "Others" / "Unidentified" don't apply on this tab (they're contact-side city groups) and are treated as "All".

### 🖱️ Interactivity
- **Click an inactive scorecard** → highlights it + changes the unified Trend chart's metric.
- **Click the already-active scorecard again** → opens the drill-down modal with Survey Name / Page URL / Referrer breakdowns.
- **Trend / Table toggle** below the scorecards — table content depends on which scorecard is active:
  - **Website Leads** → all leads (`Email · Pipeline · Stage · Owner · Status · Appointment Status · City · Latest Source · Lead Date`).
  - **Bookings** → same columns, filtered to leads where `has_booking = 1`.
  - **Showed** → same columns, filtered to leads where `canonical_outcome = 'show'`.
  - **Booking Rate** → all leads (so you see both numerator + denominator).
  - **Sessions / Engaged Sess. / GA4 Conv.** → top landing pages from `vw_seo_top_pages` (`Landing Page · Page Views · Active Users · Form Fills · Form Contacts · Conv. Rate`), sorted by the active metric. Site-wide — GA4 page data isn't filtered by city.
  - **GSC Clicks / GSC Impressions / Avg Position** → top landing pages (GSC) + top search queries from `vw_seo_top_pages_gsc` and `vw_seo_top_queries`, sorted by the active metric (Avg Position sorted ascending — lower = better).
  - **Trend** view always shows the daily Altair area chart for the active metric in the active city.

### 🧬 Latest Source (Table view's "Latest Source" column)
Computed **live at query time** — not read from GHL's stored Latest Source custom field. Same 8-step precedence as the Counsellors tab:
1. `campaign -- utm_content`
2. `campaign` alone
3. `form_name` OR `survey_name`
4. `event_form_name`
5. `session_source`
6. `event_source`
7. **Counsellor name from the contact's latest appointment's calendar** (fallback for booking-only contacts)
8. `fact_contacts.latest_attribution_source`

### 🧾 Owner column logic (Table view)
- First tries the **latest opportunity's** `assigned_user_id` → `dim_users.full_name`.
- Falls back to the **contact's** `assigned_user_id` if no opportunity is assigned.
- Shows `—` if neither is set.

### 📊 Comparison deltas
Every scorecard shows `▲/▼ X% vs last` against the **prior equal-length window**. Avg Position is **inverted** (lower = better — downward arrow is green).

### ⚠️ Known caveats
- A Website Lead is attributed Mel/Syd only after they book an appointment. Until then they live in `Unassigned` (only visible when filter is "All").
- GSC has no city dimension — GSC metrics stay site-wide regardless of the global City filter pick.
""")

    st.markdown("---")
    st.caption(
        f"💡 **{seo_active}** is currently active (highlighted). Click it again to drill into "
        "Page URL / Referrer breakdowns."
    )
    if st.button("📘 Documentation — how these SEO numbers are calculated",
                 key="seo_docs_btn", use_container_width=True):
        _seo_docs_modal()

    st.caption(
        "Sources: GA4 `fact_ga4_sessions` + `fact_ga4_pages` + `fact_ga4_events` · "
        "GSC `fact_gsc_queries` · GHL `fact_form_submissions` + `fact_survey_submissions` "
        "+ `fact_appointments`. Mel/Syd attribution = counsellor of latest appointment. "
        "GA4 city signals are site-wide for the GA4 row."
    )


# =====================================================================
# FORECAST & GOALS TAB
# =====================================================================
# Period table (Month / Week / Day) of the funnel-economics metrics, with
# COE goal tracking. Metrics are computed from the lead cohort (who came in
# the period), appointments created in the period, and Meta spend.

with tab_fc:
    st.markdown("### Forecast & Goals")

    g1, g2, g3, _g4 = st.columns([2, 2, 2, 2])
    with g1:
        if "fc_grain" not in st.session_state:
            st.session_state["fc_grain"] = "Month"
        grain = st.segmented_control(
            "View", ["Month", "Week", "Day"], key="fc_grain") or "Month"
    with g2:
        fc_growth = st.number_input(
            "COE target growth % / period", min_value=0.0, value=16.0, step=1.0,
            key="fc_growth",
            help="COE Target = previous period's target × (1 + this %).")
    with g3:
        fc_coe_base = st.number_input(
            "COE target — first period", min_value=0, value=50, step=1,
            key="fc_coe_base",
            help="Seed for the COE Target series; each later period = previous × "
                 "(1 + growth %). Your sheet started at 50 → 58 → 67 → 78 …")

    GRAIN_CFG = {"Month": ("M", 12), "Week": ("W", 12), "Day": ("D", 30)}
    freq, n_periods = GRAIN_CFG[grain]
    fc_until = date.today()
    fc_since = fc_until - timedelta(days=430)
    fc_binds = {"since": fc_since.isoformat(), "until": fc_until.isoformat()}

    # Per-period metrics — each period re-runs the Executive_1 cohort for its own
    # window so Total Leads (and the rest) match the Executive_1 tab exactly,
    # including booked-in contacts. (Meta accounts bill in USD → AUD via fx.)
    fc_fx = usd_to_aud()
    m = forecast_metrics(grain, fc_until.isoformat(), n_periods, fc_fx)

    if m.empty:
        st.info("No lead data available for the forecast window.")
    else:
        # COE Target = first shown period uses the user's base; each later
        # period = previous target × (1 + growth %).
        g = fc_growth / 100.0
        targets = []
        for i, (_idx, _row) in enumerate(m.iterrows()):
            targets.append(float(fc_coe_base) if i == 0 else targets[-1] * (1 + g))
        m["coe_target"] = targets

        # ---- Formatters ----
        def _m(v):  return "—" if v is None else f"${v:,.2f}"
        def _i(v):  return "—" if v is None else f"{int(round(v)):,}"
        def _p(v):  return "—" if v is None else f"{v*100:.2f}%"
        def _safe(n, d): return (n / d) if d else None

        def _plabel(p):
            if grain == "Month":
                return p.strftime("%b %Y")
            if grain == "Week":
                return "wk " + p.start_time.strftime("%b %d")
            return p.strftime("%b %d")

        labels = [_plabel(p) for p in m.index]
        metrics = [
            "Ad Spend (AUD)", "Meta Leads", "Organic Leads", "Total Leads",
            "Appointments Created", "Cost Per Appointment", "Consultations (Showed)",
            "Booking to Consultation", "Leads to Consultation", "MARA Appointment Booked",
            "COE Received", "COE Target", "COE Conv. Rate", "Target Achieved %", "CAC",
        ]
        table = pd.DataFrame(index=metrics, columns=labels, dtype=object)
        for li, p in enumerate(m.index):
            r = m.loc[p]
            table.iloc[:, li] = [
                _m(r["spend"]),
                _i(r["meta"]), _i(r["organic"]), _i(r["total"]),
                _i(r["appts"]),
                _m(_safe(r["spend"], r["total"])),       # Cost per Appointment = spend / total leads
                _i(r["showed"]),
                _p(_safe(r["showed"], r["appts"])),       # Booking to Consultation = showed / bookings
                _p(_safe(r["showed"], r["total"])),       # Leads to Consultation = showed / total leads
                _i(r["mara"]),
                _i(r["coes"]),
                _i(r["coe_target"]),
                _p(_safe(r["coes"], r["total"])),
                _p(_safe(r["coes"], r["coe_target"])),
                _m(_safe(r["spend"], r["coes"])),
            ]
        table = table.reset_index().rename(columns={"index": "Metric"})
        st.dataframe(table, hide_index=True, use_container_width=True,
                     height=42 + 35 * len(metrics))

        st.caption(
            f"From Oct 2025 · last {len(labels)} {grain.lower()}(s). Each period is "
            "computed with the **Executive_1 cohort for that period's window**, so "
            "**Total Leads** matches the Executive_1 Leads card exactly (created OR "
            "revived OR booked-in that period, excl. No Activity & Queries). **Meta "
            "Leads** = Paid Social; **Organic Leads** = every other lead. **Appointments "
            "/ Consultations** are cohort booked / showed; **COE Received** = the "
            f"Executive_1 **Conversions** count. **Ad Spend USD→AUD at {fc_fx:.3f}**. "
            "*A contact can appear in more than one period (e.g. created one month, "
            "booked another) — matching how Executive_1 counts per period.*"
        )

        with st.expander("📖  Forecast & Goals — metric definitions"):
            st.markdown(
                """
- **Ad Spend** — Meta spend in the period.
- **Meta Leads** — leads classified **Paid Social** (Executive_1 logic).
- **Organic Leads** — every other Executive_1 lead (non-Paid-Social).
- **Total Leads** — Executive_1 leads for the period (created OR revived OR
  booked-in that period; excludes No Activity & Queries). Matches the
  Executive_1 Leads card for the same window.
- **Appointments Created** — of those leads, how many booked an appointment.
- **Cost Per Appointment** = Ad Spend ÷ Total Leads.
- **Consultations (Showed)** — of those leads, how many showed.
- **Booking to Consultation** = Showed ÷ Appointments Created (show ÷ bookings).
- **Leads to Consultation** = Showed ÷ Total Leads.
- **MARA Appointment Booked** — of those leads, how many reached the L2C-VISA
  'MARA Appointment Booked' stage (or beyond).
- **COE Received** — the Executive_1 **Conversions** count for the period (reached
  COE/Initial Received or Won in L2C-Education / CLT-Onshore Admission, dated by
  the last stage-change in the period).
- **COE Target** = previous period's target × (1 + growth %); first shown period
  seeded from the base value.
- **COE Conv. Rate** = COE Received ÷ Total Leads.
- **Target Achieved %** = COE Received ÷ COE Target.
- **CAC** = Ad Spend ÷ COE Received.
                """
            )

        # ---- Projection (Prophet) ----
        import altair as alt
        st.markdown("#### Projection — Prophet model")
        PROPHET_METRICS = {
            "Total Leads": "total", "Meta Leads": "meta", "Organic Leads": "organic",
            "Ad Spend (AUD)": "spend", "Appointments Created": "appts",
            "Consultations (Showed)": "showed", "MARA Appointment Booked": "mara",
            "COE Received": "coes",
        }
        pp1, pp2, _pp3 = st.columns([3, 2, 3])
        with pp1:
            proj_metric = st.selectbox("Metric to project", list(PROPHET_METRICS),
                                       key="fc_proj_metric")
        with pp2:
            horizon = int(st.number_input("Periods ahead", min_value=1, max_value=24,
                                          value=3, step=1, key="fc_horizon"))
        run_proj = st.toggle("Run Prophet forecast", key="fc_run_prophet")

        if not run_proj:
            st.caption("Toggle on to fit a Prophet model on the selected metric's history "
                       "(trend + seasonality) and project it forward with an uncertainty band.")
        else:
            col = PROPHET_METRICS[proj_metric]
            # Exclude the trailing INCOMPLETE period (current month/week/day) so a
            # partial data point doesn't drag the fit + forecast down.
            m_fit = m.iloc[:-1] if len(m) > 1 else m
            if len(m_fit) < 4:
                st.info("Need at least 4 complete periods of history for a useful Prophet "
                        "fit — switch to a finer grain (Week / Day) or widen the range.")
            else:
                try:
                    import logging
                    logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
                    from prophet import Prophet
                    freq_map = {"Month": "MS", "Week": "W", "Day": "D"}
                    dfp = pd.DataFrame({"ds": m_fit.index.to_timestamp(),
                                        "y": m_fit[col].astype(float).values})
                    mdl = Prophet(yearly_seasonality=False,
                                  weekly_seasonality=(grain == "Day"),
                                  daily_seasonality=False, interval_width=0.95)
                    mdl.fit(dfp)
                    fut_all = mdl.make_future_dataframe(periods=horizon, freq=freq_map[grain])
                    fcst = mdl.predict(fut_all)

                    last_hist = dfp["ds"].max()
                    money = (col == "spend")
                    fmt = (lambda v: f"${v:,.0f}") if money else (lambda v: f"{max(0, v):,.0f}")
                    pfmt = "%b %Y" if grain == "Month" else "%b %d, %Y"

                    # ---- clean, modern forecast chart ----
                    GREY, BLUE, BAND = "#5A5A5A", "#3B82F6", "#DBEAFE"
                    fut = fcst[fcst["ds"] > last_hist].copy()
                    fut["yl"] = fut["yhat_lower"].clip(lower=0)   # leads can't be negative
                    fut["yu"] = fut["yhat_upper"].clip(lower=0)
                    hist_l = dfp.rename(columns={"y": "value"})[["ds", "value"]].copy()
                    # bridge: start the forecast line at the last actual so they connect
                    bridge = hist_l.iloc[[-1]].rename(columns={"value": "yhat"})[["ds", "yhat"]]
                    fut_l = (pd.concat([bridge, fut[["ds", "yhat"]]], ignore_index=True)
                               .rename(columns={"yhat": "value"}))
                    fut_l["value"] = fut_l["value"].clip(lower=0)

                    xax = alt.Axis(format=("%b" if grain == "Month" else "%b %d"),
                                   labelFontSize=11, labelColor="#6B7280", grid=True,
                                   gridColor="#E5E7EB", gridOpacity=0.7, tickColor="#E5E7EB",
                                   domainColor="#E5E7EB", title=None)
                    yax = alt.Axis(labelFontSize=11, labelColor="#6B7280", grid=True,
                                   gridColor="#E5E7EB", gridOpacity=0.7, domain=False, ticks=False)
                    ysc = alt.Scale(zero=True)
                    band = alt.Chart(fut).mark_area(color=BAND, opacity=0.55).encode(
                        x=alt.X("ds:T", axis=xax),
                        y=alt.Y("yl:Q", title=proj_metric, axis=yax, scale=ysc), y2="yu:Q")
                    a_line = alt.Chart(hist_l).mark_line(
                        interpolate="monotone", color=GREY, strokeWidth=2.6).encode(
                        x=alt.X("ds:T", axis=xax),
                        y=alt.Y("value:Q", title=None, axis=yax, scale=ysc),
                        tooltip=[alt.Tooltip("ds:T", title="Period", format=pfmt),
                                 alt.Tooltip("value:Q", title=proj_metric, format=",.0f")])
                    f_line = alt.Chart(fut_l).mark_line(
                        interpolate="monotone", color=BLUE, strokeWidth=2.6,
                        strokeDash=[6, 4]).encode(
                        x=alt.X("ds:T", axis=xax),
                        y=alt.Y("value:Q", title=None, axis=yax, scale=ysc),
                        tooltip=[alt.Tooltip("ds:T", title="Period", format=pfmt),
                                 alt.Tooltip("value:Q", title=proj_metric, format=",.0f")])

                    # header: title (left) + model info (right)
                    st.markdown(
                        "<div style='display:flex;justify-content:space-between;"
                        "align-items:baseline;margin:6px 2px 0'>"
                        f"<span style='font-size:18px;font-weight:700;color:#111827'>"
                        f"{proj_metric} — {horizon}-period forecast</span>"
                        "<span style='font-size:12px;color:#9CA3AF'>Prophet model &bull; "
                        "95% confidence interval</span></div>", unsafe_allow_html=True)
                    def _sq(c):
                        return (f"<span style='display:inline-block;width:11px;height:11px;"
                                f"background:{c};border-radius:2px;margin:0 6px -1px 0'></span>")
                    st.markdown(
                        "<div style='font-size:12px;color:#4B5563;margin:3px 2px 6px'>"
                        f"{_sq(GREY)}Actual&nbsp;&nbsp;&nbsp;{_sq(BLUE)}Forecast&nbsp;&nbsp;&nbsp;"
                        f"{_sq(BAND)}95% confidence band</div>", unsafe_allow_html=True)
                    st.altair_chart(
                        (band + a_line + f_line).properties(height=340)
                        .configure_view(strokeWidth=0), use_container_width=True)

                    # Table: complete history (actuals) + forecast (future).
                    rows = []
                    for p in m_fit.index:
                        rows.append({"Period": _plabel(p), "Type": "Actual",
                                     "Value": fmt(float(m_fit.loc[p, col])),
                                     "Low (95%)": "—", "High (95%)": "—"})
                    for _, fr in fut.iterrows():
                        rows.append({"Period": fr["ds"].strftime(pfmt), "Type": "Forecast",
                                     "Value": fmt(fr["yhat"]), "Low (95%)": fmt(fr["yl"]),
                                     "High (95%)": fmt(fr["yu"])})
                    out = pd.DataFrame(rows).rename(columns={"Value": proj_metric})
                    st.dataframe(out, hide_index=True, use_container_width=True,
                                 height=min(600, 42 + 35 * len(out)))
                    st.caption(
                        "Prophet additive model · 95% interval. Charcoal = actuals, dashed "
                        "blue = forecast, shaded = confidence band. The current incomplete "
                        f"{grain.lower()} is excluded so it doesn't drag the fit. Table shows "
                        f"complete history plus the next {horizon} {grain.lower()}(s). "
                        f"Fit on {len(dfp)} points."
                    )
                except Exception as e:
                    st.error(f"Prophet failed: {e}")


# =====================================================================
# UPLOAD REPORTS TAB — Lead Journey: days-in-stage matrix
# =====================================================================

with tab_up:
    import altair as alt

    st.markdown("### Upload & Compare Reports")
    st.caption(
        "Upload a report you exported from this dashboard (e.g. open **Forecast & "
        "Goals**, hover the table and click the download icon) - or any CSV / Excel "
        "laid out as one row per metric and one column per period. Pick the metrics "
        "below; the comparison chart updates above.")

    up = st.file_uploader("Upload a report (CSV or Excel)",
                          type=["csv", "xlsx", "xls"], key="upcmp_file")
    if up is None:
        st.info("Upload a file to begin.")
    else:
        try:
            raw = (pd.read_excel(up) if up.name.lower().endswith((".xlsx", ".xls"))
                   else pd.read_csv(up))
        except Exception as e:
            st.error(f"Could not read the file: {e}")
            raw = pd.DataFrame()

        if raw.empty or raw.shape[1] < 2:
            st.warning("The file needs at least a label column and one value column.")
        else:
            orient = st.radio("Layout of the file",
                              ["Metrics in rows", "Metrics in columns"],
                              horizontal=True, key="upcmp_orient")
            df = raw.copy()
            if orient == "Metrics in columns":
                df = (df.set_index(df.columns[0]).T
                        .reset_index().rename(columns={"index": "Metric"}))
            label_col = df.columns[0]
            period_cols = [c for c in df.columns[1:]]

            def _num(v):
                s = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
                try:
                    return float(s)
                except ValueError:
                    return None
            num = df.copy()
            for c in period_cols:
                num[c] = num[c].map(_num)

            metrics_all = df[label_col].astype(str).tolist()
            picked = st.multiselect("Metrics to compare", metrics_all,
                                    default=metrics_all[:3], key="upcmp_metrics")
            if not picked:
                st.info("Select one or more metrics to compare.")
            else:
                sub = num[num[label_col].astype(str).isin(picked)].copy()
                sub[label_col] = sub[label_col].astype(str)
                lng = (sub.melt(id_vars=[label_col], value_vars=period_cols,
                                var_name="Period", value_name="Value")
                          .rename(columns={label_col: "Metric"})
                          .dropna(subset=["Value"]))
                _po = {p: i for i, p in enumerate(period_cols)}
                lng = lng.sort_values("Period", key=lambda s: s.map(_po))

                norm = st.checkbox(
                    "Normalise each metric (first period = 100) - useful when metrics "
                    "are on very different scales", value=False, key="upcmp_norm")
                plot = lng.copy()
                if norm:
                    bases = {}
                    for _, r in sub.iterrows():
                        vals = [r[c] for c in period_cols if pd.notna(r[c])]
                        bases[str(r[label_col])] = (vals[0] if vals else None)
                    plot["Value"] = plot.apply(
                        lambda r: (r["Value"] / bases[r["Metric"]] * 100)
                        if bases.get(r["Metric"]) else r["Value"], axis=1)

                chart = (alt.Chart(plot).mark_line(point=True).encode(
                    x=alt.X("Period:N", sort=period_cols, title=None),
                    y=alt.Y("Value:Q",
                            title=("Indexed (first = 100)" if norm else None)),
                    color=alt.Color("Metric:N",
                                    legend=alt.Legend(orient="top", title=None)),
                    tooltip=["Metric:N", "Period:N",
                             alt.Tooltip("Value:Q", format=",.2f")])
                    .properties(height=360).configure_view(strokeWidth=0))
                st.altair_chart(chart, use_container_width=True)

                st.markdown("**Comparison - selected metrics**")
                show = sub.set_index(label_col)[period_cols]
                st.dataframe(show, use_container_width=True)
                st.download_button("Download comparison (CSV)",
                                   show.to_csv().encode("utf-8"),
                                   file_name="report_comparison.csv", mime="text/csv",
                                   key="upcmp_dl")


# =====================================================================
# EXECUTIVE_1 TAB — Leads (created OR revived) by REFINED source,
# clickable to the contact-level detail.
# =====================================================================
with tab_e1:
    st.markdown(
        "<div class='panel-title'>Executive_1 — Leads by source"
        "<span class='hint'>created or revived in the selected range</span></div>",
        unsafe_allow_html=True)

    e1 = run_df("vw_exec1_lead_detail", {"since": since.isoformat(), "until": until.isoformat()})
    if e1.empty:
        st.info("No leads created or revived in this window.")
    else:
        e1["booked_in_range"] = e1["booked_in_range"].fillna(False).astype(bool)
        # 'No Activity' = bare CRM records (no form/conversation/pipeline/appt/
        # payment) — never counted as leads. Drop them up front.
        e1 = e1[e1["refined_source"] != "No Activity"].copy()
        q_mask = e1["refined_source"] == "Queries"
        leads_df = e1[~q_mask]
        n_leads = len(leads_df)
        n_queries = int(q_mask.sum())
        n_new = int(leads_df["is_created"].sum())
        n_rev = int(leads_df["is_revived"].sum())
        n_bookonly = int((leads_df["booked_in_range"]
                          & (leads_df["is_created"] == 0) & (leads_df["is_revived"] == 0)).sum())
        cur_booked = int(leads_df["appt_booked"].sum())
        cur_showed = int(leads_df["appt_showed"].sum())
        q_booked = int(e1.loc[q_mask, "appt_booked"].sum())

        # ---- prior period (for the vs-last comparison) ----
        e1p = run_df("vw_exec1_lead_detail",
                     {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
        if e1p.empty:
            p_leads = p_queries = p_booked = p_showed = 0
        else:
            _qp = e1p["refined_source"] == "Queries"
            p_leads = int((~_qp).sum())
            p_queries = int(_qp.sum())
            p_booked = int(e1p.loc[~_qp, "appt_booked"].sum())
            p_showed = int(e1p.loc[~_qp, "appt_showed"].sum())

        def _d(cur, pri):
            if not pri:
                return ("", "#9aa0a6")
            pct = (cur - pri) / pri * 100
            if abs(pct) < 0.5:
                return ("— vs last", "#9aa0a6")
            up = pct > 0
            return (f"{'▲' if up else '▼'} {abs(pct):.0f}% vs last",
                    "#15803d" if up else "#dc2626")

        # ---- Conversions: COE/Initial Received or Won in L2C-Edu / CLT-Onshore,
        #      dated by last stage-change (updated_at) in window ----
        conv_df = run_df("vw_exec1_conversions",
                         {"since": since.isoformat(), "until": until.isoformat()})
        conv_pri = run_df("vw_exec1_conversions",
                          {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
        n_conv = len(conv_df)
        p_conv = len(conv_pri)

        # ---- Ad spend (Meta, USD->AUD) + Revenue (GHL succeeded payments) ----
        fx = usd_to_aud()
        _ms_cur = run_df("vw_exec_meta_spend", {"since": since.isoformat(), "until": until.isoformat()})
        _ms_pri = run_df("vw_exec_meta_spend",
                         {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
        spend_aud   = (float(_ms_cur["spend"].sum()) if not _ms_cur.empty else 0.0) * fx
        p_spend_aud = (float(_ms_pri["spend"].sum()) if not _ms_pri.empty else 0.0) * fx

        rev_cur = run_df("vw_exec1_revenue_detail", {"since": since.isoformat(), "until": until.isoformat()})
        rev_pri = run_df("vw_exec1_revenue_detail",
                         {"since": prior_since.isoformat(), "until": prior_until.isoformat()})
        rev_aud   = float(rev_cur["revenue"].sum()) if not rev_cur.empty else 0.0
        p_rev_aud = float(rev_pri["revenue"].sum()) if not rev_pri.empty else 0.0

        # conversion rates
        ltb   = (cur_booked / n_leads) if n_leads else 0.0
        p_ltb = (p_booked / p_leads) if p_leads else 0.0
        sr    = (cur_showed / cur_booked) if cur_booked else 0.0
        p_sr  = (p_showed / p_booked) if p_booked else 0.0

        def _dpp(cur_rate, pri_rate):
            """Delta in percentage-points for a rate metric."""
            if not pri_rate:
                return ("", "#9aa0a6")
            diff = (cur_rate - pri_rate) * 100
            if abs(diff) < 0.5:
                return ("— vs last", "#9aa0a6")
            up = diff > 0
            return (f"{'▲' if up else '▼'} {abs(diff):.0f} pts vs last",
                    "#15803d" if up else "#dc2626")

        # ---- master by-source summary (drives the per-card summary tables) ----
        # Walk-in / Agentcis / Unknown plus the channel-named sources (Phone/SMS,
        # Web Chat, Email) are grouped under one "Others" umbrella in the by-source
        # tables; the granular value stays on e1 for the drill-down.
        OTHERS_SUB = ["Walk-in", "Agentcis", "Unknown", "Phone/SMS", "Web Chat", "Email"]
        e1["src_group"] = e1["refined_source"].where(
            ~e1["refined_source"].isin(OTHERS_SUB), "Others")
        src = (e1.groupby("src_group")
               .agg(Leads=("contact_id", "count"), Opportunities=("n_opps", "sum"),
                    Booked=("appt_booked", "sum"), Showed=("appt_showed", "sum"))
               .reset_index().rename(columns={"src_group": "Source"}))
        if not conv_df.empty:
            _grp = conv_df["source"].apply(
                lambda s: "Others" if s in OTHERS_SUB + ["Other / Unknown"] else s)
            _cc = _grp.value_counts().rename_axis("Source").reset_index(name="Conversions")
            src = src.merge(_cc, on="Source", how="left")
        else:
            src["Conversions"] = 0
        src["Conversions"] = src["Conversions"].fillna(0).astype(int)
        for _c in ["Leads", "Opportunities", "Booked", "Showed"]:
            src[_c] = src[_c].astype(int)
        src["Booking Rate"] = (src["Booked"] / src["Leads"]).replace([float("inf")], 0).fillna(0)
        src["Show Rate"] = (src["Showed"] / src["Booked"]).replace([float("inf")], 0).fillna(0)
        src["% of Leads"] = (src["Leads"] / src["Leads"].sum()).fillna(0)
        src = src.sort_values("Leads", ascending=False).reset_index(drop=True)

        # Prior-period per-source booking/show rates — drives the ▲/▼ deltas in
        # Table 1 (Booked / Showed cells).
        pr_br, pr_sr = {}, {}
        if not e1p.empty:
            _e1p = e1p[e1p["refined_source"] != "No Activity"].copy()
            _e1p["src_group"] = _e1p["refined_source"].where(
                ~_e1p["refined_source"].isin(OTHERS_SUB), "Others")
            _sp = (_e1p.groupby("src_group")
                   .agg(Leads=("contact_id", "count"), Booked=("appt_booked", "sum"),
                        Showed=("appt_showed", "sum")).reset_index())
            for _, _r in _sp.iterrows():
                pr_br[_r["src_group"]] = (_r["Booked"] / _r["Leads"]) if _r["Leads"] else None
                pr_sr[_r["src_group"]] = (_r["Showed"] / _r["Booked"]) if _r["Booked"] else None

        def _rate_cell(count, rate_cur, rate_pri):
            """'<count>  <rate>% ▲/▼<pts>' + a colour for the cell."""
            txt = f"{int(count)}  ·  {rate_cur*100:.0f}%"
            if rate_pri is None or pd.isna(rate_pri):
                return txt, "#6b7280"
            diff = (rate_cur - rate_pri) * 100
            if abs(diff) < 0.5:
                return f"{txt}  —", "#6b7280"
            up = diff > 0
            return f"{txt}  {'▲' if up else '▼'}{abs(diff):.0f}", ("#15803d" if up else "#dc2626")

        def _fmt_summary(cols):
            disp = pd.DataFrame({"Source": src["Source"]})
            for c in cols:
                if c in ("Booking Rate", "Show Rate", "% of Leads"):
                    disp[c] = src[c].map(lambda v: f"{v*100:.0f}%")
                else:
                    disp[c] = src[c].astype(int)
            return disp

        def _leads_emails(df):
            dd = df.copy()
            appt = dd.apply(lambda r: "Showed" if r["appt_showed"] == 1
                            else ("Booked" if r["appt_booked"] == 1 else "—"), axis=1)
            cal = dd.apply(lambda r: r["calendar_name"]
                           if (r["appt_booked"] == 1 and pd.notna(r["calendar_name"])) else "—", axis=1)
            return pd.DataFrame({
                "Email": dd["email"].fillna("(no email)").values,
                "Source": dd["refined_source"].values,
                "Platform": dd["social_platform"].where(
                    dd["refined_source"].isin(["Social media", "Paid Social"]), "—")
                    .fillna("—").replace("", "—").values,
                "Lead Created Date": pd.to_datetime(dd["lead_date"]).dt.strftime("%Y-%m-%d").values,
                "Appt Created Date": dd["appt_booked_date"].map(
                    lambda v: pd.to_datetime(v).strftime("%Y-%m-%d") if pd.notna(v) else "—").values,
                "Appointment Status": appt.values,
                "Calendar Name": cal.values,
                "Pipeline": dd["pipeline"].fillna("—").values,
                "Stage": dd["stage"].fillna("—").values,
                "Name": dd["contact_name"].fillna("—").replace("", "—").values,
                "Phone": dd["phone"].fillna("—").replace("", "—").values,
            })

        def _dl(df, label, key):
            st.download_button(label, df.to_csv(index=False).encode("utf-8"),
                               file_name=f"exec1_{key}_{since.isoformat()}_{until.isoformat()}.csv",
                               mime="text/csv", key=f"e1dl_{key}")

        ICON = {"Leads": "👥", "Queries": "🔍", "Booked": "📅", "Showed": "✅",
                "Conversions": "🎯", "Ad Spend": "💰", "Leads to Booking": "📈",
                "Show Rate": "📊", "Revenue": "💵"}

        # ---- Drill-down modal: opens when a scorecard is clicked (Executive style) ----
        @st.dialog(" ", width="large")
        def _e1_modal():
            card = st.session_state.get("e1_card", "Leads")
            st.markdown(f"### {ICON.get(card, '')} {card} — Drill Down")

            if card == "Leads":
                st.caption(f"Lead→Booking **{ltb*100:.0f}%** · Booking→Show **{sr*100:.0f}%**")
                st.markdown("**Table 1 — by source** (click a row to filter the contacts below)")
                # Queries have their own scorecard — exclude them here and
                # recompute "% of Leads" over the non-Queries total.
                _sx = src[src["Source"] != "Queries"].copy().reset_index(drop=True)
                _tot = int(_sx["Leads"].sum())
                # Booked / Showed cells carry the booking-/show-rate with a
                # ▲/▼ vs last period (coloured green/red).
                _bk_txt, _bk_col, _sh_txt, _sh_col = [], [], [], []
                for _, _r in _sx.iterrows():
                    s = _r["Source"]
                    t, c = _rate_cell(_r["Booked"], _r["Booking Rate"], pr_br.get(s))
                    _bk_txt.append(t); _bk_col.append(c)
                    t, c = _rate_cell(_r["Showed"], _r["Show Rate"], pr_sr.get(s))
                    _sh_txt.append(t); _sh_col.append(c)
                summ = pd.DataFrame({
                    "Source": _sx["Source"].values,
                    "Leads": _sx["Leads"].astype(int).values,
                    "Opportunities": _sx["Opportunities"].astype(int).values,
                    "Booked": _bk_txt,
                    "Showed": _sh_txt,
                    "% of Leads": ((_sx["Leads"] / _tot * 100) if _tot else _sx["Leads"] * 0)
                                  .map(lambda v: f"{v:.0f}%").values,
                })
                _csm = pd.DataFrame("", index=summ.index, columns=summ.columns)
                _csm["Booked"] = [f"color:{c}; font-weight:600" for c in _bk_col]
                _csm["Showed"] = [f"color:{c}; font-weight:600" for c in _sh_col]
                _sty = summ.style.apply(lambda _: _csm, axis=None)
                sel = st.dataframe(_sty, hide_index=True, use_container_width=True,
                                   on_select="rerun", selection_mode="single-row",
                                   key="e1_src_sel", height=300)
                picked = None
                try:
                    rows = (sel.selection.get("rows") if sel else None) or []
                    if rows:
                        picked = summ.iloc[int(rows[0])]["Source"]
                except Exception:
                    picked = None
                def _nested(base_df, group_col, label, key, prior_df=None):
                    nb = (base_df.groupby(group_col)
                          .agg(Leads=("contact_id", "count"), Opportunities=("n_opps", "sum"),
                               Booked=("appt_booked", "sum"), Showed=("appt_showed", "sum"))
                          .reset_index().sort_values("Leads", ascending=False))
                    # prior-period per-group booking/show rates → ▲/▼ vs last period
                    pbr, psr = {}, {}
                    if prior_df is not None and not prior_df.empty:
                        _pp = (prior_df.groupby(group_col)
                               .agg(Leads=("contact_id", "count"), Booked=("appt_booked", "sum"),
                                    Showed=("appt_showed", "sum")).reset_index())
                        for _, _r in _pp.iterrows():
                            _g = _r[group_col]
                            pbr[_g] = (_r["Booked"] / _r["Leads"]) if _r["Leads"] else None
                            psr[_g] = (_r["Showed"] / _r["Booked"]) if _r["Booked"] else None
                    # Booked / Showed carry count · rate% · ▲/▼ (coloured green/red)
                    _bt, _bc, _stt, _sc = [], [], [], []
                    for _, _r in nb.iterrows():
                        _g = _r[group_col]
                        _br = (_r["Booked"] / _r["Leads"]) if _r["Leads"] else 0.0
                        _sr2 = (_r["Showed"] / _r["Booked"]) if _r["Booked"] else 0.0
                        t, c = _rate_cell(_r["Booked"], _br, pbr.get(_g)); _bt.append(t); _bc.append(c)
                        t, c = _rate_cell(_r["Showed"], _sr2, psr.get(_g)); _stt.append(t); _sc.append(c)
                    nd = pd.DataFrame({
                        label: nb[group_col].fillna("—").replace("", "—").values,
                        "Leads": nb["Leads"].astype(int).values,
                        "Opportunities": nb["Opportunities"].astype(int).values,
                        "Booked": _bt,
                        "Showed": _stt,
                    })
                    _cm = pd.DataFrame("", index=nd.index, columns=nd.columns)
                    _cm["Booked"] = [f"color:{c}; font-weight:600" for c in _bc]
                    _cm["Showed"] = [f"color:{c}; font-weight:600" for c in _sc]
                    _sty2 = nd.style.apply(lambda _: _cm, axis=None)
                    s2 = st.dataframe(_sty2, hide_index=True, use_container_width=True,
                                      on_select="rerun", selection_mode="single-row",
                                      key=key, height=180)
                    p2 = None
                    try:
                        rr = (s2.selection.get("rows") if s2 else None) or []
                        if rr:
                            p2 = nd.iloc[int(rr[0])][label]
                    except Exception:
                        p2 = None
                    db = base_df if not p2 else base_df[base_df[group_col].fillna("—") == p2]
                    return db, f"{label} — {p2 or 'all'}"

                # ---- Booked vs Showed trend (between Table 1 and Table 2) ----
                st.markdown("**Booked vs Showed — trend**")
                gran = st.segmented_control(
                    "Trend by", ["Day", "Week", "Month"], key="e1_trend_gran") or "Month"

                def _months_back(d, n):
                    m = d.month - 1 - n
                    return date(d.year + m // 12, m % 12 + 1, 1)

                if gran == "Day":
                    _tsince = until - timedelta(days=29)
                elif gran == "Week":
                    _tsince = until - timedelta(weeks=11)
                else:
                    _tsince = _months_back(until, 11)
                _tr = run_df("vw_exec1_lead_detail",
                             {"since": _tsince.isoformat(), "until": until.isoformat()})
                if not _tr.empty:
                    _tr = _tr[~_tr["refined_source"].isin(["No Activity", "Queries"])]
                _tb = _tr[_tr["appt_booked"] == 1].copy() if not _tr.empty else _tr
                if not _tb.empty:
                    _tb["dt"] = pd.to_datetime(_tb["appt_booked_date"], errors="coerce")
                    _tb = _tb.dropna(subset=["dt"])
                if _tb.empty:
                    st.caption("No bookings in the trailing window to chart.")
                else:
                    if gran == "Day":
                        _tb["period"] = _tb["dt"].dt.normalize()
                    elif gran == "Week":
                        _tb["period"] = (_tb["dt"]
                                         - pd.to_timedelta(_tb["dt"].dt.weekday, unit="D")).dt.normalize()
                    else:
                        _tb["period"] = _tb["dt"].dt.to_period("M").dt.to_timestamp()
                    _bk = _tb.groupby("period").size().rename("Booked")
                    _sh = _tb[_tb["appt_showed"] == 1].groupby("period").size().rename("Showed")
                    _ts = pd.concat([_bk, _sh], axis=1).fillna(0).reset_index()
                    _ts[["Booked", "Showed"]] = _ts[["Booked", "Showed"]].astype(int)
                    _long = _ts.melt("period", value_vars=["Booked", "Showed"],
                                     var_name="Metric", value_name="Count")
                    import altair as alt
                    _ch = (alt.Chart(_long).mark_line(point=True).encode(
                        x=alt.X("period:T", title=None,
                                axis=alt.Axis(format=("%b %d" if gran != "Month" else "%b %y"))),
                        y=alt.Y("Count:Q", title=None),
                        color=alt.Color("Metric:N",
                                        scale=alt.Scale(domain=["Booked", "Showed"],
                                                        range=["#4DA6FF", "#7A52CC"]),
                                        legend=alt.Legend(title=None, orient="top")),
                        tooltip=[alt.Tooltip("period:T", title="Period"),
                                 "Metric:N", "Count:Q"])
                        .properties(height=200))
                    st.altair_chart(_ch, use_container_width=True)
                    st.caption(f"All leads · Booked vs Showed by {gran.lower()} "
                               f"({_tsince.strftime('%b %d, %Y')} → {until.strftime('%b %d, %Y')}). "
                               "Widen the date filter for a longer history.")

                if picked == "Others":
                    st.markdown("**Others — breakdown** (Walk-in · Agentcis · Phone/SMS · "
                                "Web Chat · Email · Unknown — click a row)")
                    base, ttl = _nested(e1[e1["src_group"] == "Others"], "refined_source",
                                        "Sub-source", "e1_others_sel",
                                        prior_df=(e1p[e1p["refined_source"].isin(OTHERS_SUB)]
                                                  if not e1p.empty else None))
                elif picked == "Social media":
                    st.markdown("**Social media — by platform** (Instagram · LinkedIn · TikTok · "
                                "WhatsApp · Facebook — click a row)")
                    base, ttl = _nested(e1[e1["refined_source"] == "Social media"],
                                        "social_platform", "Platform", "e1_social_sel",
                                        prior_df=(e1p[e1p["refined_source"] == "Social media"]
                                                  if not e1p.empty else None))
                else:
                    base = leads_df if not picked else e1[e1["src_group"] == picked]
                    ttl = "Table 2 — all leads" if not picked else f"Table 2 — {picked}"
                st.markdown(f"**{ttl} · {len(base):,} contacts**")
                tbl = _leads_emails(base).sort_values("Lead Created Date", ascending=False)
                st.dataframe(tbl, hide_index=True, use_container_width=True, height=420)
                _dl(tbl, "Download (CSV)", "leads")

            elif card == "Queries":
                q = e1[q_mask]
                st.markdown(f"**Queries — {len(q):,} contacts**")
                tbl = _leads_emails(q).sort_values("Lead Created Date", ascending=False)
                st.dataframe(tbl, hide_index=True, use_container_width=True, height=420)
                _dl(tbl, "Download (CSV)", "queries")
                st.markdown("**Summary**")
                qr = src[src["Source"] == "Queries"]
                if qr.empty:
                    st.caption("No queries in this window.")
                else:
                    qs = pd.DataFrame({
                        "Leads": qr["Leads"].astype(int).values,
                        "Opportunities": qr["Opportunities"].astype(int).values,
                        "Booked": qr["Booked"].astype(int).values,
                        "Showed": qr["Showed"].astype(int).values,
                        "% of Leads": (qr["% of Leads"] * 100).map(lambda v: f"{v:.0f}%").values,
                    })
                    st.dataframe(qs, hide_index=True, use_container_width=True)

            elif card in ("Booked", "Leads to Booking"):
                st.caption(f"Lead→Booking rate **{ltb*100:.0f}%** ({cur_booked:,} of {n_leads:,} leads)")
                st.markdown("**Table 1 — by source (through booking rate)**")
                st.dataframe(_fmt_summary(["Leads", "Opportunities", "Booked", "Booking Rate"]),
                             hide_index=True, use_container_width=True, height=300)
                bdf = leads_df[leads_df["appt_booked"] == 1]
                st.markdown(f"**Booked contacts · {len(bdf):,}**")
                tbl = _leads_emails(bdf).sort_values("Appt Created Date", ascending=False)
                st.dataframe(tbl, hide_index=True, use_container_width=True, height=440)
                _dl(tbl, "Download (CSV)", "booked")

            elif card in ("Showed", "Show Rate"):
                st.caption(f"Booking→Show rate **{sr*100:.0f}%** ({cur_showed:,} of {cur_booked:,} booked)")
                st.markdown("**Table 1 — by source (through show rate)**")
                st.dataframe(_fmt_summary(["Leads", "Booked", "Showed", "Show Rate"]),
                             hide_index=True, use_container_width=True, height=300)
                sdf = leads_df[leads_df["appt_showed"] == 1]
                st.markdown(f"**Showed contacts · {len(sdf):,}**")
                tbl = _leads_emails(sdf).sort_values("Appt Created Date", ascending=False)
                st.dataframe(tbl, hide_index=True, use_container_width=True, height=440)
                _dl(tbl, "Download (CSV)", "showed")

            elif card == "Conversions":
                st.caption("**COE** = COE / Initial Received or Won in L2C-Education / "
                           "CLT-Onshore Admission. **POC** = Application Submitted / "
                           "Acknowledgment Sent + Doc or Won in CLT-VISA. Dated by the "
                           "last stage-change in the window.")
                _ctype = st.segmented_control(
                    "Conversion type", ["All", "POC", "COE"],
                    default="All", key="e1_conv_type") or "All"
                _cdf = conv_df if (conv_df.empty or _ctype == "All") \
                    else conv_df[conv_df["conv_type"] == _ctype]
                if _cdf.empty:
                    st.caption(f"No {_ctype} conversions in this window.")
                else:
                    cc = _cdf.copy()
                    cc["is_won"] = (cc["status"].astype(str).str.lower() == "won").astype(int)
                    cc["is_coe"] = (cc["conv_type"] == "COE").astype(int)
                    cc["is_poc"] = (cc["conv_type"] == "POC").astype(int)
                    csum = (cc.groupby("source")
                            .agg(Conversions=("contact_id", "count"), coe=("is_coe", "sum"),
                                 poc=("is_poc", "sum"), won=("is_won", "sum"))
                            .reset_index()
                            .rename(columns={"source": "Source", "coe": "COE",
                                             "poc": "POC", "won": "Won"})
                            .sort_values("Conversions", ascending=False))
                    csum["% of Conv"] = (csum["Conversions"] / csum["Conversions"].sum() * 100) \
                        .map(lambda v: f"{v:.0f}%")
                    # grand total row
                    csum = pd.concat([csum, pd.DataFrame([{
                        "Source": "TOTAL",
                        "Conversions": int(csum["Conversions"].sum()),
                        "COE": int(csum["COE"].sum()), "POC": int(csum["POC"].sum()),
                        "Won": int(csum["Won"].sum()), "% of Conv": "100%"}])], ignore_index=True)
                    st.markdown(f"**Table 1 — by source** ({_ctype} · {len(_cdf):,})")
                    st.dataframe(csum, hide_index=True, use_container_width=True, height=315)
                    cv = pd.DataFrame({
                        "Email": _cdf["email"].fillna("(no email)"),
                        "Type": _cdf["conv_type"],
                        "Source": _cdf["source"],
                        "Detail": _cdf["detail"].fillna("—").replace("", "—"),
                        "Pipeline": _cdf["pipeline"],
                        "Stage": _cdf["stage"],
                        "Status": _cdf["status"],
                        "Last State-Change Date": pd.to_datetime(_cdf["changed_date"]).dt.strftime("%Y-%m-%d"),
                    }).sort_values("Last State-Change Date", ascending=False)
                    st.markdown(f"**Conversion contacts · {len(cv):,}**")
                    st.dataframe(cv, hide_index=True, use_container_width=True, height=440)
                    _dl(cv, "Download (CSV)", "conversions")

            elif card == "Ad Spend":
                st.caption(f"Meta ad spend, USD→AUD @ {fx:.2f}. Total **${spend_aud:,.0f}**.")
                ad = run_df("vw_exec1_adspend_detail", {"since": since.isoformat(), "until": until.isoformat()})
                if ad.empty:
                    st.caption("No Meta spend in this window.")
                else:
                    ad = ad.sort_values("spend", ascending=False)
                    at = pd.DataFrame({
                        "Account": ad["account_label"].values,
                        "Campaign": ad["campaign_name"].astype(str).str.slice(0, 48).values,
                        "Spend (AUD)": (ad["spend"] * fx).map(lambda v: f"${v:,.0f}").values,
                        "Impressions": ad["impressions"].fillna(0).astype(int).values,
                        "Clicks": ad["clicks"].fillna(0).astype(int).values,
                        "Meta Leads": ad["leads"].fillna(0).astype(int).values,
                        "CPL (AUD)": ((ad["spend"] * fx) / ad["leads"].replace(0, float("nan"))).map(
                            lambda v: f"${v:,.0f}" if pd.notna(v) else "—").values,
                    })
                    st.dataframe(at, hide_index=True, use_container_width=True, height=420)
                    _dl(at, "Download (CSV)", "ad_spend")

            elif card == "Revenue":
                st.caption(f"Succeeded GHL payments in window. Total **${rev_aud:,.0f}** "
                           f"from {len(rev_cur):,} payers.")
                if rev_cur.empty:
                    st.caption("No payments in this window.")
                else:
                    rsum = (rev_cur.groupby("source")
                            .agg(Payers=("revenue", "count"), Revenue=("revenue", "sum"))
                            .reset_index().rename(columns={"source": "Source"})
                            .sort_values("Revenue", ascending=False))
                    rsum_disp = pd.DataFrame({
                        "Source": rsum["Source"].values,
                        "Payers": rsum["Payers"].astype(int).values,
                        "Revenue (AUD)": rsum["Revenue"].map(lambda v: f"${v:,.0f}").values,
                        "% of Rev": (rsum["Revenue"] / rsum["Revenue"].sum() * 100).map(lambda v: f"{v:.0f}%").values,
                    })
                    st.markdown("**By source**")
                    st.dataframe(rsum_disp, hide_index=True, use_container_width=True, height=260)
                    rd = rev_cur.sort_values("revenue", ascending=False)
                    rt = pd.DataFrame({
                        "Email": rd["email"].fillna("(no email)").values,
                        "Source": rd["source"].values,
                        "Revenue (AUD)": rd["revenue"].map(lambda v: f"${v:,.0f}").values,
                        "Last Payment Date": pd.to_datetime(rd["last_payment_date"]).dt.strftime("%Y-%m-%d").values,
                    })
                    st.markdown(f"**Paying contacts · {len(rt):,}**")
                    st.dataframe(rt, hide_index=True, use_container_width=True, height=420)
                    _dl(rt, "Download (CSV)", "revenue")

            elif card == "Blended CPA":
                cpa_txt = f"${blended_cpa:,.0f}" if blended_cpa else "—"
                st.caption(f"**{cpa_txt}** = ${spend_aud:,.0f} Meta spend ÷ {total_appts:,} "
                           "appointments booked in the window (all sources blended).")
                st.markdown("**By source — booking ratio**")
                st.dataframe(_fmt_summary(["Leads", "Booked", "Booking Rate"]),
                             hide_index=True, use_container_width=True, height=300)
                adf = e1[e1["appt_booked"] == 1]
                st.markdown(f"**Booked appointments · {len(adf):,} contacts**")
                tbl = _leads_emails(adf).sort_values("Appt Created Date", ascending=False)
                st.dataframe(tbl, hide_index=True, use_container_width=True, height=440)
                _dl(tbl, "Download (CSV)", "blended_cpa")

        # ---- Scorecards: click a card to open its drill-down modal ----
        def _e1_scorecard(col, name, value, *extra):
            lines = [name.upper(), value] + [x for x in extra if x]
            with col:
                if st.button("\n\n".join(lines), key=f"e1sc_{name}",
                             use_container_width=True):
                    st.session_state["e1_card"] = name
                    _e1_modal()

        sub = (f"{n_new:,} created · {n_rev:,} revived"
               + (f" · {n_bookonly:,} booked-in" if n_bookonly else ""))
        kc = st.columns(5)
        _e1_scorecard(kc[0], "Leads", f"{n_leads:,}", sub,
                      _delta_md(n_leads, p_leads, higher_is_better=True, fmt="pct"))
        _e1_scorecard(kc[1], "Queries", f"{n_queries:,}", f"no pipeline · {q_booked:,} booked",
                      _delta_md(n_queries, p_queries, higher_is_better=True, fmt="pct"))
        _e1_scorecard(kc[2], "Booked", f"{cur_booked:,}", "appointment booked",
                      _delta_md(cur_booked, p_booked, higher_is_better=True, fmt="pct"))
        _e1_scorecard(kc[3], "Showed", f"{cur_showed:,}", "consultation attended",
                      _delta_md(cur_showed, p_showed, higher_is_better=True, fmt="pct"))
        _e1_scorecard(kc[4], "Conversions", f"{n_conv:,}", "COE + POC · click for All/POC/COE",
                      _delta_md(n_conv, p_conv, higher_is_better=True, fmt="pct"))

        # Blended cost per appointment = Meta ad spend ÷ ALL appointments booked
        # in the window (across every source — "blended").
        total_appts = int(e1["appt_booked"].sum())
        p_total_appts = int(e1p["appt_booked"].sum()) if not e1p.empty else 0
        blended_cpa = (spend_aud / total_appts) if total_appts else None
        p_blended_cpa = (p_spend_aud / p_total_appts) if p_total_appts else None

        kc2 = st.columns(5)
        _e1_scorecard(kc2[0], "Ad Spend", f"${spend_aud:,.0f}", f"Meta · AUD @ {fx:.2f}",
                      _delta_md(spend_aud, p_spend_aud, higher_is_better=True, fmt="pct"))
        _e1_scorecard(kc2[1], "Leads to Booking", f"{ltb*100:.0f}%", f"{cur_booked:,} of {n_leads:,} leads",
                      _delta_md(ltb, p_ltb, higher_is_better=True, fmt="pts"))
        _e1_scorecard(kc2[2], "Show Rate", f"{sr*100:.0f}%", f"{cur_showed:,} of {cur_booked:,} booked",
                      _delta_md(sr, p_sr, higher_is_better=True, fmt="pts"))
        _e1_scorecard(kc2[3], "Blended CPA", f"${blended_cpa:,.0f}" if blended_cpa else "—",
                      f"spend ÷ {total_appts:,} appts",
                      _delta_md(blended_cpa, p_blended_cpa, higher_is_better=False, fmt="pct"))
        _e1_scorecard(kc2[4], "Revenue", f"${rev_aud:,.0f}", f"GHL payments · {len(rev_cur):,} payers",
                      _delta_md(rev_aud, p_rev_aud, higher_is_better=True, fmt="pct"))

        st.caption(
            "Click any **scorecard** to open its drill-down. **Leads** = created or revived (+ booked-in), "
            "**Queries excluded**. Rates: Lead→Booking = Booked ÷ Leads; Show = Showed ÷ Booked. "
            "**Ad Spend** is Meta (USD→AUD); **Revenue** is succeeded GHL payments in the window.")

        # =============================================================
        # ANALYTICS — source trend · counsellor pie · insights · goals · funnel
        # =============================================================
        import altair as _alt
        import re as _re

        # Brand palette — 3 primary hues (blue / purple / coral-red) plus light
        # & dark variants so charts with >3 categories stay on-brand. Solid
        # lines, low-opacity fills, muted-gray axis labels (see brand spec).
        BLUE, PURPLE, RED = "#4DA6FF", "#7A52CC", "#FF4D66"
        PAL = [BLUE, PURPLE, RED, "#8AC6FF", "#A98EDB", "#FF8A99",
               "#2E7FD6", "#5A3DA6", "#D63A52"]
        AXIS_GRAY, INK = "#718096", "#1A1A1A"
        _xaxis = _alt.Axis(format="%b %d", tickCount=8, grid=False, domain=False,
                           ticks=False, labelFontSize=11, labelColor=AXIS_GRAY)
        _yaxis = _alt.Axis(grid=True, gridColor="#F0F2F5", domain=False, ticks=False,
                           labelColor=AXIS_GRAY)

        # ---- 1) Leads-by-source over time (one line per source) — Queries excluded ----
        st.markdown("---")
        st.markdown("### 📈 Leads by source — over time")
        _ts = (leads_df.assign(d=pd.to_datetime(leads_df["lead_date"]))
                 .groupby([pd.Grouper(key="d", freq="D"), "refined_source"])
                 .size().reset_index(name="Leads"))
        if _ts.empty:
            st.caption("No leads to chart in this window.")
        else:
            # One line per source so each source's value reads directly off the
            # y-axis (a stacked area hid this — a band's height was its position in
            # the stack, not its lead count).
            _hover = _alt.selection_point(fields=["refined_source"], bind="legend")
            line = (_alt.Chart(_ts).mark_line(interpolate="monotone", strokeWidth=2.5,
                                              point=True)
                    .encode(
                        x=_alt.X("d:T", title=None, axis=_xaxis),
                        y=_alt.Y("Leads:Q", title=None, axis=_yaxis,
                                 scale=_alt.Scale(zero=True)),
                        color=_alt.Color("refined_source:N", title="Source",
                                         scale=_alt.Scale(range=PAL)),
                        opacity=_alt.condition(_hover, _alt.value(1.0), _alt.value(0.15)),
                        tooltip=[_alt.Tooltip("d:T", title="Date", format="%b %d"),
                                 _alt.Tooltip("refined_source:N", title="Source"),
                                 _alt.Tooltip("Leads:Q")])
                    .add_params(_hover)
                    .properties(height=320).configure_view(strokeWidth=0))
            st.altair_chart(line, use_container_width=True)
            st.caption("Each line = that source's leads per day (read straight off the "
                       "y-axis). Click a legend item to isolate a source. Queries "
                       "excluded — they have their own scorecard.")

        # ---- 2) Counsellor booking share & show rate (pie + table) ----
        st.markdown("### 🥧 Counsellor booking share & show rate")
        _cn = run_df("vw_counsellors",
                     {"since": since.isoformat(), "until": until.isoformat(), "city": city})
        _gc = pd.DataFrame()
        if _cn.empty:
            st.caption("No counsellor appointments in this window.")
        else:
            _c2name = {cid: c["name"].split(" - ")[0]
                       for c in COUNSELLORS for cid in c["calendar_ids"]}
            _cn["Counsellor"] = _cn["calendar_id"].map(_c2name).fillna("Other")
            _gc = (_cn.groupby("Counsellor")
                   .agg(Booked=("appointments", "sum"), Showed=("showed", "sum"))
                   .reset_index())
            _gc["ShowRate"] = (_gc["Showed"] / _gc["Booked"]).replace([float("inf")], 0).fillna(0)
            _gc = _gc.sort_values("Booked", ascending=False)
            cpie, ctbl = st.columns([1, 1])
            with cpie:
                pie = (_alt.Chart(_gc).mark_arc(innerRadius=72, stroke="#FFFFFF",
                                                strokeWidth=2).encode(
                    theta=_alt.Theta("Booked:Q"),
                    color=_alt.Color("Counsellor:N", scale=_alt.Scale(range=PAL),
                                     title="Counsellor"),
                    tooltip=["Counsellor:N", "Booked:Q", "Showed:Q",
                             _alt.Tooltip("ShowRate:Q", title="Show rate", format=".0%")])
                    .properties(height=300))
                st.altair_chart(pie, use_container_width=True)
            with ctbl:
                _gd = pd.DataFrame({
                    "Counsellor": _gc["Counsellor"].values,
                    "Booked": _gc["Booked"].astype(int).values,
                    "Showed": _gc["Showed"].astype(int).values,
                    "Show Rate": (_gc["ShowRate"] * 100).map(lambda v: f"{v:.0f}%").values,
                })
                st.dataframe(_gd, hide_index=True, use_container_width=True, height=300)

        # ---- 3) Auto-Insights (rule-based) ----
        st.markdown("### 💡 Auto-Insights")
        n_total = n_leads + n_queries
        conv_rate = (n_conv / n_leads) if n_leads else 0.0
        insights = []   # (level, text) — level in {warn, info, good}

        if p_ltb:
            diff = (ltb - p_ltb) * 100
            lead_chg = ((n_leads - p_leads) / p_leads * 100) if p_leads else 0
            if diff <= -2:
                if abs(lead_chg) < 10:
                    why = (f"Lead volume is roughly flat ({n_leads:,} vs {p_leads:,} last period), so the "
                           "drop points to **lead quality / speed-to-lead / follow-up** rather than volume.")
                elif lead_chg < 0:
                    why = (f"Leads fell **{abs(lead_chg):.0f}%** ({p_leads:,} → {n_leads:,}) — fewer leads is "
                           "the main driver; the rate itself held up better than the count.")
                else:
                    why = (f"Leads rose **{lead_chg:.0f}%** but bookings didn't keep pace — likely a "
                           "**capacity / response-time** bottleneck, not lead supply.")
                insights.append(("warn", f"**Booking rate down {abs(diff):.0f} pts** to {ltb*100:.0f}%. {why}"))
            elif diff >= 2:
                insights.append(("good", f"**Booking rate up {diff:.0f} pts** to {ltb*100:.0f}% vs last "
                                         f"period (leads {p_leads:,} → {n_leads:,}). Keep the current mix."))
            else:
                insights.append(("info", f"**Booking rate steady** at {ltb*100:.0f}% "
                                         f"(last period {p_ltb*100:.0f}%)."))

        if p_sr:
            sdiff = (sr - p_sr) * 100
            worst = ""
            if not _gc.empty:
                _w = _gc[_gc["Booked"] >= 3].sort_values("ShowRate")
                if len(_w):
                    ww = _w.iloc[0]
                    worst = (f" Largest gap on **{ww['Counsellor']}**'s calendar "
                             f"({ww['ShowRate']*100:.0f}%). Suggest reminder-SMS automation.")
            if sdiff <= -2:
                insights.append(("warn", f"**Show rate down {abs(sdiff):.0f} pts** to {sr*100:.0f}%.{worst}"))
            elif sdiff >= 2:
                insights.append(("good", f"**Show rate up {sdiff:.0f} pts** to {sr*100:.0f}%.{worst}"))

        if not src.empty and n_total:
            _top = src.iloc[0]
            insights.append(("info", f"**{_top['Source']}** drove {_top['% of Leads']*100:.0f}% of leads "
                                     f"({int(_top['Leads'])} of {int(src['Leads'].sum())}) this period — "
                                     "the largest channel."))

        _ad = run_df("vw_exec1_adspend_detail",
                     {"since": since.isoformat(), "until": until.isoformat()})
        if not _ad.empty and _ad["leads"].fillna(0).sum() > 0:
            _a = _ad.copy()
            _a["cpa"] = (_a["spend"] * fx) / _a["leads"].replace(0, float("nan"))
            best = _a.sort_values("leads", ascending=False).iloc[0]
            cpa_cmp = ""
            if blended_cpa and pd.notna(best["cpa"]):
                cheaper = best["cpa"] < blended_cpa
                cpa_cmp = (f" Its ${best['cpa']:,.0f}/lead is **{'below' if cheaper else 'above'}** the "
                           f"blended ${blended_cpa:,.0f}/appointment — {'scale it' if cheaper else 'watch efficiency'}.")
            insights.append(("info", f"Top ad by volume: **{str(best['campaign_name'])[:44]}** — "
                                     f"{int(best['leads'])} Meta leads on ${best['spend']*fx:,.0f} spend.{cpa_cmp}"))

        if n_total:
            qshare = n_queries / n_total
            if qshare > 0.35:
                insights.append(("warn", f"**{qshare*100:.0f}% of contacts are Queries** ({n_queries:,} of "
                                         f"{n_total:,}) — a large untracked top-of-funnel (DMs with no form / "
                                         "no contact info). Tighten lead capture to convert these."))

        _ICL = {"warn": ("rgba(255,77,102,0.10)", RED),
                "info": ("rgba(122,82,204,0.10)", PURPLE),
                "good": ("rgba(77,166,255,0.12)", BLUE)}
        if not insights:
            st.caption("No notable changes vs last period.")
        for lvl, txt in insights:
            bg, br = _ICL.get(lvl, _ICL["info"])
            html = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", txt)
            st.markdown(
                f"<div style='background:{bg};border-left:4px solid {br};border-radius:8px;"
                f"padding:10px 14px;margin-bottom:8px;color:#1f2937;font-size:14px;'>{html}</div>",
                unsafe_allow_html=True)

        # ---- 4) Goal Progress + Set Targets (with historical suggestion) ----
        st.markdown("### 🎯 Goal Progress")
        sug_leads = int(round(max(n_leads, p_leads, 1) * 1.16))
        sug_book  = int(round(max(ltb, p_ltb, 0.20) * 100))
        sug_conv  = int(round(max(n_conv, p_conv, 1) * 1.16))
        for _k, _v in (("e1_goal_leads", sug_leads), ("e1_goal_booking", sug_book),
                       ("e1_goal_conv", sug_conv)):
            if _k not in st.session_state:
                st.session_state[_k] = _v

        with st.expander("⚙️ Set targets (defaults = +16% on the better of this/last period)"):
            g1, g2, g3 = st.columns(3)
            st.session_state["e1_goal_leads"] = g1.number_input(
                "Leads Target", min_value=0, step=10,
                value=int(st.session_state["e1_goal_leads"]), key="e1_ni_leads")
            st.session_state["e1_goal_booking"] = g2.number_input(
                "Booking Rate Target (%)", min_value=0, max_value=100, step=1,
                value=int(st.session_state["e1_goal_booking"]), key="e1_ni_booking")
            st.session_state["e1_goal_conv"] = g3.number_input(
                "Conversion Target", min_value=0, step=5,
                value=int(st.session_state["e1_goal_conv"]), key="e1_ni_conv")

        t_leads = int(st.session_state["e1_goal_leads"])
        t_book  = int(st.session_state["e1_goal_booking"])
        t_conv  = int(st.session_state["e1_goal_conv"])
        req_leads = int(round(t_conv / conv_rate)) if conv_rate else None
        _need = (f"need ~**{req_leads:,} leads** at your current {conv_rate*100:.1f}% lead→conversion rate"
                 if req_leads else "raise lead volume and/or booking rate")
        st.caption(f"To hit **{t_conv} conversions** (~16% over last period's {p_conv}), you'd {_need} — "
                   f"or lift booking rate toward **{t_book}%**.")

        def _goal_bar(label, current, target, color, is_pct=False):
            pct = (current / target) if target else 0
            w = min(1.0, max(0.0, pct))
            cur_s = f"{current:.0f}%" if is_pct else f"{current:,.0f}"
            tgt_s = f"{target:.0f}%" if is_pct else f"{target:,.0f}"
            hint = "" if pct >= 0.6 else " <span style='color:#FF4D66;'>· behind pace</span>"
            st.markdown(
                f"<div style='margin:2px 0 4px;font-weight:600;color:#111;'>{label} "
                f"<span style='color:#6b7280;font-weight:500;'>— {cur_s} / {tgt_s} "
                f"({pct*100:.0f}%)</span>{hint}</div>"
                f"<div style='background:#eef0f2;border-radius:6px;height:13px;margin-bottom:12px;'>"
                f"<div style='width:{w*100:.0f}%;background:{color};height:13px;border-radius:6px;'></div></div>",
                unsafe_allow_html=True)

        gb1, gb2, gb3 = st.columns(3)
        with gb1:
            _goal_bar("Leads", n_leads, t_leads, PAL[0])
        with gb2:
            _goal_bar("Booking Rate", ltb * 100, t_book, PAL[1], is_pct=True)
        with gb3:
            _goal_bar("Conversions", n_conv, t_conv, PAL[2])

        # ---- 5) Funnel — Leads → Booked → Showed → Conversions ----
        st.markdown("### 🔻 Conversion funnel")
        _fun = pd.DataFrame({
            "Stage": ["Leads", "Booked", "Showed", "Conversions"],
            "Count": [n_leads, cur_booked, cur_showed, n_conv],
        })
        _fun["Pct"] = _fun["Count"] / (n_leads if n_leads else 1)
        _fun["Label"] = _fun.apply(lambda r: f"{int(r['Count']):,}  ({r['Pct']*100:.0f}%)", axis=1)
        _order = ["Leads", "Booked", "Showed", "Conversions"]
        _base = _alt.Chart(_fun).encode(
            y=_alt.Y("Stage:N", sort=_order, title=None,
                     axis=_alt.Axis(labelFontSize=13, domain=False, ticks=False)),
            x=_alt.X("Count:Q", title=None, axis=_alt.Axis(grid=False, labels=False, ticks=False)))
        # Funnel: top-of-funnel coral red -> purple -> blue -> deep-blue conversion.
        _bars = _base.mark_bar(height=34, cornerRadius=6).encode(
            color=_alt.Color("Stage:N", sort=_order, legend=None,
                             scale=_alt.Scale(domain=_order,
                                              range=[RED, PURPLE, BLUE, "#2E7FD6"])),
            tooltip=["Stage:N", "Count:Q", _alt.Tooltip("Pct:Q", format=".0%")])
        _txt = _base.mark_text(align="left", dx=6, fontSize=13, fontWeight="bold",
                               color=INK).encode(text="Label:N")
        st.altair_chart((_bars + _txt).properties(height=240).configure_view(strokeWidth=0),
                        use_container_width=True)
        st.caption("Funnel uses the same definitions as the scorecards: **Leads** (created/revived, "
                   "excl. Queries) → **Booked** → **Showed** → **Conversions** (COE/Initial Received or Won).")


# =====================================================================
# FUNNELS_1 TAB — contacts created · opportunities (+status) · paid consults
# =====================================================================
with tab_funnels1:
    st.markdown(
        "<div class='panel-title'>Funnels_1 — contacts · opportunities · paid consultations"
        "<span class='hint'>selected range (AEST)</span></div>", unsafe_allow_html=True)
    _con = get_con()
    _s, _u = since.isoformat(), until.isoformat()
    _ps, _pu = prior_since.isoformat(), prior_until.isoformat()

    def _f1_contacts(s, u):
        return _con.execute(
            "SELECT COUNT(*) FROM fact_contacts "
            "WHERE CAST(date_added + INTERVAL 10 HOUR AS DATE) BETWEEN ? AND ? "
            "AND LOWER(TRIM(COALESCE(contact_name,''))) NOT IN ('insta user','insta ai')",
            [s, u]).fetchone()[0]

    def _f1_opps(s, u):
        d = _con.execute(
            "SELECT LOWER(COALESCE(status,'open')) st, COUNT(*) n FROM fact_opportunities "
            "WHERE CAST(created_at + INTERVAL 10 HOUR AS DATE) BETWEEN ? AND ? GROUP BY 1",
            [s, u]).fetchdf()
        return dict(zip(d["st"], d["n"]))

    c_contacts, p_contacts = _f1_contacts(_s, _u), _f1_contacts(_ps, _pu)
    osm, p_osm = _f1_opps(_s, _u), _f1_opps(_ps, _pu)
    n_opps, p_opps = int(sum(osm.values())), int(sum(p_osm.values()))
    n_open = int(osm.get("open", 0)); n_won = int(osm.get("won", 0))
    n_lost = int(osm.get("lost", 0)); n_aband = int(osm.get("abandoned", 0))

    # ---- Paid consultations: Stripe succeeded charges for contacts who booked
    #      a PAID calendar (Gurbir / Nasir / Turab) ----
    import sys as _sys3
    from pathlib import Path as _P3
    _sys3.path.insert(0, str(_P3(__file__).resolve().parent))
    import stripe_revenue as _srev
    _paid_cals = [cid for c in COUNSELLORS if c.get("is_paid") for cid in c["calendar_ids"]]
    _phh = ",".join(["?"] * len(_paid_cals))
    _booked = set(_con.execute(
        f"SELECT DISTINCT contact_id FROM fact_appointments WHERE calendar_id IN ({_phh}) "
        "AND LOWER(COALESCE(appointment_status,'')) <> 'invalid' AND contact_id IS NOT NULL",
        _paid_cals).fetchdf()["contact_id"])
    pc_count, pc_total, pc_count_p = 0, 0.0, 0
    # Canonical paid consultations (shared with the Counsellors tab so both tabs
    # agree): a Stripe charge matched to a non-follow-up paid-calendar appointment.
    paid_df = paid_consults_detail(_s, _u)
    _pcd_p  = paid_consults_detail(_ps, _pu)
    _stripe_on = False
    try:
        _stripe_on = _srev.enabled()
    except Exception:
        _stripe_on = False
    if not paid_df.empty:
        pc_count = len(paid_df)
        pc_total = float(paid_df["net"].sum())
    pc_count_p = len(_pcd_p)

    def _f1d(c, p):
        if not p:
            return ("", "#9aa0a6")
        pct = (c - p) / p * 100
        if abs(pct) < 0.5:
            return ("— vs last", "#9aa0a6")
        up = pct > 0
        return (f"{'▲' if up else '▼'} {abs(pct):.0f}% vs last", "#15803d" if up else "#dc2626")

    # ---- cohort opportunities (created + revived) — drives the funnel and the
    #      Booking-Link-Shared / Post-Consultation cards ----
    _so = _con.execute(
        "SELECT p.pipeline_name pn, s.stage_name sn, s.stage_order so "
        "FROM dim_stages s JOIN dim_pipelines p ON p.pipeline_id = s.pipeline_id").fetchdf()
    _ordm = {(r.pn, r.sn): r.so for r in _so.itertuples()}
    fopps = _con.execute(
        "WITH subs AS ("
        "  SELECT contact_id, MAX(submitted_at) ms FROM ("
        "    SELECT contact_id, submitted_at FROM fact_form_submissions WHERE contact_id IS NOT NULL"
        "    UNION ALL SELECT contact_id, submitted_at FROM fact_survey_submissions WHERE contact_id IS NOT NULL"
        "  ) GROUP BY 1),"
        " revived AS (SELECT contact_id FROM subs "
        "             WHERE CAST(ms + INTERVAL 10 HOUR AS DATE) BETWEEN ? AND ?) "
        "SELECT o.contact_id, p.pipeline_name pn, s.stage_name sn, s.stage_order so, "
        "COALESCE(o.status,'open') status, "
        "date_diff('day', CAST(o.updated_at + INTERVAL 10 HOUR AS DATE), CURRENT_DATE) days_in_stage "
        "FROM fact_opportunities o "
        "JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id "
        "JOIN dim_stages s ON s.stage_id = o.stage_id "
        "WHERE CAST(o.created_at + INTERVAL 10 HOUR AS DATE) BETWEEN ? AND ? "
        "   OR o.contact_id IN (SELECT contact_id FROM revived)",
        [_s, _u, _s, _u]).fetchdf()

    def _olvl(r):
        pn, sn, stt, so = r["pn"], r["sn"], str(r["status"]).lower(), r["so"]
        B = 10 ** 9; g = _ordm.get
        if stt == "won":
            return 6
        if pn in ("L2C - Education", "CLT - Onshore Admission") and so >= g((pn, "COE Received"), B):
            return 6
        if pn == "CLT - VISA" and so >= g((pn, "Application Submitted"), B):
            return 6
        if pn == "CLT - Onshore Admission" and so >= g((pn, "COE Payment Received"), B):
            return 5
        if pn == "CLT - VISA" and so >= g((pn, "Payment Received"), B):
            return 5
        if pn == "L2C - Education" and so >= g((pn, "Appointment Booked"), B):
            return 4
        if pn == "L2C - VISA" and so >= g((pn, "MARA Appointment Booked"), B):
            return 4
        if pn == "L2C - Education" and so >= g((pn, "Pre Sales (1)"), B):
            return 3
        if pn == "L2C - Education" and so >= g((pn, "Qualifier"), B):
            return 2
        return 1

    bls_count, bls_mara, postc_contacts, postc_avg = 0, 0, 0, 0.0
    _ll = pd.Series(dtype="int64")
    _blsids, _postcids = set(), set()
    _pc = pd.DataFrame()
    if not fopps.empty:
        fopps["lvl"] = fopps.apply(_olvl, axis=1)
        _blso = _ordm.get(("L2C - Education", "Booking Link Shared"), 10 ** 9)
        _marao = _ordm.get(("L2C - VISA", "MARA Appointment Booked"), 10 ** 9)
        fopps["_bls"] = ((fopps["pn"] == "L2C - Education") & (fopps["so"] >= _blso)).astype(int)
        fopps["_mara"] = ((fopps["pn"] == "L2C - VISA") & (fopps["so"] >= _marao)).astype(int)
        _g = fopps.groupby("contact_id")
        _ll = _g["lvl"].max()
        _blsids = set(_g["_bls"].max().loc[lambda s: s > 0].index)
        _maraids = set(_g["_mara"].max().loc[lambda s: s > 0].index)
        bls_count = len(_blsids)
        bls_mara = len(_blsids & _maraids)
        _pc = fopps[fopps["sn"] == "Post Consultation"]
        _postcids = set(_pc["contact_id"])
        postc_contacts = int(_pc["contact_id"].nunique())
        postc_avg = float(_pc["days_in_stage"].mean()) if len(_pc) else 0.0

    # ---- detail tables (for the clickable scorecards) + country (by phone) ----
    def _f1_country(ph):
        s = str(ph or "").replace(" ", "").replace("-", "")
        # the two most common non-Australian origins in the data (Pakistan, India)
        # get their own buttons; everything else rolls up into "Other".
        if s.startswith("+92") or s.startswith("0092"):
            return "Pakistan"
        if s.startswith("+91") or s.startswith("0091"):
            return "India"
        if s.startswith("+61") or s.startswith("0061") or s.startswith("0"):
            return "Australia"
        return "Other"

    _e1f = run_df("vw_exec1_lead_detail", {"since": _s, "until": _u})

    def _f1fmt(df):
        cols = ["Email", "Contact Created Date", "Pipeline", "Stage", "Status", "Appointment",
                "Calendar", "Source", "Name", "Phone", "Country"]
        if df.empty:
            return pd.DataFrame(columns=cols)
        dd = df.copy()
        appt = dd.apply(lambda r: "Showed" if r.get("appt_showed") == 1
                        else ("Booked" if r.get("appt_booked") == 1 else "—"), axis=1)
        cal = dd.apply(lambda r: r["calendar_name"]
                       if (r.get("appt_booked") == 1 and pd.notna(r.get("calendar_name"))) else "—", axis=1)
        return pd.DataFrame({
            "Email": dd["email"].fillna("(no email)").values,
            "Contact Created Date": pd.to_datetime(dd["lead_date"]).dt.strftime("%Y-%m-%d").values,
            "Pipeline": dd["pipeline"].fillna("—").values,
            "Stage": dd["stage"].fillna("—").values,
            "Status": dd["status"].fillna("—").values,
            "Appointment": appt.values,
            "Calendar": cal.values,
            "Source": dd["refined_source"].values,
            "Name": dd["contact_name"].fillna("—").replace("", "—").values,
            "Phone": dd["phone"].fillna("—").replace("", "—").values,
            "Country": dd["phone"].map(_f1_country).values,
        })

    def _f1fmt_pc(opp):
        """Post-Consultation detail keyed on the opportunity that is actually in
        the 'Post Consultation' stage — so Pipeline / Stage reflect that opp, not
        the contact's representative opportunity (which may be e.g. 'MARA
        Appointment Booked' from a different L2C-VISA opportunity)."""
        cols = ["Email", "Contact Created Date", "Pipeline", "Stage", "Status", "Days in Stage",
                "Appointment", "Calendar", "Source", "Name", "Phone", "Country"]
        if opp.empty or _e1f.empty:
            return pd.DataFrame(columns=cols)
        _em = dict(zip(_e1f["contact_id"], _e1f["email"]))
        _nm = dict(zip(_e1f["contact_id"], _e1f["contact_name"]))
        _phm = dict(zip(_e1f["contact_id"], _e1f["phone"]))
        _srcm = dict(zip(_e1f["contact_id"], _e1f["refined_source"]))
        _ldm = dict(zip(_e1f["contact_id"], _e1f["lead_date"]))
        _abm = dict(zip(_e1f["contact_id"], _e1f["appt_booked"]))
        _ashm = dict(zip(_e1f["contact_id"], _e1f["appt_showed"]))
        _calm = dict(zip(_e1f["contact_id"], _e1f["calendar_name"]))
        rows = []
        for r in opp.itertuples():
            cid = r.contact_id
            ab, ash = _abm.get(cid), _ashm.get(cid)
            appt = "Showed" if ash == 1 else ("Booked" if ab == 1 else "—")
            caln = _calm.get(cid)
            ld, ph, nm = _ldm.get(cid), _phm.get(cid), _nm.get(cid)
            rows.append({
                "Email": _em.get(cid) or "(no email)",
                "Contact Created Date": pd.to_datetime(ld).strftime("%Y-%m-%d") if pd.notna(ld) else "—",
                "Pipeline": r.pn, "Stage": r.sn, "Status": r.status,
                "Days in Stage": int(r.days_in_stage) if pd.notna(r.days_in_stage) else "—",
                "Appointment": appt,
                "Calendar": caln if (ab == 1 and pd.notna(caln)) else "—",
                "Source": _srcm.get(cid, "—"),
                "Name": nm if (nm and str(nm).strip()) else "—",
                "Phone": ph if (ph and str(ph).strip()) else "—",
                "Country": _f1_country(ph),
            })
        return pd.DataFrame(rows, columns=cols)

    cc_det = _f1fmt(_e1f[_e1f["is_created"] == 1]) if not _e1f.empty else pd.DataFrame()
    bls_det = _f1fmt(_e1f[_e1f["contact_id"].isin(_blsids)]) if (not _e1f.empty and _blsids) else _f1fmt(pd.DataFrame())
    # one row per contact (the Post-Consultation opp with the longest current dwell)
    _pcu = (_pc.sort_values("days_in_stage", ascending=False).drop_duplicates("contact_id")
            if not _pc.empty else _pc)
    postc_det = _f1fmt_pc(_pcu)

    # opportunity-level detail (one row per opp created in range)
    _srcmap = dict(zip(_e1f["contact_id"], _e1f["refined_source"])) if not _e1f.empty else {}
    _calmap = {}
    if not _e1f.empty:
        for _r in _e1f.itertuples():
            if getattr(_r, "appt_booked", 0) == 1 and pd.notna(getattr(_r, "calendar_name", None)):
                _calmap[_r.contact_id] = _r.calendar_name
    _oc = _con.execute(
        "SELECT o.contact_id, c.email, c.contact_name, c.phone, "
        "CAST(c.date_added + INTERVAL 10 HOUR AS DATE) cdate, "
        "p.pipeline_name pn, s.stage_name sn, COALESCE(o.status,'open') status "
        "FROM fact_opportunities o JOIN fact_contacts c ON c.contact_id = o.contact_id "
        "JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id "
        "JOIN dim_stages s ON s.stage_id = o.stage_id "
        "WHERE CAST(o.created_at + INTERVAL 10 HOUR AS DATE) BETWEEN ? AND ? "
        "AND LOWER(TRIM(COALESCE(c.contact_name,''))) NOT IN ('insta user','insta ai')",
        [_s, _u]).fetchdf()
    if _oc.empty:
        oc_det = pd.DataFrame(columns=["Email", "Contact Created Date", "Pipeline", "Stage", "Status",
                                       "Calendar", "Source", "Name", "Phone", "Country"])
    else:
        oc_det = pd.DataFrame({
            "Email": _oc["email"].fillna("(no email)").values,
            "Contact Created Date": _oc["cdate"].astype(str).values,
            "Pipeline": _oc["pn"].fillna("—").values,
            "Stage": _oc["sn"].fillna("—").values,
            "Status": _oc["status"].fillna("—").values,
            "Calendar": _oc["contact_id"].map(lambda c: _calmap.get(c, "—")).values,
            "Source": _oc["contact_id"].map(lambda c: _srcmap.get(c, "—")).values,
            "Name": _oc["contact_name"].fillna("—").replace("", "—").values,
            "Phone": _oc["phone"].fillna("—").replace("", "—").values,
            "Country": _oc["phone"].map(_f1_country).values,
        })

    # paid-consultations detail (+ stripe payment date, appt created & scheduled)
    pc_det = pd.DataFrame(columns=["Email", "Name", "Counsellor", "Amount", "Stripe Payment Date",
                                   "Appt Created Date", "Appt Scheduled Date", "Phone", "Country"])
    if not paid_df.empty:
        _ai = _con.execute(
            f"SELECT contact_id, MIN(CAST(date_added + INTERVAL 10 HOUR AS DATE)) ac, "
            f"MIN(CAST(start_time AS DATE)) asch FROM fact_appointments "
            f"WHERE calendar_id IN ({_phh}) AND contact_id IS NOT NULL "
            "AND LOWER(COALESCE(appointment_status,'')) <> 'invalid' GROUP BY 1", _paid_cals).fetchdf()
        _acm = dict(zip(_ai["contact_id"], _ai["ac"])); _asm = dict(zip(_ai["contact_id"], _ai["asch"]))
        _ids2 = paid_df["contact_id"].dropna().tolist()
        _em2 = (_con.execute(
            f"SELECT contact_id, email, contact_name, phone FROM fact_contacts "
            f"WHERE contact_id IN ({','.join(['?']*len(_ids2))})", _ids2).fetchdf()
            if _ids2 else pd.DataFrame(columns=["contact_id", "email", "contact_name", "phone"]))
        _em2m = dict(zip(_em2["contact_id"], _em2["email"]))
        _nm2 = dict(zip(_em2["contact_id"], _em2["contact_name"]))
        _pm2 = dict(zip(_em2["contact_id"], _em2["phone"]))
        pc_det = pd.DataFrame({
            "Email": paid_df["contact_id"].map(_em2m).fillna("—").values,
            "Name": paid_df["contact_id"].map(_nm2).fillna("—").values,
            "Counsellor": paid_df["counsellor"].values,
            "Amount": paid_df["amount"].map(lambda v: f"${v:,.0f}").values,
            "Stripe Payment Date": paid_df["created_date"].astype(str).values,
            "Appt Created Date": paid_df["contact_id"].map(lambda c: str(_acm.get(c, "—"))).values,
            "Appt Scheduled Date": paid_df["contact_id"].map(lambda c: str(_asm.get(c, "—"))).values,
            "Phone": paid_df["contact_id"].map(lambda c: _pm2.get(c) or "—").values,
            "Country": paid_df["contact_id"].map(lambda c: _f1_country(_pm2.get(c))).values,
        })

    _F1MAP = {"Contacts Created": cc_det, "Opportunities Created": oc_det,
              "Opportunity Status": oc_det, "Paid Consultations": pc_det,
              "Booking Link Shared": bls_det, "In Post Consultation": postc_det}

    @st.dialog(" ", width="large")
    def _f1_modal():
        card = st.session_state.get("f1_card", "Contacts Created")
        st.markdown(f"### {card} — Drill Down")
        if "f1_country" not in st.session_state:
            st.session_state["f1_country"] = "All"
        country = st.segmented_control(
            "Country (by phone)", ["All", "Australia", "Pakistan", "India", "Other"],
            key="f1_country") or "All"
        df = _F1MAP.get(card, pd.DataFrame())
        if "Country" in df.columns and country != "All":
            df = df[df["Country"] == country]
        st.markdown(f"**{len(df):,} rows**")
        if df.empty:
            st.caption("No rows for this selection.")
        else:
            st.dataframe(df, hide_index=True, use_container_width=True, height=440)
            st.download_button("Download (CSV)", df.to_csv(index=False).encode("utf-8"),
                               file_name=f"funnels1_{card.replace(' ', '_')}_{_s}_{_u}.csv",
                               mime="text/csv", key="f1_modal_dl")

    def _f1sc(col, name, value, sub):
        lines = [name.upper(), value] + ([sub] if sub else [])
        with col:
            if st.button("\n\n".join(lines), key=f"f1sc_{name}", use_container_width=True):
                st.session_state["f1_card"] = name
                _f1_modal()

    fc = st.columns(4)
    _f1sc(fc[0], "Contacts Created", f"{c_contacts:,}", "new GHL contacts")
    _f1sc(fc[1], "Opportunities Created", f"{n_opps:,}", "new opportunities")
    _f1sc(fc[2], "Opportunity Status", f"{n_opps:,}",
          f"Open {n_open} · Won {n_won} · Lost {n_lost} · Aband {n_aband}")
    _f1sc(fc[3], "Paid Consultations", f"{pc_count:,}", f"${pc_total:,.0f} · Gurbir/Nasir/Turab")

    fc2 = st.columns(4)
    _f1sc(fc2[0], "Booking Link Shared", f"{bls_count:,}", f"{bls_mara:,} → MARA Booked")
    _f1sc(fc2[1], "In Post Consultation", f"{postc_contacts:,}", f"avg {postc_avg:.0f}d in stage")
    with fc2[2]:
        st.write("")
    with fc2[3]:
        st.write("")

    if not _stripe_on:
        st.info("Add **STRIPE_RESTRICTED_KEY** to `.env` to populate Paid Consultations.")
    st.caption("Click any scorecard for its contacts. **Country** is inferred from the phone prefix "
               "(+61 / 0 → Australia, +92 → Pakistan, +91 → India — the two most common origins; "
               "everything else → Other). **Opportunities Created** & **Opportunity Status** "
               "are opportunity-level; the rest are contact-level.")

    # ===== Opportunity funnel (created + revived) =====
    import altair as _alt
    st.markdown("---")
    st.markdown("### 🔻 Opportunity funnel — created + revived")
    if fopps.empty or _ll.empty:
        st.caption("No created/revived opportunities in this window.")
    else:
        n1 = int(_ll.size)
        _ncnt = [n1] + [int((_ll >= L).sum()) for L in (2, 3, 4, 5, 6)]
        _stages = ["Opportunities", "Qualifier", "Pre Sales (1+2)", "Booked",
                   "Payment", "Won / COE / Submitted"]
        _fun = pd.DataFrame({"Stage": _stages, "Count": _ncnt})
        _fun["Pct"] = _fun["Count"] / (n1 if n1 else 1)
        _fun["Label"] = _fun.apply(lambda r: f"{int(r['Count']):,}  ({r['Pct']*100:.0f}%)", axis=1)
        _b = _alt.Chart(_fun).encode(
            y=_alt.Y("Stage:N", sort=_stages, title=None,
                     axis=_alt.Axis(labelFontSize=13, domain=False, ticks=False)),
            x=_alt.X("Count:Q", title=None, axis=_alt.Axis(grid=False, labels=False, ticks=False)))
        _bars = _b.mark_bar(height=32, cornerRadius=6).encode(
            color=_alt.Color("Stage:N", sort=_stages, legend=None,
                             scale=_alt.Scale(domain=_stages,
                                              range=["#4DA6FF", "#7A52CC", "#9b8bf0",
                                                     "#FF4D66", "#f6995c", "#2E7FD6"])),
            tooltip=["Stage:N", "Count:Q", _alt.Tooltip("Pct:Q", format=".0%")])
        _txt = _b.mark_text(align="left", dx=6, fontSize=13, fontWeight="bold",
                            color="#1A1A1A").encode(text="Label:N")
        st.altair_chart((_bars + _txt).properties(height=290).configure_view(strokeWidth=0),
                        use_container_width=True)
        st.caption(
            "Distinct **leads** (created/revived opps), by furthest milestone. **Qualifier** = L2C-Edu ≥ "
            "*Qualifier* · **Pre Sales (1+2)** = L2C-Edu ≥ *Pre Sales (1)* · **Booked** = L2C-Edu ≥ "
            "*Appointment Booked* or L2C-VISA ≥ *MARA Appointment Booked* · **Payment** = CLT-Onshore ≥ "
            "*COE Payment Received* or CLT-VISA ≥ *Payment Received* · **Won / COE / Submitted** = *Won*, "
            "*COE Received* (L2C-Edu / CLT-Onshore), or CLT-VISA ≥ *Application Submitted*.")

    # ===== Avg days in stage — by pipeline =====
    st.markdown("### ⏳ Avg days in stage — by pipeline")
    _PIPES = ["L2C - Education", "L2C - VISA", "CLT - Onshore Admission",
              "CLT - Admissions Sub-Applications", "CLT - VISA"]
    _phq = ",".join(["?"] * len(_PIPES))
    _dd = _con.execute(
        "SELECT p.pipeline_name pn, s.stage_name sn, s.stage_order so, "
        "AVG(date_diff('day', CAST(o.updated_at + INTERVAL 10 HOUR AS DATE), CURRENT_DATE)) avg_days, "
        "COUNT(*) n FROM fact_opportunities o "
        "JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id "
        "JOIN dim_stages s ON s.stage_id = o.stage_id "
        f"WHERE p.pipeline_name IN ({_phq}) GROUP BY 1, 2, 3 HAVING COUNT(*) > 0", _PIPES).fetchdf()
    if _dd.empty:
        st.caption("No opportunities in these pipelines.")
    else:
        _SHORT = {"L2C - Education": "Education", "L2C - VISA": "L2C-VISA",
                  "CLT - Onshore Admission": "Onshore Admission",
                  "CLT - Admissions Sub-Applications": "Sub-Applications", "CLT - VISA": "CLT-VISA"}
        _dd["label"] = _dd["pn"].map(_SHORT) + " · " + _dd["sn"]
        _dd["pord"] = _dd["pn"].map({p: i for i, p in enumerate(_PIPES)})
        _dd = _dd.sort_values(["pord", "so"]).reset_index(drop=True)
        _ysort = _dd["label"].tolist()
        _dch = (_alt.Chart(_dd).mark_bar(cornerRadius=2).encode(
            y=_alt.Y("label:N", sort=_ysort, title=None,
                     axis=_alt.Axis(labelFontSize=11, labelLimit=280, domain=False, ticks=False)),
            x=_alt.X("avg_days:Q", title="Avg days in current stage",
                     axis=_alt.Axis(grid=True, gridColor="#F0F2F5")),
            color=_alt.Color("pn:N", title="Pipeline",
                             scale=_alt.Scale(domain=_PIPES,
                                              range=["#4DA6FF", "#7A52CC", "#10b981", "#f6995c", "#FF4D66"])),
            tooltip=["pn:N", "sn:N", _alt.Tooltip("avg_days:Q", title="Avg days", format=".0f"),
                     _alt.Tooltip("n:Q", title="Opps")])
            .properties(height=max(320, 20 * len(_dd))).configure_view(strokeWidth=0))
        st.altair_chart(_dch, use_container_width=True)
        st.caption("Avg **days in current stage** = today − last stage move (`updated_at`), for opportunities "
                   "**currently** in each stage. (Full stage history isn't stored, so this is current-stage "
                   "dwell time, not historical time-in-stage.)")
