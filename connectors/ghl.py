"""
GHL (GoHighLevel) connector.

Fetches raw data from the GHL REST API and returns plain Python
list-of-dicts. Date filtering is done at the API where supported and
in-memory otherwise. No DataFrames, no DB writes.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
PAGE_LIMIT = 100
MAX_PAGES = 500
HTTP_TIMEOUT = 30


def _headers() -> dict:
    api_key = os.getenv("GHL_API_KEY")
    if not api_key:
        raise RuntimeError("GHL_API_KEY not set in .env")
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": API_VERSION,
        "Accept": "application/json",
    }


def _location_id() -> str:
    loc = os.getenv("GHL_LOCATION_ID")
    if not loc:
        raise RuntimeError("GHL_LOCATION_ID not set in .env")
    return loc


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fetch_pipelines() -> list[dict]:
    """All pipelines + stages. No date filter. Used as dim lookup."""
    r = requests.get(
        f"{BASE_URL}/opportunities/pipelines",
        headers=_headers(),
        params={"locationId": _location_id()},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    pipelines = r.json().get("pipelines", [])
    logger.info("GHL pipelines: %d", len(pipelines))
    return pipelines


def fetch_users() -> list[dict]:
    """All location users. Maps assignedTo → counsellor name."""
    r = requests.get(
        f"{BASE_URL}/users/",
        headers=_headers(),
        params={"locationId": _location_id()},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        logger.error("GHL users HTTP %d: %s", r.status_code, r.text[:200])
        return []
    users = r.json().get("users", [])
    logger.info("GHL users: %d", len(users))
    return users


def fetch_calendars() -> list[dict]:
    """All calendars in the location."""
    r = requests.get(
        f"{BASE_URL}/calendars/",
        headers=_headers(),
        params={"locationId": _location_id()},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        logger.error("GHL calendars HTTP %d: %s", r.status_code, r.text[:200])
        return []
    cals = r.json().get("calendars", [])
    logger.info("GHL calendars: %d", len(cals))
    return cals


def fetch_contacts(since: str, until: Optional[str] = None) -> list[dict]:
    """Contacts with dateAdded in [since, until]. Filtered in memory because
    /contacts/ doesn't expose a server-side date filter — paginated DESC by
    dateAdded so we can stop early."""
    until = until or _today()
    all_items: list[dict] = []
    start_after, start_after_id = None, None
    pages = 0
    while pages < MAX_PAGES:
        params: dict = {"locationId": _location_id(), "limit": PAGE_LIMIT}
        if start_after and start_after_id:
            params["startAfter"] = start_after
            params["startAfterId"] = start_after_id
        r = requests.get(
            f"{BASE_URL}/contacts/",
            headers=_headers(), params=params, timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            logger.error("GHL contacts page %d HTTP %d: %s",
                         pages + 1, r.status_code, r.text[:200])
            break
        body = r.json()
        items = body.get("contacts", [])
        if not items:
            break
        all_items.extend(items)
        meta = body.get("meta", {})
        start_after = meta.get("startAfter")
        start_after_id = meta.get("startAfterId")
        pages += 1
        oldest = items[-1].get("dateAdded", "")[:10]
        if oldest and oldest < since:
            break
        if not start_after_id:
            break
    in_range = [
        c for c in all_items
        if since <= (c.get("dateAdded") or "")[:10] <= until
    ]
    logger.info("GHL contacts: scanned %d in %d pages, %d in [%s..%s]",
                len(all_items), pages, len(in_range), since, until)
    return in_range


def fetch_opportunities(since: str, until: Optional[str] = None) -> list[dict]:
    """Opportunities with createdAt in [since, until]."""
    until = until or _today()
    all_items: list[dict] = []
    start_after, start_after_id = None, None
    pages = 0
    while pages < MAX_PAGES:
        params: dict = {"location_id": _location_id(), "limit": PAGE_LIMIT}
        if start_after and start_after_id:
            params["startAfter"] = start_after
            params["startAfterId"] = start_after_id
        r = requests.get(
            f"{BASE_URL}/opportunities/search",
            headers=_headers(), params=params, timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            logger.error("GHL opps page %d HTTP %d: %s",
                         pages + 1, r.status_code, r.text[:200])
            break
        body = r.json()
        items = body.get("opportunities", [])
        if not items:
            break
        all_items.extend(items)
        meta = body.get("meta", {})
        start_after = meta.get("nextPageStart") or meta.get("startAfter")
        start_after_id = meta.get("nextPageId") or meta.get("startAfterId")
        pages += 1
        oldest = items[-1].get("createdAt", "")[:10]
        if oldest and oldest < since:
            break
        if not start_after_id:
            break
    in_range = [
        o for o in all_items
        if since <= (o.get("createdAt") or "")[:10] <= until
    ]
    logger.info("GHL opportunities: scanned %d in %d pages, %d in [%s..%s]",
                len(all_items), pages, len(in_range), since, until)
    return in_range


def fetch_appointments(since: str, until: Optional[str] = None) -> list[dict]:
    """Calendar events for every calendar in the location, in [since, until].
    Each event keeps `calendarId` so consultant mapping happens downstream."""
    until = until or _today()
    syd = timezone(timedelta(hours=10))  # AEST; close enough for date-range filter
    s_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=syd)
    u_dt = datetime.strptime(until, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=syd)
    start_ms = int(s_dt.timestamp() * 1000)
    end_ms = int(u_dt.timestamp() * 1000)

    cals = fetch_calendars()
    all_events: list[dict] = []
    for cal in cals:
        cid = cal.get("id")
        if not cid:
            continue
        start_after, start_after_id = None, None
        pages = 0
        while pages < MAX_PAGES:
            params: dict = {
                "locationId": _location_id(),
                "calendarId": cid,
                "startTime": start_ms,
                "endTime": end_ms,
            }
            if start_after_id and start_after:
                params["startAfterId"] = start_after_id
                params["startAfter"] = start_after
            r = requests.get(
                f"{BASE_URL}/calendars/events",
                headers=_headers(), params=params, timeout=HTTP_TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning("GHL events cal=%s HTTP %d", cid, r.status_code)
                break
            body = r.json()
            chunk = body.get("events", [])
            if not chunk:
                break
            all_events.extend(chunk)
            meta = body.get("meta", {})
            start_after_id = meta.get("nextPageId") or meta.get("startAfterId")
            start_after = meta.get("nextPageStart") or meta.get("startAfter")
            pages += 1
            if not start_after_id:
                break
    logger.info("GHL appointments: %d events across %d calendars",
                len(all_events), len(cals))
    return all_events


def fetch_conversations(since: Optional[str] = None) -> list[dict]:
    """All conversations (newest first). Each carries `type` + `lastMessageType`
    (the channel — TYPE_FACEBOOK / TYPE_INSTAGRAM / TYPE_WHATSAPP / TYPE_TIKTOK /
    TYPE_EMAIL / TYPE_SMS / ...) plus contactId. Used to attribute a lead to its
    messaging platform (esp. Facebook-Messenger leads that never filled a form).
    If `since` (YYYY-MM-DD) is given, stops once lastMessageDate predates it."""
    since_ms = None
    if since:
        since_ms = int(datetime.strptime(since, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
    out: list[dict] = []
    start_after = None
    pages = 0
    while pages < MAX_PAGES:
        params: dict = {"locationId": _location_id(), "limit": PAGE_LIMIT,
                        "sortBy": "last_message_date", "sort": "desc"}
        if start_after:
            params["startAfterDate"] = start_after
        r = requests.get(f"{BASE_URL}/conversations/search",
                         headers=_headers(), params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            logger.warning("GHL conversations HTTP %d: %s", r.status_code, r.text[:200])
            break
        convs = r.json().get("conversations", [])
        if not convs:
            break
        out.extend(convs)
        pages += 1
        last = convs[-1]
        sa = last.get("sort")
        start_after = sa[0] if isinstance(sa, list) and sa else last.get("lastMessageDate")
        if since_ms and (last.get("lastMessageDate") or 0) < since_ms:
            break
        if len(convs) < PAGE_LIMIT:
            break
    logger.info("GHL conversations: %d across %d pages", len(out), pages)
    return out


def fetch_surveys() -> list[dict]:
    """All surveys defined in the GHL location. Returns id + name so we can
    map a survey-submission's surveyId to its name (e.g. 'Points Calculator')
    for the Latest Source field."""
    r = requests.get(
        f"{BASE_URL}/surveys/",
        headers=_headers(),
        params={"locationId": _location_id(), "limit": 50},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        logger.error("GHL /surveys HTTP %d: %s", r.status_code, r.text[:200])
        return []
    return r.json().get("surveys", [])


def fetch_survey_submissions(since: str, until: Optional[str] = None) -> list[dict]:
    """Survey submissions in [since, until]. Used as a Latest Source fallback
    for contacts who took a survey but never filled a form."""
    until = until or _today()
    all_items: list[dict] = []
    page = 1
    while page <= MAX_PAGES:
        r = requests.get(
            f"{BASE_URL}/surveys/submissions",
            headers=_headers(),
            params={
                "locationId": _location_id(),
                "startAt": since,
                "endAt": until,
                "limit": PAGE_LIMIT,
                "page": page,
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            logger.error("GHL /surveys/submissions page %d HTTP %d: %s",
                         page, r.status_code, r.text[:200])
            break
        body = r.json()
        items = body.get("submissions", [])
        if not items:
            break
        all_items.extend(items)
        nxt = (body.get("meta") or {}).get("nextPage")
        if not nxt:
            break
        page = nxt
    logger.info("GHL survey submissions: %d in [%s..%s]",
                len(all_items), since, until)
    return all_items


def fetch_contact(contact_id: str) -> dict:
    """Single contact GET, including attribution fields + custom fields."""
    r = requests.get(
        f"{BASE_URL}/contacts/{contact_id}",
        headers=_headers(),
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        return {}
    return r.json().get("contact", {})


def fetch_forms() -> list[dict]:
    """All forms defined in the GHL location. Used to look up form name from
    formId when the submission row has no formName / parentName (happens on
    funnel page-visit submissions where the form_name field is empty but the
    formId still points at a real form definition)."""
    r = requests.get(
        f"{BASE_URL}/forms/",
        headers=_headers(),
        params={"locationId": _location_id(), "limit": 100},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        logger.error("GHL /forms HTTP %d: %s", r.status_code, r.text[:200])
        return []
    return r.json().get("forms", [])


def fetch_form_submissions(since: str, until: Optional[str] = None) -> list[dict]:
    """Form submissions (real form-FILL events) with submission date in
    [since, until]. Uses /forms/submissions which — unlike the contacts and
    opportunities feeds — exposes server-side startAt/endAt date filters and a
    page cursor. Each row carries the contact, the submission timestamp, and
    the lastAttributionSource campaign (the 'new source' for a returning lead).
    """
    until = until or _today()
    all_items: list[dict] = []
    page = 1
    while page <= MAX_PAGES:
        params = {
            "locationId": _location_id(),
            "startAt": since,
            "endAt": until,
            "limit": PAGE_LIMIT,
            "page": page,
        }
        r = requests.get(
            f"{BASE_URL}/forms/submissions",
            headers=_headers(), params=params, timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            logger.error("GHL form submissions page %d HTTP %d: %s",
                         page, r.status_code, r.text[:200])
            break
        body = r.json()
        items = body.get("submissions", [])
        if not items:
            break
        all_items.extend(items)
        nxt = (body.get("meta") or {}).get("nextPage")
        if not nxt:
            break
        page = nxt
    logger.info("GHL form submissions: %d in [%s..%s]", len(all_items), since, until)
    return all_items


def fetch_payments(since: str, until: Optional[str] = None) -> list[dict]:
    """Payment transactions in [since, until]. Filtered in memory.
    Uses altId/altType=location params + offset pagination (v2 payments API
    shape — 403s if you use `locationId`)."""
    until = until or _today()
    all_items: list[dict] = []
    offset = 0
    pages = 0
    while pages < MAX_PAGES:
        r = requests.get(
            f"{BASE_URL}/payments/transactions",
            headers=_headers(),
            params={
                "altId": _location_id(),
                "altType": "location",
                "limit": PAGE_LIMIT,
                "offset": offset,
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning("GHL payments page %d HTTP %d: %s",
                           pages + 1, r.status_code, r.text[:200])
            break
        body = r.json()
        items = body.get("data") or body.get("transactions") or []
        if not items:
            break
        all_items.extend(items)
        pages += 1
        if len(items) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

    def _date(p: dict) -> str:
        for k in ("createdAt", "dateCreated", "date_created", "created"):
            v = p.get(k)
            if v:
                return v[:10]
        return ""

    in_range = [p for p in all_items if since <= _date(p) <= until]
    logger.info("GHL payments: scanned %d, %d in [%s..%s]",
                len(all_items), len(in_range), since, until)
    return in_range


# ---------------------------------------------------------------------
# Writes — "Latest Source" custom field (contact + opportunity)
# Field IDs are fixed for location Cy61ZIoB1Q68krX0lSZA (see memory).
# ---------------------------------------------------------------------

LATEST_SOURCE_CONTACT_FIELD = "MOxKefk4DsSy146Mcg0q"   # contact.latest_source
LATEST_SOURCE_OPP_FIELD = "SIrT43qV7MXhLy6Er1ti"       # opportunity.latest_source


def update_contact_custom_field(contact_id: str, field_id: str, value: str) -> bool:
    """PUT a single custom field on a contact (other fields preserved)."""
    r = requests.put(
        f"{BASE_URL}/contacts/{contact_id}",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"customFields": [{"id": field_id, "value": value}]},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code not in (200, 201):
        logger.warning("update contact %s field HTTP %d: %s", contact_id, r.status_code, r.text[:160])
    return r.status_code in (200, 201)


def fetch_contact_opportunities(contact_id: str) -> list[dict]:
    """All opportunities for one contact (any pipeline)."""
    r = requests.get(
        f"{BASE_URL}/opportunities/search",
        headers=_headers(),
        params={"location_id": _location_id(), "contact_id": contact_id},
        timeout=HTTP_TIMEOUT,
    )
    return r.json().get("opportunities", []) if r.status_code == 200 else []


def update_opportunity_custom_field(opportunity_id: str, field_id: str, value: str) -> bool:
    """PUT a single custom field on an opportunity (other fields preserved)."""
    r = requests.put(
        f"{BASE_URL}/opportunities/{opportunity_id}",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"customFields": [{"id": field_id, "value": value}]},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code not in (200, 201):
        logger.warning("update opp %s field HTTP %d: %s", opportunity_id, r.status_code, r.text[:160])
    return r.status_code in (200, 201)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print("Pipelines:", len(fetch_pipelines()))
    print("Users:", len(fetch_users()))
    print("Calendars:", len(fetch_calendars()))
    print("Opportunities (April):", len(fetch_opportunities("2026-04-01", "2026-04-30")))
