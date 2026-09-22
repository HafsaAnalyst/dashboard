"""SQL template loading + view execution.

The .sql files in dashboards/sql/ stay the single source of truth — the same
files the Streamlit app reads. DuckDB does not accept bind parameters inside
CREATE VIEW, so the files are treated as a template library: each
`CREATE OR REPLACE VIEW <name> AS <body>` is parsed out and the body is executed
directly with binds at query time.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .db import db_exec

ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "dashboards" / "sql"
SQL_FILES = ("executive_cards.sql", "counsellor_cards.sql", "tab_cards.sql")
YAML_PATH = ROOT / "dashboards" / "metrics.yaml"

_VIEW_RE = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|\Z)',
    re.DOTALL | re.IGNORECASE,
)

CITIES = ["All", "Melbourne", "Sydney", "Others", "Unidentified"]
PERIODS = ["Current month", "Last 30 days", "Last 7 days", "Custom"]


@lru_cache(maxsize=1)
def load_queries() -> dict[str, str]:
    """{view_name: SELECT body}. Cached for the life of the function instance —
    the .sql files only change on redeploy, which replaces the instance."""
    text = "\n".join(
        (SQL_DIR / f).read_text(encoding="utf-8") for f in SQL_FILES
    )
    return {m.group(1): m.group(2).strip().rstrip(';') for m in _VIEW_RE.finditer(text)}


def resolve_period(label: str, since: str | None = None, until: str | None = None) -> dict:
    """Return the four date binds for a period label. Mirrors resolve_period() in
    dashboards/app.py exactly, including the prior-window rule (same length,
    immediately before the selected range)."""
    today = date.today()
    if label == "Last 30 days":
        s, u = today - timedelta(days=29), today
    elif label == "Last 7 days":
        s, u = today - timedelta(days=6), today
    elif label == "Custom" and since and until:
        s, u = date.fromisoformat(since), date.fromisoformat(until)
    else:   # "Current month" and any unrecognised label
        s, u = today.replace(day=1), today
    length_days = (u - s).days
    prior_until = s - timedelta(days=1)
    prior_since = prior_until - timedelta(days=length_days)
    return {
        "since": s.isoformat(),
        "until": u.isoformat(),
        "prior_since": prior_since.isoformat(),
        "prior_until": prior_until.isoformat(),
    }


def run_df(view: str, binds: dict) -> pd.DataFrame:
    """Run a templated view, passing ONLY the binds it actually references, so
    callers can share one binds dict across views that use different subsets
    without DuckDB complaining about excess parameters."""
    body = load_queries().get(view)
    if body is None:
        raise KeyError(f"Unknown view: {view}")
    needed = {k: v for k, v in binds.items() if ("$" + k) in body}
    df = db_exec(body, needed).fetchdf()
    return df if df is not None else pd.DataFrame()


def run_view(view: str, binds: dict) -> dict:
    """Run a scorecard view and return {'current': {...}, 'prior': {...}}.
    Scorecard views return one row per tag; the UI computes the delta."""
    df = run_df(view, binds)
    if df.empty or "tag" not in df.columns:
        return {}
    return {row["tag"]: _clean(row.to_dict()) for _, row in df.iterrows()}


def _clean(d: dict) -> dict:
    """Coerce numpy/pandas scalars to JSON-safe Python values."""
    out = {}
    for k, v in d.items():
        if v is None or (isinstance(v, float) and pd.isna(v)):
            out[k] = None
        elif isinstance(v, (datetime, date, pd.Timestamp)):
            out[k] = v.isoformat()
        elif hasattr(v, "item"):        # numpy scalar
            out[k] = None if pd.isna(v) else v.item()
        else:
            out[k] = v
    return out


def df_to_payload(df: pd.DataFrame) -> dict:
    """DataFrame -> {columns, rows} with NaN/NaT as null and dates as ISO
    strings. Column order is preserved so the UI renders tables in view order."""
    if df is None or df.empty:
        return {"columns": list(df.columns) if df is not None else [], "rows": []}
    safe = df.copy()
    for col in safe.columns:
        if pd.api.types.is_datetime64_any_dtype(safe[col]):
            safe[col] = safe[col].dt.strftime("%Y-%m-%dT%H:%M:%S").where(safe[col].notna(), None)
    safe = safe.astype(object).where(pd.notna(safe), None)
    return {"columns": list(safe.columns), "rows": safe.to_dict(orient="records")}
