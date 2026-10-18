"""
Google Ads connector.

STATUS: standalone / not yet wired into etl/run_etl.py or models/schema.sql.
Add a fact_google_ads table + normalize.normalize_google_ads_daily() and call
fetch_campaign_daily_insights() from run_etl.py once the account details below
are confirmed (this mirrors how meta.py feeds fact_meta_daily).

Needs the `google-ads` package (pip install google-ads) and an OAuth refresh
token generated for a Google Ads API project — see
https://developers.google.com/google-ads/api/docs/first-call/overview

Env vars (.env):
    GOOGLE_ADS_DEVELOPER_TOKEN
    GOOGLE_ADS_CLIENT_ID
    GOOGLE_ADS_CLIENT_SECRET
    GOOGLE_ADS_REFRESH_TOKEN
    GOOGLE_ADS_LOGIN_CUSTOMER_ID   # MCC id, digits only, only if using a manager account
    GOOGLE_ADS_CUSTOMER_ID         # target account id, digits only
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

API_VERSION = "v18"


def _client():
    from google.ads.googleads.client import GoogleAdsClient

    required = ("GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID",
                "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing Google Ads env vars: {', '.join(missing)}")
    config = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus": True,
    }
    login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login_customer_id:
        config["login_customer_id"] = login_customer_id
    return GoogleAdsClient.load_from_dict(config, version=API_VERSION)


def _customer_id() -> str:
    cid = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
    if not cid:
        raise RuntimeError("GOOGLE_ADS_CUSTOMER_ID not set in .env")
    return cid.replace("-", "")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fetch_campaigns(customer_id: Optional[str] = None) -> list[dict]:
    """All campaigns (id, name, status) in the account. Dim lookup, no date
    filter — mirrors meta.fetch_campaigns()."""
    customer_id = customer_id or _customer_id()
    client = _client()
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT campaign.id, campaign.name, campaign.status
        FROM campaign
    """
    rows = []
    try:
        for batch in ga_service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                rows.append({
                    "campaign_id": row.campaign.id,
                    "campaign_name": row.campaign.name,
                    "status": row.campaign.status.name,
                })
    except Exception as e:
        logger.exception("Google Ads campaigns fetch failed: %s", e)
        return []
    logger.info("Google Ads campaigns %s: %d", customer_id, len(rows))
    return rows


def fetch_campaign_daily_insights(
    since: str, until: Optional[str] = None, customer_id: Optional[str] = None
) -> list[dict]:
    """Daily campaign-level spend/clicks/conversions in [since, until].
    cost is returned in micros by the API and converted to whole currency
    units here (spend = cost_micros / 1_000_000), matching fact_meta_daily's
    `spend` column so the same dashboard views can union both sources later."""
    until = until or _today()
    customer_id = customer_id or _customer_id()
    client = _client()
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions,
            metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{since}' AND '{until}'
    """
    rows: list[dict] = []
    try:
        for batch in ga_service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                rows.append({
                    "date": row.segments.date,
                    "campaign_id": row.campaign.id,
                    "campaign_name": row.campaign.name,
                    "spend": row.metrics.cost_micros / 1_000_000,
                    "impressions": row.metrics.impressions,
                    "clicks": row.metrics.clicks,
                    "conversions": row.metrics.conversions,
                    "conversions_value": row.metrics.conversions_value,
                    "_customer_id": customer_id,
                })
    except Exception as e:
        logger.exception("Google Ads daily insights fetch failed: %s", e)
        return []
    logger.info("Google Ads daily_insights %s [%s..%s]: %d rows",
                customer_id, since, until, len(rows))
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print("Campaigns:", len(fetch_campaigns()))
    print("Daily insights (last 7 days):",
          len(fetch_campaign_daily_insights(
              (datetime.now().replace(day=1)).strftime("%Y-%m-%d"))))
