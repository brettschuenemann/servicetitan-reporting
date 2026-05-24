"""End-of-day progress report for Brett — how Fey did against today's call list.

Measures adherence to the morning CSR email by joining today's
`csr_recommendations` rows against `calls`, `invoices`, and `memberships`
to see what actually happened.

The report:
  - Top scorecard: contact rate, conversation rate, conversions
  - Per-section breakdown: who was contacted, who wasn't
  - 7-day trend table
  - Top untouched leads (still pending, no outreach detected)
  - Hot signals: any customer who called us back today

Sends to EMAIL_TO (Brett). Designed for GitHub Actions cron at 22:00 UTC
weekdays. Manual run:
  python scripts/send_csr_progress_report.py
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from lib.database import db  # noqa: E402
from lib.email_utils import header as fmt_recipients, parse_recipients  # noqa: E402

REQUIRED = ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO", "DATABASE_URL")
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    sys.exit(f"Missing env vars: {', '.join(missing)}")

# Same thresholds as the morning email so badges stay consistent.
CONVERSATION_DURATION = 90
VOICEMAIL_MIN_DURATION = 15

# Brand colors (matches lib/style.py).
NAVY, PRIMARY, GREEN, AMBER, RED = "#00214D", "#0066EE", "#10B981", "#F59E0B", "#EF4444"


# ---------- helpers ----------

def pct(numer: int, denom: int) -> str:
    return f"{(numer / denom * 100):.0f}%" if denom else "—"


def fmt_money(v) -> str:
    return f"${float(v or 0):,.0f}"


# ---------- data ----------

def load_today_state(conn) -> dict:
    """Everything needed for the scorecard + per-section breakdown."""
    with conn.cursor() as cur:
        # Today's recommendations (in Chicago time). Group by kind + customer
        # so multi-kind recs for the same customer count once per section.
        # Note: MIN(jsonb) isn't a valid aggregate in Postgres — array_agg
        # gives us any one payload (we don't care which for display purposes).
        cur.execute(
            """
            SELECT
              kind,
              customer_id,
              dedup_key,
              MIN(sent_at) AS first_sent_today,
              (ARRAY_AGG(payload ORDER BY sent_at))[1] AS payload
            FROM csr_recommendations
            WHERE (sent_at AT TIME ZONE 'America/Chicago')::date =
                  (NOW()   AT TIME ZONE 'America/Chicago')::date
            GROUP BY kind, customer_id, dedup_key
            """
        )
        recs = [dict(r) for r in cur.fetchall()]

        if not recs:
            return {"recs": [], "outreach": {}, "bookings": {}, "enrollments": set(), "inbound": set()}

        cust_ids = list({r["customer_id"] for r in recs if r["customer_id"]})

        # Outreach since the start of today (Chicago time) for these customers.
        cur.execute(
            f"""
            SELECT
              customer_id,
              COUNT(*) FILTER (WHERE direction='Outbound')                                AS attempts,
              COUNT(*) FILTER (WHERE direction='Outbound'
                               AND duration_seconds >= {CONVERSATION_DURATION})            AS conversations,
              COUNT(*) FILTER (WHERE direction='Outbound'
                               AND duration_seconds >= {VOICEMAIL_MIN_DURATION}
                               AND duration_seconds <  {CONVERSATION_DURATION})            AS voicemails,
              COUNT(*) FILTER (WHERE direction='Outbound'
                               AND COALESCE(duration_seconds,0) < {VOICEMAIL_MIN_DURATION}) AS no_answers,
              COUNT(*) FILTER (WHERE direction='Inbound')                                  AS inbound,
              MAX(created_on) FILTER (WHERE direction='Outbound')                          AS last_outbound,
              MAX(created_on) FILTER (WHERE direction='Inbound')                           AS last_inbound
            FROM calls
            WHERE customer_id = ANY(%s)
              AND created_on >= (DATE_TRUNC('day', NOW() AT TIME ZONE 'America/Chicago')
                                  AT TIME ZONE 'America/Chicago')
            GROUP BY customer_id
            """,
            (cust_ids,),
        )
        outreach = {int(r["customer_id"]): dict(r) for r in cur.fetchall()}

        # Conversions today: invoices for any recommended customer.
        cur.execute(
            """
            SELECT customer_id, COUNT(*) AS jobs, SUM(total) AS revenue
            FROM invoices
            WHERE customer_id = ANY(%s)
              AND invoice_date = (NOW() AT TIME ZONE 'America/Chicago')::date
              AND total > 0
            GROUP BY customer_id
            """,
            (cust_ids,),
        )
        bookings = {int(r["customer_id"]): dict(r) for r in cur.fetchall()}

        # Membership enrollments today: any active membership created today.
        cur.execute(
            """
            SELECT DISTINCT customer_id
            FROM memberships
            WHERE customer_id = ANY(%s)
              AND status = 'Active'
              AND (created_on AT TIME ZONE 'America/Chicago')::date =
                  (NOW()       AT TIME ZONE 'America/Chicago')::date
            """,
            (cust_ids,),
        )
        enrollments = {int(r["customer_id"]) for r in cur.fetchall()}

        # Hot signal: any recommended customer who called us today
        inbound = {cid for cid, ot in outreach.items() if (ot.get("inbound") or 0) > 0}

    return {
        "recs": recs,
        "outreach": outreach,
        "bookings": bookings,
        "enrollments": enrollments,
        "inbound": inbound,
    }


def load_7day_trend(conn) -> list[dict]:
    """Per-day adherence over the last 7 days (today included)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH days AS (
              SELECT generate_series(
                (NOW() AT TIME ZONE 'America/Chicago')::date - 6,
                (NOW() AT TIME ZONE 'America/Chicago')::date,
                '1 day'::interval
              )::date AS day
            ),
            recs AS (
              SELECT
                (sent_at AT TIME ZONE 'America/Chicago')::date AS day,
                customer_id,
                kind
              FROM csr_recommendations
              WHERE sent_at >= (NOW() AT TIME ZONE 'America/Chicago')::date - 7
                AND customer_id IS NOT NULL
            ),
            day_totals AS (
              SELECT day, COUNT(DISTINCT customer_id) AS recommended
              FROM recs GROUP BY day
            ),
            contact AS (
              SELECT
                (c.created_on AT TIME ZONE 'America/Chicago')::date AS day,
                r.customer_id,
                MAX(c.duration_seconds) AS max_dur
              FROM calls c
              JOIN recs r
                ON r.customer_id = c.customer_id
               AND (c.created_on AT TIME ZONE 'America/Chicago')::date = r.day
              WHERE c.direction = 'Outbound'
              GROUP BY day, r.customer_id
            ),
            day_contact AS (
              SELECT
                day,
                COUNT(DISTINCT customer_id) AS contacted,
                COUNT(DISTINCT customer_id) FILTER (WHERE max_dur >= {CONVERSATION_DURATION}) AS spoke
              FROM contact GROUP BY day
            )
            SELECT
              d.day,
              COALESCE(dt.recommended, 0) AS recommended,
              COALESCE(dc.contacted, 0)   AS contacted,
              COALESCE(dc.spoke, 0)       AS spoke
            FROM days d
            LEFT JOIN day_totals  dt ON dt.day = d.day
            LEFT JOIN day_contact dc ON dc.day = d.day
            ORDER BY d.day
            """
        )
        return [dict(r) for r in cur.fetchall()]


def load_untouched_backlog(conn, max_age_days: int = 21) -> list[dict]:
    """Customers recommended 3+ days ago with no outreach detected since.

    Surfaces the persistent gaps — leads that have been on the list multiple
    times and Fey still hasn't engaged them.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH first_recs AS (
              SELECT kind, customer_id,
                     MIN(sent_at) AS first_sent,
                     COUNT(*)     AS times_recommended
              FROM csr_recommendations
              WHERE sent_at >= NOW() - %s * INTERVAL '1 day'
                AND customer_id IS NOT NULL
              GROUP BY kind, customer_id
            ),
            outreach AS (
              SELECT c.customer_id,
                     COUNT(*) FILTER (WHERE c.direction='Outbound') AS attempts,
                     MAX(c.created_on) FILTER (WHERE c.direction='Outbound') AS last_outbound
              FROM calls c
              JOIN first_recs fr ON fr.customer_id = c.customer_id
              WHERE c.created_on >= fr.first_sent
              GROUP BY c.customer_id
            ),
            cust_meta AS (
              SELECT customer_id,
                     MIN(customer_name) AS name,
                     SUM(total)         AS lifetime
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            )
            SELECT
              fr.kind, fr.customer_id, fr.first_sent, fr.times_recommended,
              COALESCE(cm.name, 'Customer ' || fr.customer_id::text) AS name,
              COALESCE(cm.lifetime, 0) AS lifetime,
              COALESCE(o.attempts, 0)  AS attempts
            FROM first_recs fr
            LEFT JOIN outreach  o  ON o.customer_id  = fr.customer_id
            LEFT JOIN cust_meta cm ON cm.customer_id = fr.customer_id
            WHERE COALESCE(o.attempts, 0) = 0
              AND fr.times_recommended >= 2
              AND fr.first_sent <= NOW() - INTERVAL '3 days'
            ORDER BY cm.lifetime DESC NULLS LAST, fr.times_recommended DESC
            LIMIT 10
            """,
            (max_age_days,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- aggregation ----------

def aggregate(state: dict) -> dict:
    """Per-section + overall counts."""
    by_kind: dict[str, dict] = {}
    for r in state["recs"]:
        kind = r["kind"]
        cid = r["customer_id"]
        bucket = by_kind.setdefault(kind, {
            "recommended": 0, "contacted": 0, "spoke": 0, "voicemail": 0,
            "no_answer": 0, "inbound": 0, "booked": 0, "enrolled": 0,
            "untouched_rows": [],
        })
        bucket["recommended"] += 1
        ot = state["outreach"].get(cid, {})
        attempts = ot.get("attempts") or 0
        if attempts > 0:
            bucket["contacted"] += 1
        if (ot.get("conversations") or 0) > 0:
            bucket["spoke"] += 1
        elif (ot.get("voicemails") or 0) > 0:
            bucket["voicemail"] += 1
        elif (ot.get("no_answers") or 0) > 0:
            bucket["no_answer"] += 1
        if cid and cid in state["inbound"]:
            bucket["inbound"] += 1
        if cid and cid in state["bookings"]:
            bucket["booked"] += 1
        if cid and cid in state["enrollments"]:
            bucket["enrolled"] += 1
        if attempts == 0:
            # Pull the customer name out of the payload snapshot if present.
            payload = r.get("payload") or {}
            name = payload.get("customer_name") if isinstance(payload, dict) else None
            bucket["untouched_rows"].append({
                "customer_id": cid,
                "name": name or f"Customer {cid}",
            })

    overall = {
        "recommended": sum(b["recommended"] for b in by_kind.values()),
        "contacted":   sum(b["contacted"]   for b in by_kind.values()),
        "spoke":       sum(b["spoke"]       for b in by_kind.values()),
        "voicemail":   sum(b["voicemail"]   for b in by_kind.values()),
        "no_answer":   sum(b["no_answer"]   for b in by_kind.values()),
        "inbound":     sum(b["inbound"]     for b in by_kind.values()),
        "booked":      sum(b["booked"]      for b in by_kind.values()),
        "enrolled":    sum(b["enrolled"]    for b in by_kind.values()),
    }
    return {"by_kind": by_kind, "overall": overall}


# ---------- HTML ----------

KIND_LABELS = {
    "membership": "🤝 Membership opportunities",
    "sleeping":   "💤 Sleeping customers",
    "missed":     "📞 Missed-call followups",
}


def render_scorecard(overall: dict) -> str:
    recommended = overall["recommended"]
    contact_rate = pct(overall["contacted"], recommended)
    spoke_rate   = pct(overall["spoke"], recommended)
    conv_count   = overall["booked"] + overall["enrolled"]

    def kpi(label, value, sub=""):
        return f"""
