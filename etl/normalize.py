"""
Normalization layer.

Each normalize_* function takes raw API output (lists of dicts) and returns
a pandas DataFrame whose columns match a fact / dim / bridge table exactly.
Missing fields are filled with None — never crash on a missing key.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Canonical mappings
# ---------------------------------------------------------------------

# Six pipelines we report on (per sprint decision 1b)
CANONICAL_PIPELINES = {
    "L2C - Education": "admissions",
    "L2C - VISA": "visa",
    "CLT - Onshore Admission": "admissions",
    "CLT - VISA": "visa",
    "L2C - Skill Migration": "visa",
    "Accounts": "accounts",
}

# Eight named counsellors (per sprint brief).
# Match by substring on calendar/user name.
NAMED_COUNSELLORS: list[tuple[str, str]] = [
    # (counsellor_id, name-fragment lowercased)
    ("usr_wajahat",   "wajah"),
    ("usr_saurab",    "saurab"),
    ("usr_kajal",     "kajal"),
    ("usr_navneet",   "navneet"),
    ("usr_nasir",     "nasir"),
    ("usr_gurbir",    "gurbir"),
    ("usr_syedturab", "turab"),
    ("usr_manhal",    "manhal"),
]


def map_canonical_source(raw: Optional[str]) -> str:
    """GHL `source` field → canonical bucket."""
    if not raw:
        return "other"
    s = raw.lower()
    if any(k in s for k in ("facebook", "instagram", "meta", " fb", "ig ", "ig_")):
        return "meta_paid"
    if any(k in s for k in ("google", "seo", "organic")):
        return "organic_seo"
    if any(k in s for k in ("chat", "manychat", "bot")):
        return "chatbot"
    if "refer" in s:
        return "referral"
    if any(k in s for k in ("form", "contact us", "website", "homepage")):
        return "website_form"
    if any(k in s for k in ("survey", "quiz", "calculator")):
        return "survey"
    return "other"


def map_canonical_loss_reason(raw: Optional[str]) -> str:
    """GHL lostReason free text → 5-bucket canonical."""
    if not raw:
        return "other"
    s = raw.lower()
    if any(k in s for k in ("not eligible", "not qualified", "doesn't qualify",
                            "no points", "ineligible")):
        return "not_qualified"
    if any(k in s for k in ("no response", "unreachable", "ghost", "no answer",
                            "did not respond", "not responding")):
        return "no_response"
    if any(k in s for k in ("budget", "too expensive", "cost", "afford", "price")):
        return "budget"
    if any(k in s for k in ("competitor", "went elsewhere", "another agent",
                            "other agent")):
        return "competitor"
    return "other"


def map_appointment_outcome(raw: Optional[str]) -> str:
    """GHL appointmentStatus → 4-bucket canonical."""
    if not raw:
        return "pending"
    s = raw.lower()
    if "showed" in s or s == "show":
        return "show"
    if "noshow" in s or "no-show" in s or "no show" in s:
        return "noshow"
    if "cancel" in s or "invalid" in s:
        return "cancelled"
    return "pending"


def map_funnel_step(stage_name: str) -> str:
    """Heuristic: stage_name → one of 6 funnel buckets."""
    if not stage_name:
        return "other"
    s = stage_name.lower()
    if any(k in s for k in ("new lead", "qualifier", "pre sales")):
        return "lead"
    if "book" in s or "appointment" in s:
        return "booking"
    if "post consultation" in s or s == "show":
        return "show"
    if "initial req" in s or "initial received" in s:
        return "initial_requested"
    if "paid" in s or "payment" in s:
        return "paid"
    if "coe" in s or "voe" in s or s == "won":
        return "coe_voe"
    return "other"


def assign_counsellor_id(name: Optional[str]) -> Optional[str]:
    """Map a calendar/user name to one of the 8 named consultants by substring."""
    if not name:
        return None
    n = name.lower()
    for cid, frag in NAMED_COUNSELLORS:
        if frag in n:
            return cid
    return None


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _ts(value: Any) -> Optional[pd.Timestamp]:
    """Parse ISO timestamp → pandas Timestamp (UTC). Returns None on error."""
    if not value:
        return None
    try:
        return pd.to_datetime(value, utc=True, errors="coerce")
    except Exception:
        return None


def _ts_flex(value: Any) -> Optional[pd.Timestamp]:
    """Like _ts but handles GHL fields that come as a Unix MILLISECONDS epoch
    (e.g. conversation lastMessageDate/dateAdded = 1779277972528). A bare integer
    passed to pd.to_datetime is read as NANOSECONDS → ~1970, so route numeric
    values through unit='ms'."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            return pd.to_datetime(int(value), unit="ms", utc=True)
        return pd.to_datetime(value, utc=True, errors="coerce")
    except Exception:
        return None


