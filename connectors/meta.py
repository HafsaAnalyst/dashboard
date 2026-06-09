"""
Meta Ads connector.

ETL invokes once per ad account (Melbourne + Sydney) and tags rows with
`_account_id` so downstream normalization can apply account_label.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BASE_URL = "https://graph.facebook.com/v19.0"
PAGE_LIMIT = 500
HTTP_TIMEOUT = 60


def _token() -> str:
    t = os.getenv("META_ACCESS_TOKEN")
    if not t:
        raise RuntimeError("META_ACCESS_TOKEN not set in .env")
    return t


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _paginate(url: str, params: dict) -> list[dict]:
    """Walk Meta's `paging.cursors.after` until exhausted."""
    out: list[dict] = []
    after = None
    while True:
        p = dict(params)
        if after:
            p["after"] = after
        r = requests.get(url, params=p, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            logger.error("Meta API HTTP %d on %s: %s",
                         r.status_code, url, r.text[:300])
            break
        body = r.json()
        rows = body.get("data", [])
        if not rows:
            break
        out.extend(rows)
        after = body.get("paging", {}).get("cursors", {}).get("after")
        if not after:
            break
    return out


def fetch_campaign_insights(
    account_id: str, since: str, until: Optional[str] = None
) -> list[dict]:
    """Campaign-level insights for one ad account. Includes ALL relevant lead
    actions so normalization can compute the canonical total_leads.

    Returns one row per campaign per (since..until) range with raw `actions`
    array preserved — normalize.py extracts `lead`, `fb_pixel_lead`,
    `fb_pixel_custom`, `messaging_*`, etc."""
    until = until or _today()
    fields = (
        "campaign_id,campaign_name,objective,spend,impressions,reach,frequency,"
        "clicks,ctr,cpc,cpm,inline_link_clicks,inline_link_click_ctr,outbound_clicks,"
        "actions,action_values,cost_per_action_type,"
        "video_thruplay_watched_actions,video_p50_watched_actions,"
        "video_p95_watched_actions"
    )
    params = {
        "access_token": _token(),
        "level": "campaign",
        "time_range": json.dumps({"since": since, "until": until}),
        "fields": fields,
        "limit": PAGE_LIMIT,
        "filtering": json.dumps([{
            "field": "campaign.effective_status",
            "operator": "IN",
            "value": ["ACTIVE", "PAUSED", "ARCHIVED", "DELETED"],
        }]),
    }
    rows = _paginate(f"{BASE_URL}/{account_id}/insights", params)
    for r in rows:
        r["_account_id"] = account_id
    logger.info("Meta campaign_insights %s [%s..%s]: %d rows",
                account_id, since, until, len(rows))
    return rows


def fetch_daily_insights(
    account_id: str, since: str, until: Optional[str] = None
) -> list[dict]:
    """Daily campaign-level insights with country breakdown.
    Used for time-series and geo charts."""
    until = until or _today()
    params = {
        "access_token": _token(),
        "level": "campaign",
        "time_increment": 1,
        "breakdowns": "country",
        "time_range": json.dumps({"since": since, "until": until}),
        "fields": "date_start,campaign_id,campaign_name,objective,spend,impressions,clicks,actions",
        "limit": PAGE_LIMIT,
    }
    rows = _paginate(f"{BASE_URL}/{account_id}/insights", params)
    for r in rows:
        r["_account_id"] = account_id
    logger.info("Meta daily_insights %s [%s..%s]: %d rows",
                account_id, since, until, len(rows))
    return rows


def fetch_adset_optimization(account_id: str) -> list[dict]:
    """Pull optimization_goal + promoted_object per ad set so the ETL can
    map a campaign to its 'Results' event when needed."""
    params = {
        "access_token": _token(),
        "fields": "campaign_id,optimization_goal,destination_type,promoted_object,status",
        "limit": PAGE_LIMIT,
    }
    rows = _paginate(f"{BASE_URL}/{account_id}/adsets", params)
    logger.info("Meta adsets %s: %d", account_id, len(rows))
    return rows


def fetch_lead_form_submissions(
    account_id: str, since: str, until: Optional[str] = None
) -> list[dict]:
    """Lead Form submissions across all ads in the account, for bridge_lead
    matching. Requires `leads_retrieval` scope on the access token; falls
    back to empty list with a warning if the scope isn't granted."""
    until = until or _today()
    # First list lead forms in the account
    forms_url = f"{BASE_URL}/{account_id}/leadgen_forms"
    forms = _paginate(forms_url, {
        "access_token": _token(), "fields": "id,name", "limit": PAGE_LIMIT,
    })
    if not forms:
        logger.warning("Meta lead_forms %s: 0 forms (scope missing or none)", account_id)
        return []
    leads: list[dict] = []
    since_unix = int(datetime.strptime(since, "%Y-%m-%d").timestamp())
    until_unix = int(datetime.strptime(until, "%Y-%m-%d").timestamp()) + 86399
    for form in forms:
        form_id = form.get("id")
        if not form_id:
            continue
        params = {
            "access_token": _token(),
            "fields": "id,created_time,ad_id,form_id,campaign_id,field_data",
            "filtering": json.dumps([
                {"field": "time_created", "operator": "GREATER_THAN", "value": since_unix},
                {"field": "time_created", "operator": "LESS_THAN", "value": until_unix},
            ]),
            "limit": PAGE_LIMIT,
        }
        leads.extend(_paginate(f"{BASE_URL}/{form_id}/leads", params))
    logger.info("Meta leads %s [%s..%s]: %d (across %d forms)",
                account_id, since, until, len(leads), len(forms))
    return leads


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    mel = os.getenv("META_MELBOURNE_AD_ACCOUNT_ID")
    print("Melbourne campaign insights:",
          len(fetch_campaign_insights(mel, "2026-04-01", "2026-04-30")))