<td style="padding:14px 18px;border:1px solid #e5e7eb;background:white;border-radius:8px;min-width:140px;text-align:center">
  <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.04em;font-weight:500">{label}</div>
  <div style="font-size:24px;font-weight:700;color:{NAVY};margin-top:2px">{value}</div>
  <div style="font-size:11px;color:#6b7280;margin-top:2px">{sub}</div>
</td>"""

    return f"""
<table cellspacing="8" cellpadding="0" style="border-collapse:separate;margin-bottom:16px">
  <tr>
    {kpi("Recommended", recommended, "today's list size")}
    {kpi("Contacted", f"{overall['contacted']} / {recommended}", f"contact rate {contact_rate}")}
    {kpi("Spoke", overall['spoke'], f"conversation rate {spoke_rate}")}
    {kpi("Conversions", conv_count, f"{overall['booked']} booked · {overall['enrolled']} enrolled")}
  </tr>
</table>
"""


def render_kind_card(kind: str, bucket: dict) -> str:
    label = KIND_LABELS.get(kind, kind)
    recommended = bucket["recommended"]
    contact_rate = pct(bucket["contacted"], recommended)
    breakdown = (
        f"<b style='color:{GREEN}'>{bucket['spoke']}</b> spoke · "
        f"<b style='color:{AMBER}'>{bucket['voicemail']}</b> voicemail · "
        f"<b style='color:{RED}'>{bucket['no_answer']}</b> no answer · "
        f"<b style='color:#6b7280'>{recommended - bucket['contacted']}</b> untouched"
    )
    convs = []
    if bucket["enrolled"]:  convs.append(f"<b style='color:{GREEN}'>✅ {bucket['enrolled']} enrolled</b>")
    if bucket["booked"]:    convs.append(f"<b style='color:{GREEN}'>✅ {bucket['booked']} booked</b>")
    if bucket["inbound"]:   convs.append(f"<b style='color:{PRIMARY}'>📥 {bucket['inbound']} called us back</b>")
    conv_line = " · ".join(convs) if convs else "<span style='color:#9ca3af'>no conversions yet today</span>"

    untouched = bucket["untouched_rows"]
    if untouched:
        names = ", ".join(escape(r["name"]) for r in untouched[:6])
        suffix = f" (+{len(untouched)-6} more)" if len(untouched) > 6 else ""
        untouched_html = (
            f"<div style='margin-top:8px;font-size:13px;color:#7f1d1d;"
            f"background:#fef2f2;border-radius:6px;padding:8px 10px'>"
            f"<b>Untouched:</b> {names}{suffix}</div>"
        )
    else:
        untouched_html = ""

    return f"""
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:14px 18px;margin-bottom:12px;background:white">
  <div style="font-size:16px;font-weight:700;color:{NAVY};margin-bottom:6px">{label}</div>
  <div style="font-size:13px;color:#374151;margin-bottom:4px">
    <b>{bucket['contacted']} / {recommended}</b> contacted ({contact_rate}) — {breakdown}
  </div>
  <div style="font-size:13px;color:#374151">{conv_line}</div>
  {untouched_html}
