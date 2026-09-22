"""The Migration Dashboard — data API (Vercel Python Function).

A plain ASGI app with NO web framework, deliberately: Vercel's Python framework
presets (FastAPI/Flask/Django) take precedence over file-based /api functions and
would swallow every request, including the Next.js frontend's. Framework-free
keeps the Next.js preset in charge of the project and this file serving /api/*.

Routes (all under /api, see the rewrite in vercel.json):
    GET  /api/health   data-source health + freshness
    GET  /api/meta     filter options, tab list, view catalogue
    POST /api/login    exchange the shared password for a session cookie
    POST /api/query    run one or more views and return their rows
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import date, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import auth                                              # noqa: E402
from _lib.db import DataSourceUnavailable, health, last_refreshed  # noqa: E402
from _lib.queries import (                                         # noqa: E402
    CITIES, PERIODS, df_to_payload, load_queries, resolve_period, run_df, run_view,
)

TAB_NAMES = [
    "Executive", "Meta Ads", "Funnels", "Counsellors", "SEO & Traffic",
    "Forecast & Goals", "Upload Reports", "Sales Team Perf.", "WBR",
    "Weekly Report", "Breakdown",
]

# A single request may ask for several views (a tab's scorecard row is ~6 views).
# Cap it so one request can't hold the function open for its whole 300s budget.
MAX_VIEWS_PER_REQUEST = 25


# ---------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------

def route_health():
    try:
        health()
    except DataSourceUnavailable as e:
        return 503, {
            "ok": False,
            "lapsed": e.lapsed,
            "error": (
                "Data source unavailable - the MotherDuck plan has lapsed. An admin "
                "needs to choose a plan (Free is enough) at https://app.motherduck.com"
                if e.lapsed else
                "Data source (MotherDuck) is temporarily unavailable. Please retry."
            ),
        }
    ts = last_refreshed()
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = None
    return 200, {
        "ok": True,
        "last_refreshed": ts.isoformat() if isinstance(ts, (datetime, date)) else None,
        "server_time": datetime.now().isoformat(),
    }


def route_meta():
    return 200, {
        "tabs": TAB_NAMES,
        "cities": CITIES,
        "periods": PERIODS,
        "views": sorted(load_queries().keys()),
    }


def route_login(body):
    if auth.auth_disabled():
        return 200, {"ok": True, "note": "auth disabled - no DASHBOARD_PASSWORD set"}, []
    if not auth.check_password(str(body.get("password") or "")):
        return 401, {"ok": False, "error": "Incorrect password"}, []
    token = auth.issue()
    secure = "" if os.getenv("VERCEL_ENV") is None else " Secure;"
    cookie = (
        f"{auth.COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax;"
        f"{secure} Max-Age={auth.TTL_SECONDS}"
    )
    return 200, {"ok": True}, [("set-cookie", cookie)]


def route_query(body):
    """Body:
        {
          "period": "Current month" | ... ,
          "since": "YYYY-MM-DD",     # only for period == "Custom"
          "until": "YYYY-MM-DD",
          "city":  "All" | "Melbourne" | ...,
          "views": [ {"key": "leads", "view": "vw_exec1_...", "kind": "card"|"table",
                      "binds": {...optional extra binds...}} ]
        }

    Returns {"binds": {...}, "results": {key: payload}} where payload is either
    {"current": {...}, "prior": {...}} for kind="card" or {"columns", "rows"} for
    kind="table". A failing view yields {"error": ...} for that key alone, so one
    broken card never blanks the whole tab.
    """
    period = str(body.get("period") or "Current month")
    binds = resolve_period(period, body.get("since"), body.get("until"))
    binds["city"] = str(body.get("city") or "All")

    requested = body.get("views") or []
    if not isinstance(requested, list):
        return 400, {"error": "'views' must be a list"}
    if len(requested) > MAX_VIEWS_PER_REQUEST:
        return 400, {"error": f"Too many views (max {MAX_VIEWS_PER_REQUEST})"}

    results = {}
    for spec in requested:
        if not isinstance(spec, dict):
            continue
        view = str(spec.get("view") or "")
        key = str(spec.get("key") or view)
        kind = str(spec.get("kind") or "table")
        merged = dict(binds)
        extra = spec.get("binds")
        if isinstance(extra, dict):
            merged.update({str(k): v for k, v in extra.items()})
        try:
            results[key] = (
                run_view(view, merged) if kind == "card"
                else df_to_payload(run_df(view, merged))
            )
        except KeyError as e:
            results[key] = {"error": str(e)}
        except DataSourceUnavailable as e:
            return 503, {"error": str(e), "lapsed": e.lapsed}
        except Exception as e:
            results[key] = {"error": f"Query failed for {view}: {e}"}
    return 200, {"binds": binds, "period": period, "results": results}


# ---------------------------------------------------------------------
# Minimal ASGI plumbing
# ---------------------------------------------------------------------

async def _read_body(receive):
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _authorized(scope):
    if auth.auth_disabled():
        return True
    raw = ""
    for name, value in scope.get("headers", []):
        if name.decode().lower() == "cookie":
            raw = value.decode()
            break
    if not raw:
        return False
    jar = SimpleCookie()
    jar.load(raw)
    morsel = jar.get(auth.COOKIE_NAME)
    return auth.verify(morsel.value if morsel else None)


async def _send_json(send, status, payload, headers=None):
    raw = json.dumps(payload, default=str).encode()
    out = [
        (b"content-type", b"application/json"),
        # Queries hit MotherDuck, which the ETL only refreshes hourly. A short
        # shared cache absorbs tab-switching and multiple viewers without a
        # round-trip each, matching the Streamlit app's 10-minute cache TTL.
        (b"cache-control", b"private, max-age=0, s-maxage=600, stale-while-revalidate=60"),
    ]
    for k, v in (headers or []):
        out.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": out})
    await send({"type": "http.response.body", "body": raw})


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    path = scope["path"].rstrip("/") or "/api"
    method = scope["method"].upper()

    # Vercel rewrites /api/* here, so the sub-route is the tail of the path.
    route = path[len("/api"):].lstrip("/") if path.startswith("/api") else path.lstrip("/")
    if not route:
        route = parse_qs(scope.get("query_string", b"").decode()).get("route", [""])[0]

    try:
        if route == "login" and method == "POST":
            body = json.loads(await _read_body(receive) or b"{}")
            status, payload, headers = route_login(body)
            return await _send_json(send, status, payload, headers)

        if not _authorized(scope):
            return await _send_json(send, 401, {"error": "Not authenticated"})

        if route == "health":
            status, payload = route_health()
            return await _send_json(send, status, payload)

        if route == "meta":
            status, payload = route_meta()
            return await _send_json(send, status, payload)

        if route == "query" and method == "POST":
            body = json.loads(await _read_body(receive) or b"{}")
            status, payload = route_query(body)
            return await _send_json(send, status, payload)

        return await _send_json(send, 404, {"error": f"No such route: /api/{route}"})

    except json.JSONDecodeError:
        return await _send_json(send, 400, {"error": "Request body is not valid JSON"})
    except DataSourceUnavailable as e:
        return await _send_json(send, 503, {"error": str(e), "lapsed": e.lapsed})
    except Exception as e:
        # Never leak a stack trace to the browser; log it for `vercel logs`.
        traceback.print_exc()
        return await _send_json(send, 500, {"error": f"Internal error: {type(e).__name__}: {e}"})
