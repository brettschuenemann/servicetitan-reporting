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
- Use plain markdown only — no LaTeX, no `$...$` math expressions, no `\frac{}{}`. Dollar signs go directly in the prose (e.g. "$10,500" or "USD 10,500") and must NOT be paired in ways a markdown renderer could mistake for math delimiters.

Methodology you must respect:
- Revenue numbers are from the ServiceTitan invoice ledger, reconciled with the accountant's "Total for Income" to within 0.5%.
- Maintenance contracts shown in the data are already inside invoice revenue — DO NOT add them on top.
- Lead source attribution only covers jobs created in ServiceTitan since March 2026 (~3 months); older revenue can't be source-attributed.
- Only reference numbers and facts present in the provided data. Do not invent figures.
- Don't summarize the data section back to the user — they have the data. Add interpretation, context, and recommendations on top of it.

Required sections (use these markdown headers):
1. **The Headline** — what's notable about the period
2. **What's Working** — wins, momentum, concentration
3. **What's Concerning** — risks, leaks, stale pipeline
4. **PPC Performance** — dedicated section on Pay Per Click using the PPC data provided. Interpret the *funnel* (estimates sent → won/lost/open, jobs paid vs $0, conversion rates). Comment on whether PPC is paying off, getting better/worse, or has a specific bottleneck (e.g. low estimate win rate vs low job conversion are different problems).
5. **Do This Week** — 2-3 specific actions tied to dollar amounts"""


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
            "SELECT c.name, "
            "  COUNT(*) jobs, "
            "  COUNT(*) FILTER (WHERE COALESCE(i.total,0) > 0) paid_jobs, "
            "  COALESCE(SUM(i.total),0) rev "
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

        # ---------- PPC funnel ----------
        # PPC jobs created in the window
        cur.execute(
            "SELECT "
            "  COUNT(*) jobs, "
            "  COUNT(*) FILTER (WHERE COALESCE(i.total,0) > 0) paid_jobs, "
            "  COALESCE(SUM(i.total),0) rev "
            "FROM jobs j JOIN campaigns c ON c.id = j.campaign_id "
            "LEFT JOIN invoices i ON i.id = j.invoice_id "
            "WHERE c.name = 'Pay Per Click (PPC)' "
            "  AND j.created_on >= %s AND j.created_on < %s",
            (start, end + timedelta(days=1)),
        )
        ppc_jobs_window = dict(cur.fetchone())

        # PPC estimates created in the window, by status
        cur.execute(
            "SELECT e.status_name, COUNT(*) n, COALESCE(SUM(e.subtotal),0) v "
            "FROM estimates e "
            "JOIN jobs j ON j.id = e.job_id "
            "JOIN campaigns c ON c.id = j.campaign_id "
            "WHERE c.name = 'Pay Per Click (PPC)' AND e.active = TRUE "
            "  AND e.created_on >= %s AND e.created_on < %s "
            "GROUP BY e.status_name",
            (start, end + timedelta(days=1)),
        )
        ppc_estimates_window = {r["status_name"]: dict(r) for r in cur.fetchall()}

        # PPC estimates currently open (any age) — the followup pile
        cur.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(e.subtotal),0) v, "
            "  AVG(EXTRACT(DAY FROM (NOW() - e.created_on))) avg_age, "
            "  MAX(EXTRACT(DAY FROM (NOW() - e.created_on))) oldest_age "
            "FROM estimates e "
            "JOIN jobs j ON j.id = e.job_id "
            "JOIN campaigns c ON c.id = j.campaign_id "
            "WHERE c.name = 'Pay Per Click (PPC)' AND e.active = TRUE "
            "  AND e.status_name = 'Open'",
        )
        ppc_open_now = dict(cur.fetchone())

        # PPC lifetime funnel (post-migration data only — Mar 2026 onwards)
        cur.execute(
            "SELECT e.status_name, COUNT(*) n, COALESCE(SUM(e.subtotal),0) v "
            "FROM estimates e "
            "JOIN jobs j ON j.id = e.job_id "
            "JOIN campaigns c ON c.id = j.campaign_id "
            "WHERE c.name = 'Pay Per Click (PPC)' AND e.active = TRUE "
            "GROUP BY e.status_name",
        )
        ppc_estimates_lifetime = {r["status_name"]: dict(r) for r in cur.fetchall()}

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
        "ppc_jobs_window": ppc_jobs_window,
        "ppc_estimates_window": ppc_estimates_window,
        "ppc_open_now": ppc_open_now,
        "ppc_estimates_lifetime": ppc_estimates_lifetime,
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
        lines.append(
            "(format: total jobs / paid jobs / conversion / avg per paid job / revenue. "
            "Many sources have lots of $0 invoices — estimates that didn't close, warranty work, or "
            "free callouts — so look at avg-per-paid-job for real per-job value, and at conversion "
            "for source quality.)"
        )
        for s in m["sources"]:
            paid = s["paid_jobs"]
            total_jobs = s["jobs"]
            conv = (paid / total_jobs * 100) if total_jobs else 0
            avg_paid = (float(s["rev"]) / paid) if paid else 0
            lines.append(
                f"- {s['name']}: {total_jobs} jobs / {paid} paid ({conv:.0f}% conv) / "
                f"${avg_paid:,.0f} avg per paid job / ${float(s['rev']):,.0f} revenue"
            )
    else:
        lines.append("- (no attributed jobs created in this window)")

    lines += [
        "",
        "NEW MAINTENANCE CONTRACTS IN WINDOW",
        f"- {m['new_maint']['n']} contracts, ${float(m['new_maint']['v']):,.0f} contract value (already inside invoice revenue above)",
    ]

    # ---------- PPC funnel section ----------
    pj = m["ppc_jobs_window"]
    pew = m["ppc_estimates_window"]
    pon = m["ppc_open_now"]
    pel = m["ppc_estimates_lifetime"]

    paid = int(pj["paid_jobs"] or 0)
    total_jobs = int(pj["jobs"] or 0)
    rev = float(pj["rev"] or 0)
    conv = (paid / total_jobs * 100) if total_jobs else 0
    avg_paid = (rev / paid) if paid else 0

    lines += [
        "",
        "PPC PERFORMANCE (last {} days — post-migration data only, Mar 2026+)".format(n),
        f"- Jobs created from PPC: {total_jobs}",
        f"  - Paid (invoiced > $0): {paid}  →  job→paid conversion: {conv:.0f}%",
        f"  - Revenue:              ${rev:,.0f}  (${avg_paid:,.0f} avg per paid job)",
        f"- Estimates SENT to PPC leads in window, by status:",
    ]
    if pew:
        for status, row in sorted(pew.items()):
            lines.append(
                f"    - {status}: {row['n']} estimates, ${float(row['v']):,.0f}"
            )
    else:
        lines.append("    - (no estimates created from PPC jobs in window)")

    open_now_n = int(pon["n"] or 0)
    open_now_v = float(pon["v"] or 0)
    avg_age = float(pon["avg_age"] or 0)
    oldest_age = int(pon["oldest_age"] or 0)
    lines += [
        f"- Open PPC estimates RIGHT NOW (any age): {open_now_n} estimates, "
        f"${open_now_v:,.0f} pipeline, avg age {avg_age:.0f} days (oldest: {oldest_age}d)",
        f"- Lifetime PPC estimate outcomes (since Mar 2026):",
    ]
    if pel:
        total_lifetime = sum(int(r["n"]) for r in pel.values())
        sold_n = int(pel.get("Sold", {}).get("n") or 0)
        sold_v = float(pel.get("Sold", {}).get("v") or 0)
        win_rate = (sold_n / total_lifetime * 100) if total_lifetime else 0
        for status, row in sorted(pel.items()):
            lines.append(
                f"    - {status}: {row['n']} estimates, ${float(row['v']):,.0f}"
            )
        lines.append(
            f"  → PPC estimate-to-sale win rate (lifetime): {win_rate:.0f}% "
            f"(${sold_v:,.0f} sold)"
        )
    else:
        lines.append("    - (no PPC estimates on record)")

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
    # Streamlit's markdown renderer treats unescaped $ pairs as LaTeX math.
    # Escape every $ so dollar amounts render literally.
    text = text.replace("$", r"\$")
    return text, user_brief