</div>
"""


def render_trend(trend: list[dict]) -> str:
    rows = []
    for d in trend:
        day = d["day"]
        rec = int(d["recommended"] or 0)
        contacted = int(d["contacted"] or 0)
        spoke = int(d["spoke"] or 0)
        rate = pct(contacted, rec)
        # tiny bar = spoke contribution within contacted (visual signal)
        bar_w = int((contacted / rec) * 120) if rec else 0
        spoke_w = int((spoke / rec) * 120) if rec else 0
        bar = (
            f"<div style='width:120px;height:8px;background:#f3f4f6;border-radius:4px;display:inline-block;vertical-align:middle'>"
            f"  <div style='width:{bar_w}px;height:8px;background:{AMBER};border-radius:4px;position:relative'>"
            f"    <div style='width:{spoke_w}px;height:8px;background:{GREEN};border-radius:4px'></div>"
            f"  </div>"
            f"</div>"
        )
        weekday = day.strftime("%a")
        rows.append(
            f"<tr><td style='padding:4px 8px;color:#6b7280;font-size:12px'>{weekday} {day:%m/%d}</td>"
            f"<td style='padding:4px 8px;text-align:right;font-size:13px'>{rec}</td>"
            f"<td style='padding:4px 8px;text-align:right;font-size:13px'><b>{contacted}</b></td>"
            f"<td style='padding:4px 8px;text-align:right;font-size:13px;color:{GREEN}'>{spoke}</td>"
            f"<td style='padding:4px 8px'>{bar}</td>"
            f"<td style='padding:4px 8px;text-align:right;font-size:13px'>{rate}</td></tr>"
        )
    return f"""
