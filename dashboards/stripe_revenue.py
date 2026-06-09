"""
Stripe revenue data layer for the dashboard (read-only, live).

Prefers STRIPE_RESTRICTED_KEY (read-only restricted key); falls back to
STRIPE_SECRET_KEY. All calls are GET/list — this module never writes to Stripe.

balance_transactions is the source of truth: each row carries gross `amount`,
Stripe `fee`, and `net` already computed, plus a `reporting_category`
(charge / refund / dispute / payout / ...). The counsellor name is parsed from
the transaction description (mirrors the GHL payment source names).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, date, timedelta

import pandas as pd
import requests
import streamlit as st

BASE = "https://api.stripe.com/v1"


def _key() -> str | None:
    return os.getenv("STRIPE_RESTRICTED_KEY") or os.getenv("STRIPE_SECRET_KEY")


def key_kind() -> str:
    k = _key() or ""
    if k.startswith("rk_"):
        return "restricted (read-only)"
    if k.startswith("sk_live"):
        return "live secret"
    if k.startswith("sk_test"):
        return "test secret"
    return "none"


def enabled() -> bool:
    return bool(_key())


def parse_counsellor(desc: str) -> str:
    """Counsellor name from a charge/refund description, else a catch-all."""
    d = (desc or "").strip()
    if d.upper().startswith("REFUND FOR CHARGE (") and "(" in d:
        d = d[d.find("(") + 1:]
    low = d.lower()
    if (not d or d.upper().startswith("STRIPE PAYMENT") or "auto-recharge" in low
            or "invoice" in low or "subscription" in low or "wallet" in low):
        return "Other / GHL"
    if " - " in d:
        return d.split(" - ")[0].strip()
    return "Other"


def counsellor_city(name: str) -> str:
    n = (name or "").lower()
    if "gurbir" in n or "navneet" in n:
        return "Melbourne"
    if name in ("Other / GHL", "Other", "", None):
        return "-"
    return "Sydney"


@st.cache_data(ttl=600, show_spinner="Loading Stripe transactions…")
def fetch_balance_transactions(since_iso: str, until_iso: str) -> pd.DataFrame:
    """All balance transactions created in [since, until] (AEST-aware window).
    Returns a tidy DataFrame; empty if no key or no data. Raises on API error."""
    key = _key()
    if not key:
        return pd.DataFrame()
    # Window in UTC unix (pad the end of day; created is UTC)
    since_unix = int(datetime.fromisoformat(since_iso).replace(tzinfo=timezone.utc).timestamp())
    until_unix = int(datetime.fromisoformat(until_iso).replace(tzinfo=timezone.utc).timestamp()) + 86399
    headers = {"Authorization": f"Bearer {key}"}
    params = {"limit": 100, "created[gte]": since_unix, "created[lte]": until_unix}
    rows: list[dict] = []
    starting_after = None
    for _ in range(300):  # safety cap = 30k transactions
        p = dict(params)
        if starting_after:
            p["starting_after"] = starting_after
        r = requests.get(f"{BASE}/balance_transactions", params=p, headers=headers, timeout=60)
        if not r.ok:
            raise RuntimeError(f"Stripe API {r.status_code}: {r.text[:200]}")
        body = r.json()
        data = body.get("data", [])
        rows.extend(data)
        if not body.get("has_more") or not data:
            break
        starting_after = data[-1]["id"]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "id": b["id"],
        "type": b["type"],
        "category": b.get("reporting_category"),
        "amount": b["amount"] / 100.0,
        "fee": b["fee"] / 100.0,
        "net": b["net"] / 100.0,
        "currency": (b.get("currency") or "aud").upper(),
        "created_utc": datetime.fromtimestamp(b["created"], tz=timezone.utc),
        "description": b.get("description") or "",
        "source": b.get("source"),
    } for b in rows])
    # AEST calendar date
    df["date"] = (df["created_utc"] + pd.Timedelta(hours=10)).dt.date
    df["counsellor"] = df["description"].map(parse_counsellor)
    df["city"] = df["counsellor"].map(counsellor_city)
    return df


@st.cache_data(ttl=600, show_spinner="Loading Stripe charges…")
def fetch_charges(since_iso: str, until_iso: str) -> pd.DataFrame:
    """Succeeded charges created in [since, until]. Unlike balance_transactions
    these carry the counsellor (description) AND metadata.contactId, so they can
    be tied to a counsellor and a GHL contact. Returns one row per succeeded,
    non-refunded charge. Empty if no key/data; raises on API error."""
    key = _key()
    if not key:
        return pd.DataFrame()
    since_unix = int(datetime.fromisoformat(since_iso).replace(tzinfo=timezone.utc).timestamp())
    until_unix = int(datetime.fromisoformat(until_iso).replace(tzinfo=timezone.utc).timestamp()) + 86399
    headers = {"Authorization": f"Bearer {key}"}
    params = {"limit": 100, "created[gte]": since_unix, "created[lte]": until_unix}
    rows: list[dict] = []
    starting_after = None
    for _ in range(300):
        p = dict(params)
        if starting_after:
            p["starting_after"] = starting_after
        r = requests.get(f"{BASE}/charges", params=p, headers=headers, timeout=60)
        if not r.ok:
            raise RuntimeError(f"Stripe API {r.status_code}: {r.text[:200]}")
        body = r.json()
        data = body.get("data", [])
        rows.extend(data)
        if not body.get("has_more") or not data:
            break
        starting_after = data[-1]["id"]
    succ = [c for c in rows if c.get("status") == "succeeded" and c.get("paid")]
    if not succ:
        return pd.DataFrame()
    out = pd.DataFrame([{
        "id": c["id"],
        "amount": c["amount"] / 100.0,
        "net": (c["amount"] - (c.get("amount_refunded") or 0)) / 100.0,
        "created_date": (datetime.fromtimestamp(c["created"], tz=timezone.utc)
                         + timedelta(hours=10)).date(),
        "contact_id": (c.get("metadata") or {}).get("contactId"),
        "counsellor": parse_counsellor(c.get("description") or ""),
    } for c in succ])
    out["counsellor_key"] = out["counsellor"].map(
        lambda s: s.split()[0].lower() if s and s not in ("Other / GHL", "Other") else "")
    return out


def account_label() -> str:
    """Best-effort account display name (cached, one call)."""
    return _account().get("name", "Stripe")


@st.cache_data(ttl=3600, show_spinner=False)
def _account() -> dict:
    key = _key()
    if not key:
        return {}
    try:
        r = requests.get(f"{BASE}/account", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if r.ok:
            a = r.json()
            return {"name": (a.get("settings", {}).get("dashboard", {}) or {}).get("display_name")
                    or a.get("business_profile", {}).get("name") or "Stripe",
                    "id": a.get("id"), "currency": (a.get("default_currency") or "aud").upper()}
    except Exception:
        pass
    return {}
