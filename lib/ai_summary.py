"""AI-generated weekly business summary, powered by Claude.

Pulls a structured set of metrics from the Postgres cache for the last N days,
feeds them to Claude with a tight system prompt, and returns markdown.

Cost is small (~$0.04/summary on Opus 4.7) and Streamlit cache_data keeps
generation infrequent. ANTHROPIC_API_KEY is read from st.secrets first,
then os.environ.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import anthropic
import streamlit as st

from .database import db

SUMMARY_DAYS = 14  # rolling window — matches what we showed in chat earlier

_SYSTEM_PROMPT = """You are a senior data analyst writing executive summaries for Pure Air, an HVAC service business in the Chicago area. Your audience is the business owner — they want signal, not numbers they already have on a dashboard.

Style:
- 200-300 words, markdown formatted
- Lead with what's notable — a sharp change, concentration, or risk
- Specific (customer names, dollar amounts, dates) — never vague
- Surface concentration risks (one or two customers driving most of the week's revenue, big jobs that need protecting)
- Spot trends, not just point-in-time numbers
- Call out concerning signals worth investigating
- End with 2-3 specific, actionable items tied to dollar amounts

Methodology you must respect:
- Revenue numbers are from the ServiceTitan invoice ledger, reconciled with the accountant's "Total for Income" to within 0.5%.
- Maintenance contracts shown in the data are already inside invoice revenue — DO NOT add them on top.
- Lead source attribution only covers jobs created in ServiceTitan since March 2026 (~3 months); older revenue can't be source-attributed.
- Only reference numbers and facts present in the provided data. Do not invent figures.
- Don't summarize the data section back to the user — they have the data. Add interpretation, context, and recommendations on top of it."""


def _gather_metrics(end: date, window_days: int = SUMMARY_DAYS) -> dict:
    """Pull all the numbers Claude needs to write the summary."""
    start = end - timedelta(days=window_days - 1)
    prior_start = start - timedelta(days=window_days)
    prior_end = start - timedelta(days=1)
    ly_start = date(start.year - 1, start.month, start.day)
    ly_end = date(end.year - 1, end.month, end.day)

    with db() as conn, conn.cursor() as cur:
        def sum_inv(s, e):
            cur.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(total),0) r FROM invoices "
                "WHERE invoice_date BETWEEN %s AND %s",
                (s, e),
            )
            return dict(cur.fetchone())

        current = sum_inv(start, end)
        prior = sum_inv(prior_start, prior_end)
        ly = sum_inv(ly_start, ly_end)

        cur.execute(
            "SELECT invoice_date, SUM(total) rev, COUNT(*) n FROM invoices "
            "WHERE invoice_date BETWEEN %s AND %s GROUP BY invoice_date "
            "ORDER BY rev DESC LIMIT 3",
            (start, end),
        )
        best_days = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT MIN(customer_name) name, COUNT(*) n, SUM(total) rev "
            "FROM invoices WHERE invoice_date BETWEEN %s AND %s "
            "AND customer_name IS NOT NULL "
            "GROUP BY customer_id ORDER BY rev DESC LIMIT 3",
            (start, end),
        )
        top_customers = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT customer_name, total, summary FROM invoices "
            "WHERE invoice_date BETWEEN %s AND %s ORDER BY total DESC LIMIT 3",
            (start, end),
        )
        biggest = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(subtotal),0) v FROM estimates "
            "WHERE status_name='Open' AND active=TRUE "
            "AND created_on >= %s AND created_on < %s",
            (start, end + timedelta(days=1)),
        )
        new_open = dict(cur.fetchone())

        cur.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(subtotal),0) v FROM estimates "
            "WHERE status_name='Open' AND active=TRUE AND created_on < %s",
            (start,),
        )
        stale_open = dict(cur.fetchone())

        cur.execute(
            "SELECT c.name, COUNT(*) jobs, COALESCE(SUM(i.total),0) rev "
            "FROM jobs j JOIN campaigns c ON c.id = j.campaign_id "
            "LEFT JOIN invoices i ON i.id = j.invoice_id "
            "WHERE c.name <> 'Imported Default Campaign' "
            "AND j.created_on >= %s AND j.created_on < %s "
            "GROUP BY c.name ORDER BY rev DESC",
            (start, end + timedelta(days=1)),
        )
        sources = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(billing_amount),0) v FROM memberships "
            "WHERE from_date BETWEEN %s AND %s",
            (start, end),
        )
        new_maint = dict(cur.fetchone())

    return {
        "window_days": window_days,
        "start": start,
        "end": end,
        "current": current,
        "prior": prior,
        "ly": ly,
        "best_days": best_days,
        "top_customers": top_customers,
        "biggest": biggest,
        "new_open": new_open,
        "stale_open": stale_open,
        "sources": sources,
        "new_maint": new_maint,
    }