<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;border:1px solid #e5e7eb;border-radius:8px;background:white;font-size:13px">
  <thead style="background:#f9fafb">
    <tr>
      <th align="left"  style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Day</th>
      <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Rec'd</th>
      <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Contacted</th>
      <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Spoke</th>
      <th align="left"  style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Bar</th>
      <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Rate</th>
    </tr>
  </thead>
  <tbody>{''.join(rows)}</tbody>
</table>
<div style="font-size:11px;color:#6b7280;margin-top:6px">
  Bar: <span style="color:{AMBER}">orange</span> = contact made (any outreach), <span style="color:{GREEN}">green</span> = real conversation.
</div>
"""


def render_backlog(backlog: list[dict]) -> str:
    if not backlog:
        return f"<p style='color:{GREEN};font-size:13px'>✅ No persistent gaps — every repeated rec has had at least one outreach attempt.</p>"
    rows = []
    for r in backlog:
        days_old = (datetime.now() - r["first_sent"].replace(tzinfo=None)).days
        rows.append(
            f"<tr><td style='padding:6px 10px;font-size:13px'>{escape(r['name'])}</td>"
            f"<td style='padding:6px 10px;font-size:12px;color:#6b7280'>{r['kind']}</td>"
            f"<td style='padding:6px 10px;text-align:right;font-size:13px'>{int(r['times_recommended'])}×</td>"
            f"<td style='padding:6px 10px;text-align:right;font-size:13px'>{days_old}d</td>"
            f"<td style='padding:6px 10px;text-align:right;font-size:13px'>{fmt_money(r['lifetime'])}</td></tr>"
        )
    return f"""