def _to_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def _digits(s: Optional[str]) -> str:
    """Strip non-digits from a phone string."""
    if not s:
        return ""
    return re.sub(r"\D", "", s)


def _last_n_digits(phone: Optional[str], n: int = 9) -> str:
    """Last n digits of a phone — used for cross-country phone matching."""
    d = _digits(phone)
    return d[-n:] if len(d) >= n else d


# ---------------------------------------------------------------------
# GHL normalizers
# ---------------------------------------------------------------------

def normalize_pipelines(raw: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (dim_pipelines_df, dim_stages_df) from GHL pipelines API.
    Marks pipelines as is_canonical=True if their name is in CANONICAL_PIPELINES."""
    pipe_rows = []
    stage_rows = []
    for p in raw:
        pid = _to_str(p.get("id"))
        name = p.get("name") or ""
        is_canonical = name in CANONICAL_PIPELINES
        pipe_rows.append({
            "pipeline_id": pid,
            "pipeline_name": name,
            "pipeline_type": CANONICAL_PIPELINES.get(name, "other"),
            "is_canonical": is_canonical,
        })
        for idx, st in enumerate(p.get("stages", [])):
            stage_rows.append({
                "stage_id": _to_str(st.get("id")),
                "stage_name": st.get("name") or "",
                "pipeline_id": pid,
                "stage_order": idx,
                "funnel_step": map_funnel_step(st.get("name") or ""),
            })
    return pd.DataFrame(pipe_rows), pd.DataFrame(stage_rows)


def normalize_calendars(raw: list[dict], users: list[dict]) -> pd.DataFrame:
    """All 53 calendars. Maps to counsellor_id where possible."""
    user_name_by_id = {
        _to_str(u.get("id")): f"{u.get('firstName','')} {u.get('lastName','')}".strip()
        for u in users
    }
    rows = []
    for c in raw:
        cid = _to_str(c.get("id"))
        cname = c.get("name") or ""
        # Try matching against the calendar's own name first.
        counsellor_id = assign_counsellor_id(cname)
        # If not found, try the assigned team member if present.
        if counsellor_id is None:
            for tm in c.get("teamMembers", []) or []:
                uname = user_name_by_id.get(_to_str(tm.get("userId")))
                counsellor_id = assign_counsellor_id(uname)
                if counsellor_id:
                    break
        rows.append({
            "calendar_id": cid,
            "calendar_name": cname,
            "counsellor_id": counsellor_id,
            "is_named_consultant": counsellor_id is not None,
            "is_active": c.get("isActive", True),
        })
    return pd.DataFrame(rows)


def normalize_contacts(raw: list[dict]) -> pd.DataFrame:
    """fact_contacts."""
    rows = []
    def _medium(attr: dict) -> str | None:
        # GHL attribution objects carry medium as one of: medium, utmMedium,
        # sessionSource. 'manual'/'survey'/'calendar' usually live in `medium`.
        return attr.get("medium") or attr.get("utmMedium") or None

    def _form(attr: dict) -> str | None:
        return attr.get("formName") or attr.get("form") or None

    for c in raw:
        attrs = c.get("attributions") or []
        first_attr = attrs[0] if attrs else {}
        last_attr = attrs[-1] if attrs else {}
        # visa_type from custom fields if any
        visa = None
        for cf in c.get("customFields", []) or []:
            cf_id = (cf.get("id") or "").lower()
            cf_val = cf.get("value")
            if "visa" in cf_id or "visa_type" in cf_id:
                visa = _to_str(cf_val); break
        src = c.get("source")
        rows.append({
            "contact_id": _to_str(c.get("id")),
            "contact_name": c.get("contactName"),
            "email": c.get("email"),
            "phone": c.get("phone"),
            "date_added": _ts(c.get("dateAdded")),
            "date_updated": _ts(c.get("dateUpdated")),
            "source": src,
            "canonical_source": map_canonical_source(src),
            "assigned_user_id": _to_str(c.get("assignedTo")),
            "first_attribution_source": first_attr.get("utmSessionSource") or first_attr.get("source"),
            "latest_attribution_source": last_attr.get("utmSessionSource") or last_attr.get("source"),
            "first_attribution_medium": _medium(first_attr),
            "first_attribution_form": _form(first_attr),
            "latest_attribution_medium": _medium(last_attr),
            "latest_attribution_form": _form(last_attr),
            # campaign from the attribution (Facebook Lead Form etc.) — GHL stores
            # it as utmCampaign on contact attributions. Prefer latest, then first.
            "attribution_campaign": (last_attr.get("utmCampaign")
                                     or first_attr.get("utmCampaign")
                                     or last_attr.get("campaign")
                                     or first_attr.get("campaign")),
            "country": c.get("country"),
            "city": c.get("city"),
            "visa_type": visa,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["contact_id"]).drop_duplicates(subset=["contact_id"])
    return df


def channel_from_conversation(conv: dict) -> str:
    """Map a GHL conversation's type / lastMessageType to a lead channel."""
    lmt = (conv.get("lastMessageType") or "")
    t = (conv.get("type") or "")
    for u in (lmt.upper(), t.upper()):
        if "FACEBOOK" in u or u == "TYPE_FB_MESSENGER":
            return "Facebook"
        if "INSTAGRAM" in u:
            return "Instagram"
        if "WHATSAPP" in u:
            return "WhatsApp"
        if "TIKTOK" in u:
            return "TikTok"
        if "GMB" in u:
            return "Google Business"
        if "LIVE_CHAT" in u or "WEBCHAT" in u:
            return "Web Chat"
    u = lmt.upper()
    if "EMAIL" in u:
        return "Email"
    if any(x in u for x in ("SMS", "CALL", "PHONE", "VOICEMAIL", "NO_SHOW")):
        return "Phone/SMS"
    return "Other"


def normalize_conversations(raw: list[dict]) -> pd.DataFrame:
    """fact_conversations — 1 row per GHL conversation, with a derived `channel`."""
    rows = []
    for c in raw:
        rows.append({
            "conversation_id":   _to_str(c.get("id")),
            "contact_id":        _to_str(c.get("contactId")),
            "channel":           channel_from_conversation(c),
            "conv_type":         c.get("type"),
            "last_message_type": c.get("lastMessageType"),
            "last_message_at":   _ts_flex(c.get("lastMessageDate")),
            "date_added":        _ts_flex(c.get("dateAdded")),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["conversation_id"]).drop_duplicates(subset=["conversation_id"])
    return df


def normalize_opportunities(raw: list[dict], stage_name_by_id: dict) -> pd.DataFrame:
    """fact_opportunities. stage_name_by_id is built from dim_stages."""
    rows = []
    for o in raw:
        contact = o.get("contact") or {}
        stage_id = _to_str(o.get("pipelineStageId"))
        stage_name = stage_name_by_id.get(stage_id, "")
        src = o.get("source")
        loss = o.get("lostReason")
        rows.append({
            "opportunity_id": _to_str(o.get("id")),
            "contact_id": _to_str(o.get("contactId")),
            "opportunity_name": o.get("name"),
            "pipeline_id": _to_str(o.get("pipelineId")),
            "stage_id": stage_id,
            "status": o.get("status"),
            "monetary_value": float(o.get("monetaryValue") or 0),
            "assigned_user_id": _to_str(o.get("assignedTo")),
            "source": src,
            "canonical_source": map_canonical_source(src),
            "loss_reason": loss,
            "canonical_loss_reason": map_canonical_loss_reason(loss),
            "days_in_pipeline": int(o.get("days") or 0),
            "created_at": _ts(o.get("createdAt")),
            "updated_at": _ts(o.get("updatedAt")),
            "last_stage_change_at": _ts(o.get("lastStageChangeAt")),
            "last_status_change_at": _ts(o.get("lastStatusChangeAt")),
            "visa_type": None,  # not natively in opp; inherits from contact via join
            "country": contact.get("country") or o.get("country"),
            "city": contact.get("city") or o.get("city"),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["opportunity_id"]).drop_duplicates(subset=["opportunity_id"])
    return df


def normalize_messages(raw: list[dict]) -> pd.DataFrame:
    """fact_messages — 1 row per conversation message. user_id is the staff actor
    (None for system/automation logs); dateAdded is an ISO timestamp here."""
    rows = []
    for m in raw:
        rows.append({
            "message_id":      _to_str(m.get("id")),
            "conversation_id": _to_str(m.get("conversationId")),
            "contact_id":      _to_str(m.get("contactId")),
            "user_id":         _to_str(m.get("userId")),
            "message_type":    m.get("messageType") or m.get("type"),
            "direction":       m.get("direction"),
            "date_added":      _ts(m.get("dateAdded")),
        })
    df = pd.DataFrame(rows, columns=["message_id", "conversation_id", "contact_id",
                                     "user_id", "message_type", "direction", "date_added"])
    if not df.empty:
        df = df.dropna(subset=["message_id"]).drop_duplicates(subset=["message_id"])
    return df


def normalize_stage_events(raw: list[dict]) -> pd.DataFrame:
    """fact_opp_stage_events — reconstruct an opportunity's stage timeline from GHL
    TYPE_ACTIVITY_OPPORTUNITY activity logs. Each such message carries
    activity.data = {id, name, status, pipeline, stage:{oldStageName,newStageName}}.
    We keep any event that records a newStageName (stage moves carry old+new; status
    changes carry new only — still a valid stage observation at that timestamp)."""
    rows = []
    for m in raw:
        act = m.get("activity")
        if not isinstance(act, dict):
            continue
        data = act.get("data")
        if not isinstance(data, dict):
            continue
        stage = data.get("stage")
        if not isinstance(stage, dict):
            continue
        new_stage = stage.get("newStageName")
        if not new_stage:
            continue
        rows.append({
            "event_id":       _to_str(m.get("id")),
            "opportunity_id": _to_str(data.get("id")),
            "contact_id":     _to_str(m.get("contactId")),
            "pipeline":       data.get("pipeline"),
            "old_stage":      stage.get("oldStageName"),
            "new_stage":      new_stage,
            "changed_at":     _ts(m.get("dateAdded")),
        })
    df = pd.DataFrame(rows, columns=["event_id", "opportunity_id", "contact_id",
                                     "pipeline", "old_stage", "new_stage", "changed_at"])
    if not df.empty:
        df = df.dropna(subset=["event_id", "opportunity_id"]).drop_duplicates(subset=["event_id"])
    return df


def normalize_opportunity_followers(raw: list[dict]) -> pd.DataFrame:
    """fact_opportunity_followers — one row per (opportunity_id, follower_user_id),
    exploded from each opportunity's `followers` array."""
    rows = []
    for o in raw:
        oid = _to_str(o.get("id"))
        if not oid:
            continue
        for uid in (o.get("followers") or []):
            uid = _to_str(uid)
            if uid:
                rows.append({"opportunity_id": oid, "follower_user_id": uid})
    df = pd.DataFrame(rows, columns=["opportunity_id", "follower_user_id"])
    if not df.empty:
        df = df.drop_duplicates()
    return df


def normalize_appointments(
    raw: list[dict], calendar_to_counsellor: dict
) -> pd.DataFrame:
    """fact_appointments."""
    rows = []
    for e in raw:
        cal_id = _to_str(e.get("calendarId"))
        # Pull amount_paid from a few possible nested locations
        amt = 0.0
        for p in (e.get("paymentDetails"), e.get("payment"),
                  (e.get("meta") or {}).get("payment")):
            if isinstance(p, dict):
                v = (p.get("amountPaid") or p.get("amount_paid")
                     or p.get("totalAmount") or p.get("amount"))
                if v:
                    try:
                        amt = float(v); break
                    except Exception:
                        pass
        status = e.get("appointmentStatus")
        rows.append({
            "appointment_id": _to_str(e.get("id")),
            "contact_id": _to_str(e.get("contactId")),
            "calendar_id": cal_id,
            "counsellor_id": calendar_to_counsellor.get(cal_id),
            "appointment_status": status,
            "canonical_outcome": map_appointment_outcome(status),
            "start_time": _ts(e.get("startTime")),
            "end_time": _ts(e.get("endTime")),
            # dateAdded = when the booking was made; used by Executive-tab
            # Lead→Booking + Show Rate cards. startTime stays for Counsellor tab.
            "date_added": _ts(e.get("dateAdded")),
            "amount_paid": amt,
            "title": e.get("title"),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["appointment_id"]).drop_duplicates(subset=["appointment_id"])
    return df


def normalize_payments(raw: list[dict]) -> pd.DataFrame:
    """fact_payments — 1 row per /payments/transactions item.
    Amounts come back in dollars (NOT cents) already, e.g. 100.0 = AU$100."""
    rows = []
    for p in raw:
        rows.append({
            "transaction_id":     _to_str(p.get("_id") or p.get("id")),
            "contact_id":         _to_str(p.get("contactId")),
            "contact_email":      p.get("contactEmail"),
            "amount":             p.get("amount"),
            "amount_refunded":    p.get("amountRefunded") or 0,
            "currency":           p.get("currency"),
            "status":             p.get("status"),
            "entity_type":        p.get("entityType"),
            "entity_source_name": p.get("entitySourceName"),
            "payment_provider":   p.get("paymentProviderType"),
            "created_at":         _ts(p.get("createdAt")),
            "fulfilled_at":       _ts(p.get("fulfilledAt")),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["transaction_id"]).drop_duplicates(subset=["transaction_id"])
    return df


def normalize_survey_submissions(raw: list[dict], surveys_by_id: dict) -> pd.DataFrame:
    """fact_survey_submissions — 1 row per GHL survey submission.
    surveys_by_id is {survey_id: survey_name} from /surveys/. Used so we can
    resolve survey_name on the submission row (since the submission only
    carries surveyId, not the name)."""
    rows = []
    for s in raw:
        others = s.get("others") or {}
        last_attr = others.get("lastAttributionSource") or {}
        ev = others.get("eventData") or {}
        page = ev.get("page") or {}
        page_url = page.get("url")
        page_path = None
        if isinstance(page_url, str) and page_url:
            try:
                from urllib.parse import urlparse
                p = urlparse(page_url)
                pp = (p.path or "/").lower().rstrip("/") or "/"
                page_path = pp
            except Exception:
                page_path = None
        sid = _to_str(s.get("surveyId"))
        rows.append({
            "submission_id":  _to_str(s.get("id")),
            "contact_id":     _to_str(s.get("contactId")),
            "survey_id":      sid,
            "survey_name":    surveys_by_id.get(sid),
            "session_source": last_attr.get("sessionSource"),
            "campaign":       last_attr.get("campaign"),
            "utm_content":    last_attr.get("utmContent"),
            "event_source":   ev.get("source"),
            "page_url":       page_url,
            "page_path":      page_path,
            "referrer":       ev.get("referrer"),
            "email":          s.get("email"),
            "submitted_at":   _ts(s.get("createdAt")),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["submission_id"]).drop_duplicates(subset=["submission_id"])
    return df


def normalize_form_submissions(raw: list[dict]) -> pd.DataFrame:
    """fact_form_submissions — 1 row per real form-fill event.
    Pulls product_type / session_source / campaign out of the nested
    `others.lastAttributionSource` so the dashboard can flag Meta (Paid Social)
    submissions and surface the campaign as the contact's 'new source'."""
    rows = []
    for s in raw:
        others = s.get("others") or {}
        last_attr = others.get("lastAttributionSource") or {}
        ev = others.get("eventData") or {}
        parent = ev.get("parentName")
        if isinstance(parent, str):
            parent = parent.strip()  # organic parentName often has a leading nbsp
        page = ev.get("page") or {}
        page_url = page.get("url")
        # Normalised path: strip protocol+host, query string, fragment, lowercase,
        # collapse trailing slash. Lets us JOIN to fact_ga4_pages.page_path.
        page_path = None
        if isinstance(page_url, str) and page_url:
            try:
                from urllib.parse import urlparse
                p = urlparse(page_url)
                pp = (p.path or "/").lower().rstrip("/") or "/"
                page_path = pp
            except Exception:
                page_path = None
        # Chat-widget submissions don't carry a form_name in the API payload
        # (productType='chat-widget', formId='cwf-...', parentName=''), but GHL's
        # activity feed labels them "Chat Widget Form". Detect via productType
        # OR the cwf- formId prefix and use that as the form name.
        form_id_str = _to_str(s.get("formId"))
        product_type = others.get("productType")
        is_chat_widget = (product_type == "chat-widget"
                          or (form_id_str or "").startswith("cwf-"))
        form_name = (others.get("facebookFormName")
                     or last_attr.get("formName")
                     or parent
                     or ("Chat Widget Form" if is_chat_widget else None))
        rows.append({
            "submission_id": _to_str(s.get("id")),
            "contact_id": _to_str(s.get("contactId")),
            "form_id": form_id_str,
            "form_name": form_name,
            "product_type": product_type,
            "session_source": last_attr.get("sessionSource"),
            "campaign": last_attr.get("campaign"),
            "utm_content": last_attr.get("utmContent"),
            "event_form_name": parent,
            "event_source": ev.get("source"),
            "page_url": page_url,
            "page_path": page_path,
            "referrer": ev.get("referrer"),
            "email": s.get("email"),
            "submitted_at": _ts(s.get("createdAt")),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["submission_id"]).drop_duplicates(subset=["submission_id"])
    return df


# ---------------------------------------------------------------------
# Meta normalizer
# ---------------------------------------------------------------------

LEAD_FORM_ACTIONS = {"lead"}
PIXEL_LEAD_ACTIONS = {"offsite_conversion.fb_pixel_lead"}
PIXEL_CUSTOM_ACTIONS = {"offsite_conversion.fb_pixel_custom"}
MESSENGER_LEAD_ACTIONS = {"onsite_conversion.messaging_conversation_started_7d"}
CALL_LEAD_ACTIONS = {"offsite_lead_add_20_s_calls"}


def _sum_actions(actions: list[dict], match_set: set) -> int:
    return int(sum(float(a.get("value") or 0)
                   for a in actions if a.get("action_type") in match_set))


def _pick_result_event(action_map: dict[str, float]) -> tuple[Optional[str], int]:
    """Replicate Ads Manager 'Results' column heuristic (validated against
    Melbourne Apr 2026 = 153)."""
    if action_map.get("lead", 0) > 0:
        return "lead", int(action_map["lead"])
    if action_map.get("offsite_conversion.fb_pixel_lead", 0) > 0:
        return ("offsite_conversion.fb_pixel_lead",
                int(action_map["offsite_conversion.fb_pixel_lead"]))
    offsite = {k: v for k, v in action_map.items()
               if k.startswith("offsite_conversion.") and v > 0}
    if offsite:
        best = max(offsite, key=offsite.get)
        return best, int(offsite[best])
    return None, 0


def normalize_meta_insights(
    raw: list[dict], account_label: str, since: str, until: str
) -> pd.DataFrame:
    """fact_meta_insights — campaign-level. Composite key:
       {account_id}|{campaign_id}|{since}|{until}"""
    rows = []
    for r in raw:
        actions = r.get("actions") or []
        action_map = {a["action_type"]: float(a.get("value") or 0)
                      for a in actions if a.get("action_type")}
        instant_form = _sum_actions(actions, LEAD_FORM_ACTIONS)
        pixel_lead = _sum_actions(actions, PIXEL_LEAD_ACTIONS)
        pixel_custom = _sum_actions(actions, PIXEL_CUSTOM_ACTIONS)
        msgr = _sum_actions(actions, MESSENGER_LEAD_ACTIONS)
        calls = _sum_actions(actions, CALL_LEAD_ACTIONS)
        total_leads = instant_form + pixel_lead + pixel_custom
        result_event, result_count = _pick_result_event(action_map)

        # Video retention
        v3s = int(action_map.get("video_view", 0))
        thru = int(sum(float(a.get("value") or 0)
                       for a in r.get("video_thruplay_watched_actions", []) or []))
        v50 = int(sum(float(a.get("value") or 0)
                      for a in r.get("video_p50_watched_actions", []) or []))
        v95 = int(sum(float(a.get("value") or 0)
                      for a in r.get("video_p95_watched_actions", []) or []))

        # Outbound clicks
        outbound = sum(float(a.get("value") or 0)
                       for a in r.get("outbound_clicks", []) or [])
        # treat link_clicks separately
        link_clicks = int(r.get("inline_link_clicks") or 0)

        acct_id = r.get("_account_id") or ""
        camp_id = _to_str(r.get("campaign_id")) or ""
        rows.append({
            "insight_key": f"{acct_id}|{camp_id}|{since}|{until}",
            "account_id": acct_id,
            "account_label": account_label,
            "campaign_id": camp_id,
            "campaign_name": r.get("campaign_name"),
            "objective": r.get("objective"),
            "date_start": pd.to_datetime(since).date() if since else None,
            "date_end": pd.to_datetime(until).date() if until else None,
            "spend": float(r.get("spend") or 0),
            "impressions": int(r.get("impressions") or 0),
            "reach": int(r.get("reach") or 0),
            "frequency": float(r.get("frequency") or 0),
            "clicks": int(r.get("clicks") or 0),
            "link_clicks": link_clicks,
            "ctr": float(r.get("ctr") or 0),
            "link_ctr": float(r.get("inline_link_click_ctr") or 0),
            "cpc": float(r.get("cpc") or 0),
            "cpm": float(r.get("cpm") or 0),
            "instant_form_leads": instant_form,
            "pixel_lead_events": pixel_lead,
            "pixel_custom_events": pixel_custom,
            "messenger_leads": msgr,
            "call_leads": calls,
            "total_leads": total_leads,
            "result_event": result_event,
            "result_count": result_count,
            "video_3s_views": v3s,
            "video_thruplays": thru,
            "video_p50_views": v50,
            "video_p95_views": v95,
        })
    return pd.DataFrame(rows)


def normalize_meta_daily(
    raw: list[dict], account_label: str
) -> pd.DataFrame:
    """fact_meta_daily — composite key {account_id}|{campaign_id}|{date}|{country}.
    Includes per-day `result_event` + `result_count` (Ads Manager 'Results'
    heuristic) and `objective` so the dashboard can filter by lead-objective
    campaigns at day grain.
    """
    rows = []
    for r in raw:
        actions = r.get("actions") or []
        action_map = {a["action_type"]: float(a.get("value") or 0)
                      for a in actions if a.get("action_type")}
        instant_form = _sum_actions(actions, LEAD_FORM_ACTIONS)
        pixel_lead = _sum_actions(actions, PIXEL_LEAD_ACTIONS)
        pixel_custom = _sum_actions(actions, PIXEL_CUSTOM_ACTIONS)
        total_leads = instant_form + pixel_lead + pixel_custom
        result_event, result_count = _pick_result_event(action_map)
        acct_id = r.get("_account_id") or ""
        camp_id = _to_str(r.get("campaign_id")) or ""
        date = r.get("date_start") or ""
        country = r.get("country") or "(none)"
        rows.append({
            "daily_key": f"{acct_id}|{camp_id}|{date}|{country}",
            "account_id": acct_id,
            "account_label": account_label,
            "campaign_id": camp_id,
            "campaign_name": r.get("campaign_name"),
            "objective": r.get("objective"),
            "date": pd.to_datetime(date).date() if date else None,
            "country": country,
            "spend": float(r.get("spend") or 0),
            "impressions": int(r.get("impressions") or 0),
            "clicks": int(r.get("clicks") or 0),
            "total_leads": total_leads,
            "result_event": result_event,
            "result_count": result_count,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------
# GA4 normalizers
# ---------------------------------------------------------------------

def normalize_ga4_sessions(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for r in raw:
        date = r.get("date") or ""
        rows.append({
            "session_key": f"{date}|{r.get('session_source','')}|{r.get('session_medium','')}|{r.get('country','')}|{r.get('city','')}",
            "date": pd.to_datetime(date, format="%Y%m%d", errors="coerce").date() if date else None,
            "session_source": r.get("session_source"),
            "session_medium": r.get("session_medium"),
            "country": r.get("country"),
            "city": r.get("city"),
            "active_users": r.get("active_users", 0),
            "sessions": r.get("sessions", 0),
            "new_users": r.get("new_users", 0),
            "page_views": r.get("page_views", 0),
            "key_events": r.get("key_events", 0),
            "bounce_rate": r.get("bounce_rate", 0.0),
            "avg_session_duration": r.get("avg_session_duration", 0.0),
        })
    return pd.DataFrame(rows)


def normalize_ga4_daily(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for r in raw:
        date = r.get("date") or ""
        rows.append({
            "date": pd.to_datetime(date, format="%Y%m%d", errors="coerce").date() if date else None,
            "sessions": r.get("sessions", 0),
            "engaged_sessions": r.get("engaged_sessions", 0),
            "total_users": r.get("total_users", 0),
            "active_users": r.get("active_users", 0),
            "new_users": r.get("new_users", 0),
            "page_views": r.get("page_views", 0),
            "key_events": r.get("key_events", 0),
            "avg_session_duration": r.get("avg_session_duration", 0.0),
        })
    return pd.DataFrame(rows)


def normalize_ga4_pages(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for r in raw:
        date = r.get("date") or ""
        rows.append({
            "page_key": f"{date}|{r.get('page_path','')}|{r.get('country','')}",
            "date": pd.to_datetime(date, format="%Y%m%d", errors="coerce").date() if date else None,
            "page_path": r.get("page_path"),
            "country": r.get("country"),
            "page_views": r.get("page_views", 0),
            "active_users": r.get("active_users", 0),
        })
    return pd.DataFrame(rows)


def normalize_ga4_events(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for r in raw:
        date = r.get("date") or ""
        rows.append({
            "event_key": f"{date}|{r.get('event_name','')}|{r.get('country','')}",
            "date": pd.to_datetime(date, format="%Y%m%d", errors="coerce").date() if date else None,
            "event_name": r.get("event_name"),
            "country": r.get("country"),
            "event_count": r.get("event_count", 0),
            "total_users": r.get("total_users", 0),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------
# GSC normalizer
# ---------------------------------------------------------------------

def normalize_gsc(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for r in raw:
        date = r.get("date") or ""
        dim_name = r.get("dimension_name", "")
        dim_value = r.get("dimension_value", "")
        rows.append({
            "gsc_key": f"{date}|{dim_name}|{dim_value}",
            "date": pd.to_datetime(date).date() if date else None,
            "dimension_name": dim_name,
            "dimension_value": dim_value,
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr": r.get("ctr", 0.0),
            "position": r.get("position", 0.0),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------

def build_bridge(
    meta_leads: list[dict], contacts_df: pd.DataFrame
) -> pd.DataFrame:
    """Match Meta lead-form submissions to GHL contacts.
    Match on email first; fall back to last 9 digits of phone.
    Unmatched leads are dropped."""
    if contacts_df.empty or not meta_leads:
        return pd.DataFrame(columns=[
            "bridge_id", "meta_lead_id", "meta_ad_id", "meta_campaign_id",
            "meta_form_id", "contact_id", "match_method",
            "matched_email", "matched_phone",
            "meta_lead_created", "contact_created",
        ])
    # Build lookup tables once
    by_email = {
        (e or "").lower(): (cid, ts)
        for cid, e, ts in zip(
            contacts_df["contact_id"], contacts_df["email"], contacts_df["date_added"])
        if e
    }
    by_phone = {
        _last_n_digits(p): (cid, ts)
        for cid, p, ts in zip(
            contacts_df["contact_id"], contacts_df["phone"], contacts_df["date_added"])
        if p and len(_last_n_digits(p)) >= 8
    }

    rows = []
    matched = 0
    for L in meta_leads:
        # field_data is list of {name, values: [..]}
        email = None
        phone = None
        for fd in L.get("field_data") or []:
            n = (fd.get("name") or "").lower()
            v = fd.get("values") or []
            if not v:
                continue
            if "email" in n:
                email = v[0]
            elif "phone" in n:
                phone = v[0]

        cid = None
        method = None
        matched_email = None
        matched_phone = None
        if email:
            hit = by_email.get(email.lower())
            if hit:
                cid, _ = hit
                method = "email"; matched_email = email
        if not cid and phone:
            ph = _last_n_digits(phone)
            hit = by_phone.get(ph)
            if hit:
                cid, _ = hit
                method = "phone"; matched_phone = phone

        if not cid:
            continue
        meta_lead_id = _to_str(L.get("id")) or ""
        rows.append({
            "bridge_id": f"{meta_lead_id}|{cid}",
            "meta_lead_id": meta_lead_id,
            "meta_ad_id": _to_str(L.get("ad_id")),
            "meta_campaign_id": _to_str(L.get("campaign_id")),
            "meta_form_id": _to_str(L.get("form_id")),
            "contact_id": cid,
            "match_method": method,
            "matched_email": matched_email,
            "matched_phone": matched_phone,
            "meta_lead_created": _ts(L.get("created_time")),
            "contact_created": None,
        })
        matched += 1
    logger.info("Bridge: matched %d / %d leads (%.1f%%)",
                matched, len(meta_leads),
                100 * matched / max(len(meta_leads), 1))
    return pd.DataFrame(rows)
