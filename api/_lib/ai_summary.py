"""Compact, AI-agent-friendly digest of the executive scorecards.

Reuses the SAME templated views the dashboard cards run (dashboards/sql/
executive_cards.sql via queries.run_view) — no new SQL, no new data source.
Built for a GHL AI Studio Custom Action: the agent calls /api/ai-summary and
gets a few numbers plus a ready-to-speak sentence, instead of raw tables.
"""
from __future__ import annotations

from .queries import resolve_period, run_view

# key -> (view name, field to read off the 'current'/'prior' rows)
_CARDS: dict[str, tuple[str, str]] = {
    "leads":     ("vw_card_total_leads_main", "leads"),
    "cpl":       ("vw_card_avg_cpl_main", "cpl"),
    "ad_spend":  ("vw_card_ad_spend", "spend"),
    "revenue":   ("vw_card_revenue", "revenue"),
    "show_rate": ("vw_card_show_rate", "show_rate"),
    "coes":      ("vw_card_coes", "coes"),
}


def _delta_pct(current, prior) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return round((current - prior) / prior * 100, 1)


def _fmt_money(v) -> str:
    return f"${v:,.0f}" if v is not None else "n/a"


def _fmt_pct(v) -> str:
    return f"{v * 100:.0f}%" if v is not None else "n/a"


def _fmt_change(pct) -> str:
    if pct is None:
        return ""
    sign = "+" if pct >= 0 else ""
    return f" ({sign}{pct:.0f}% vs prior period)"


def build_summary(period: str, since: str | None, until: str | None, city: str) -> dict:
    """Returns {period, since, until, city, metrics, summary_text}."""
    binds = resolve_period(period, since, until)
    binds["city"] = city

    metrics: dict[str, dict] = {}
    for key, (view, field) in _CARDS.items():
        rows = run_view(view, binds)
        cur = (rows.get("current") or {}).get(field)
        pri = (rows.get("prior") or {}).get(field)
        metrics[key] = {"current": cur, "prior": pri, "change_pct": _delta_pct(cur, pri)}

    leads, cpl = metrics["leads"], metrics["cpl"]
    spend, revenue = metrics["ad_spend"], metrics["revenue"]
    show_rate, coes = metrics["show_rate"], metrics["coes"]

    summary_text = (
        f"{city} — {period} ({binds['since']} to {binds['until']}): "
        f"{leads['current'] if leads['current'] is not None else 'n/a'} leads"
        f"{_fmt_change(leads['change_pct'])}, "
        f"avg CPL {_fmt_money(cpl['current'])}, "
        f"ad spend {_fmt_money(spend['current'])}{_fmt_change(spend['change_pct'])}, "
        f"revenue {_fmt_money(revenue['current'])}{_fmt_change(revenue['change_pct'])}, "
        f"show rate {_fmt_pct(show_rate['current'])}, "
        f"COEs {coes['current'] if coes['current'] is not None else 'n/a'}."
    )

    return {
        "period": period,
        "since": binds["since"],
        "until": binds["until"],
        "city": city,
        "metrics": metrics,
        "summary_text": summary_text,
    }