<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;border:1px solid #e5e7eb;border-radius:8px;background:white;font-size:13px">
  <thead style="background:#f9fafb">
    <tr>
      <th align="left"  style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Customer</th>
      <th align="left"  style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Kind</th>
      <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Rec'd</th>
      <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">First seen</th>
      <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:#6b7280">Lifetime $</th>
    </tr>
  </thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


# ---------- main ----------

def main() -> int:
    with db() as conn:
        state = load_today_state(conn)
        trend = load_7day_trend(conn)
        backlog = load_untouched_backlog(conn)

    agg = aggregate(state)
    overall = agg["overall"]
    by_kind = agg["by_kind"]

    today = date.today()
    today_str = today.strftime("%A, %B %d, %Y")

    if overall["recommended"] == 0:
        # No list was sent today — report briefly and bail
        body = (f"<h2 style='color:{NAVY}'>No CSR call list went out today.</h2>"
                f"<p>The morning email either didn't run or had an empty list. "
                f"Worth checking the daily-csr-email GitHub Action.</p>")
    else:
        sections = [render_kind_card(k, by_kind[k]) for k in ("missed", "membership", "sleeping") if k in by_kind]
        # Also render any other kinds not in the standard set
        for k, bucket in by_kind.items():
            if k not in ("missed", "membership", "sleeping"):
                sections.append(render_kind_card(k, bucket))
        body = "".join([
            f"<h2 style='color:{NAVY};margin:0 0 12px 0'>Today's scorecard</h2>",
            render_scorecard(overall),
            f"<h2 style='color:{NAVY};margin:24px 0 12px 0'>Per section</h2>",
            *sections,
            f"<h2 style='color:{NAVY};margin:24px 0 12px 0'>7-day trend</h2>",
            render_trend(trend),
            f"<h2 style='color:{NAVY};margin:24px 0 12px 0'>Persistent gaps (rec'd 2+ times, never touched)</h2>",
            render_backlog(backlog),
        ])

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f7f7;margin:0;padding:24px">
<div style="max-width:760px;margin:0 auto">
  <h1 style="margin:0 0 4px 0;color:{NAVY};font-size:22px">📊 Fey's EOD progress — {today_str}</h1>
  <p style="margin:0 0 18px 0;color:#555;font-size:13px">
    How today's call list translated into outreach. "Contact" = any outbound call to the customer.
    "Spoke" = outbound ≥ 90 seconds (likely real conversation). Sent at end of business day.
  </p>
  {body}
  <p style="color:#888;font-size:11px;margin-top:24px;text-align:center">
    Generated automatically. Morning list goes to Fey; this evening report goes to you only.
  </p>