def _format_user_prompt(m: dict) -> str:
    """Turn the metrics dict into a plain-text brief for the LLM."""
    def pct(new, old):
        if not old:
            return "n/a"
        return f"{(new - old) / old * 100:+.0f}%"

    cur = m["current"]
    prior = m["prior"]
    ly = m["ly"]
    n = m["window_days"]

    lines = [
        f"Generate a {n}-day executive summary for Pure Air, period {m['start']} → {m['end']}.",
        f"Today is {date.today().strftime('%A, %B %d, %Y')}.",
        "",
        "REVENUE",
        f"- Last {n} days:    ${float(cur['r']):>10,.0f} across {cur['n']:>4} invoices",
        f"- Prior {n} days:   ${float(prior['r']):>10,.0f}  ({pct(cur['r'], prior['r'])} vs prior period)",
        f"- Same {n} days '25: ${float(ly['r']):>10,.0f}  ({pct(cur['r'], ly['r'])} year-over-year)",
        "",
        "BEST DAYS (top 3)",
    ]
    for d in m["best_days"]:
        lines.append(
            f"- {d['invoice_date'].strftime('%a %b %d')}: ${float(d['rev']):,.0f} across {d['n']} invoices"
        )

    lines += ["", "TOP CUSTOMERS (top 3)"]
    for c in m["top_customers"]:
        lines.append(
            f"- {(c['name'] or 'unknown')[:32]}: ${float(c['rev']):,.0f} ({c['n']} invoices)"
        )

    lines += ["", "BIGGEST INVOICES (top 3, with work summary)"]
    for b in m["biggest"]:
        summary = (b["summary"] or "")[:80]
        lines.append(
            f"- ${float(b['total']):,.0f}  {(b['customer_name'] or '')[:25]}: {summary}"
        )

    lines += [
        "",
        "ESTIMATE PIPELINE",
        f"- New open estimates (last {n}d): {m['new_open']['n']} worth ${float(m['new_open']['v']):,.0f}",
        f"- Stale open (created >14d ago): {m['stale_open']['n']} worth ${float(m['stale_open']['v']):,.0f}",
        "",
        "LEAD SOURCES (post-migration jobs only — Mar 2026+)",
    ]
    if m["sources"]:
        for s in m["sources"]:
            lines.append(
                f"- {s['name']}: {s['jobs']} jobs, ${float(s['rev']):,.0f}"
            )
    else:
        lines.append("- (no attributed jobs created in this window)")

    lines += [
        "",
        "NEW MAINTENANCE CONTRACTS IN WINDOW",
        f"- {m['new_maint']['n']} contracts, ${float(m['new_maint']['v']):,.0f} contract value (already inside invoice revenue above)",
    ]

    return "\n".join(lines)


def _anthropic_key() -> str | None:
    """Read ANTHROPIC_API_KEY from st.secrets first, then env."""
    try:
        v = st.secrets.get("ANTHROPIC_API_KEY")
        if v:
            return str(v)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


@st.cache_data(ttl=4 * 3600, show_spinner=False)
def generate_summary(today_iso: str) -> tuple[str, str]:
    """Generate the AI summary for the given day. Cached for 4 hours.

    Returns (markdown_summary, raw_metrics_brief). The `today_iso` arg is the
    cache key — same day = cached result; new day = regenerated.
    """
    api_key = _anthropic_key()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (local) or "
            ".streamlit/secrets.toml (cloud). Get one from console.anthropic.com."
        )

    end = date.fromisoformat(today_iso)
    metrics = _gather_metrics(end)
    user_brief = _format_user_prompt(metrics)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_brief}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return text, user_brief
