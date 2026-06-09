"""Smoke-test all 4 connectors with a 2-day window. Should be fast (<60s)."""
import logging
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Make sibling .env discoverable when run from migration-dashboard/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # parent of migration-dashboard

from connectors import ghl, meta, ga4, gsc  # noqa: E402

SINCE = "2026-04-01"
UNTIL = "2026-04-02"  # 2-day window for speed

print("=" * 60)
print(f"CONNECTOR SMOKE TEST  ({SINCE} to {UNTIL})")
print("=" * 60)

print("\n--- GHL ---")
print(f"  pipelines: {len(ghl.fetch_pipelines())}")
print(f"  users:     {len(ghl.fetch_users())}")
print(f"  calendars: {len(ghl.fetch_calendars())}")
print(f"  contacts:  {len(ghl.fetch_contacts(SINCE, UNTIL))}")
print(f"  opps:      {len(ghl.fetch_opportunities(SINCE, UNTIL))}")
# appointments is slow (53 calendars × pagination); skip in smoke
# print(f"  appts:     {len(ghl.fetch_appointments(SINCE, UNTIL))}")

print("\n--- META ---")
mel = os.getenv("META_MELBOURNE_AD_ACCOUNT_ID")
syd = os.getenv("META_SYDNEY_AD_ACCOUNT_ID")
print(f"  Melbourne campaigns: {len(meta.fetch_campaign_insights(mel, SINCE, UNTIL))}")
print(f"  Sydney campaigns:    {len(meta.fetch_campaign_insights(syd, SINCE, UNTIL))}")
print(f"  Mel adsets:          {len(meta.fetch_adset_optimization(mel))}")

print("\n--- GA4 ---")
print(f"  daily sessions: {len(ga4.fetch_daily_sessions(SINCE, UNTIL))}")
print(f"  key events:     {len(ga4.fetch_key_events(SINCE, UNTIL))}")

print("\n--- GSC ---")
print(f"  queries:    {len(gsc.fetch_search_analytics(SINCE, UNTIL, 'query', 100))}")
print(f"  pages:      {len(gsc.fetch_search_analytics(SINCE, UNTIL, 'page', 100))}")
print(f"  countries:  {len(gsc.fetch_search_analytics(SINCE, UNTIL, 'country', 50))}")
print(f"  devices:    {len(gsc.fetch_search_analytics(SINCE, UNTIL, 'device', 10))}")

print("\n" + "=" * 60)
print("SMOKE TEST COMPLETE")
print("=" * 60)