</div>
</body></html>"""

    # Plain text fallback
    text_lines = [f"Fey EOD progress — {today_str}", ""]
    if overall["recommended"] == 0:
        text_lines.append("No CSR list went out today.")
    else:
        text_lines += [
            f"OVERALL: {overall['recommended']} recommended, "
            f"{overall['contacted']} contacted ({pct(overall['contacted'], overall['recommended'])}), "
            f"{overall['spoke']} spoke ({pct(overall['spoke'], overall['recommended'])})",
            f"  Conversions: {overall['booked']} booked, {overall['enrolled']} enrolled, "
            f"{overall['inbound']} called us back",
            "",
            "BY SECTION:",
        ]
        for k in ("missed", "membership", "sleeping"):
            if k not in by_kind:
                continue
            b = by_kind[k]
            text_lines.append(
                f"  {k:10s}  rec={b['recommended']:>3}  contacted={b['contacted']:>3}  "
                f"spoke={b['spoke']:>3}  vm={b['voicemail']:>3}  noans={b['no_answer']:>3}  "
                f"booked={b['booked']:>2}  enrolled={b['enrolled']:>2}"
            )
        text_lines += ["", "7-DAY TREND:"]
        for d in trend:
            rec = int(d["recommended"] or 0)
            con = int(d["contacted"] or 0)
            sp = int(d["spoke"] or 0)
            text_lines.append(
                f"  {d['day']:%a %m/%d}  rec={rec:>3}  contacted={con:>3}  spoke={sp:>3}  "
                f"rate={pct(con, rec):>4}"
            )
    text = "\n".join(text_lines)

    subject = (
        f"📊 Fey EOD — {overall['contacted']}/{overall['recommended']} contacted "
        f"({pct(overall['contacted'], overall['recommended'])}) — {today_str}"
        if overall["recommended"] else f"📊 Fey EOD — no list today — {today_str}"
    )

    recipients = parse_recipients(os.environ["EMAIL_TO"])
    if not recipients:
        sys.exit("EMAIL_TO has no valid addresses.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = fmt_recipients(recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    print(f"Sending EOD progress report to {fmt_recipients(recipients)}…")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg, to_addrs=recipients)
    print("Sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
