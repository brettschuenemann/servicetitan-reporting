"""Daily CSR call list for Fey.

Three sections, each with the names, phones, and customer history needed to
make the call without opening ServiceTitan first:

  1. Membership opportunities — install customers from the last 14 days who
     haven't enrolled. Warmest possible outreach window.
  2. Sleeping customers — high-LTV customers who've gone quiet (loyal in the
     last 24 months, $0 in the last 6 months). Reactivation list.
  3. Missed-call followups — abandoned + unbooked inbound calls from the
     last 24 hours. Calls back the people we missed.

Designed for GitHub Actions cron (8 AM ET weekdays); also runnable manually:
  python scripts/send_csr_daily_email.py

Required env vars:
  ST_APP_KEY, ST_TENANT_ID, ST_CLIENT_ID, ST_CLIENT_SECRET, DATABASE_URL,
  SMTP_USER, SMTP_PASSWORD, EMAIL_TO (CC), FEY_EMAIL_TO (primary TO)
Optional: EMAIL_FROM (defaults to SMTP_USER)
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from lib.call_openers import generate_openers  # noqa: E402
from lib.database import db  # noqa: E402
from lib.email_utils import (  # noqa: E402
    exclude,
    header as fmt_recipients,
    parse_recipients,
    record_send,
    should_skip_for_retry,
)
from lib.servicetitan import ServiceTitanClient  # noqa: E402
from lib.sync import sync_for_email  # noqa: E402

REQUIRED = (
    "SMTP_USER", "SMTP_PASSWORD",
    "ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET",
    "DATABASE_URL",
)


def _check_email_env_or_exit() -> None:
    """Abort if required env vars or recipients are missing. Called from
    main() only — NOT at module-import time. The Call List page imports
    this module for its helpers and shouldn't crash when SMTP/EMAIL_TO
    aren't configured in the Streamlit Cloud environment."""
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")
    if not parse_recipients(os.environ.get("FEY_EMAIL_TO")) and \
       not parse_recipients(os.environ.get("EMAIL_TO")):
        sys.exit("Set FEY_EMAIL_TO (and/or EMAIL_TO) before running.")


# ---------- formatting helpers ----------

def fmt_phone(p: str | None) -> str:
    if not p:
        return ""
    digits = "".join(c for c in p if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return p


def tel_href(p: str | None) -> str:
    if not p:
        return ""
    digits = "".join(c for c in p if c.isdigit())
    return f"tel:+1{digits[-10:]}" if len(digits) >= 10 else ""


def days_ago(d) -> str:
    if not d:
        return "—"
    if hasattr(d, "date"):
        d = d.date()
    delta = (date.today() - d).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 30:
        return f"{delta}d ago"
    if delta < 365:
        return f"{delta // 30}mo ago"
    return f"{delta // 365}y {(delta % 365) // 30}mo ago"


def short(s: str | None, n: int = 80) -> str:
    if not s:
        return ""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_money(v) -> str:
    return f"${float(v or 0):,.0f}"


# Call timestamps from ServiceTitan come back as UTC. Convert to Central
# (Chicagoland) before formatting for display — otherwise an 8 PM call
# shows up as 2 AM the next day.
try:
    from zoneinfo import ZoneInfo  # stdlib on Python 3.9+
    _CENTRAL_TZ = ZoneInfo("America/Chicago")
    _UTC_TZ = ZoneInfo("UTC")
except ImportError:  # very old Python — no-op fallback
    _CENTRAL_TZ = None
    _UTC_TZ = None


def to_central(dt):
    """Convert a UTC-aware (or naive=UTC-assumed) datetime to Central."""
    if dt is None or _CENTRAL_TZ is None:
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC_TZ)
    return dt.astimezone(_CENTRAL_TZ)


# ---------- contact lookup with cache ----------

_contact_cache: dict[int, tuple[str, str]] = {}


def lookup_contact(client: ServiceTitanClient, cid: int | None) -> tuple[str, str]:
    """Returns (primary_phone, primary_email). Mobile preferred over landline."""
    if not cid:
        return "", ""
    if cid in _contact_cache:
        return _contact_cache[cid]
    full = fetch_customer_contacts_full(client, cid)
    out = (full["primary_phone"], full["primary_email"])
    _contact_cache[cid] = out
    return out


def fetch_customer_contacts_full(client: ServiceTitanClient,
                                  cid: int | None) -> dict:
    """Returns the FULL contact picture for a customer:

      {
        primary_phone:  str,   # best phone (mobile first, then landline)
        primary_email:  str,   # first email
        all_phones:     [ {raw, normalized, kind}, ... ]   # every phone
      }

    `normalized` is the last 10 digits of `raw` (strips country code +
    formatting). Used by the conversion-analytics phone-match path so a
    call coming from a non-primary number (spouse, work line, etc) still
    resolves to the right customer.
    """
    if not cid:
        return {"primary_phone": "", "primary_email": "", "all_phones": []}
    try:
        contacts = client.get_customer_contacts(cid) or []
    except Exception:
        contacts = []

    phones_raw = sorted(
        (c for c in contacts
         if c.get("value") and c.get("type") in ("MobilePhone", "Phone")),
        key=lambda c: 0 if c["type"] == "MobilePhone" else 1,
    )
    emails = [c["value"] for c in contacts
              if c.get("value") and c.get("type") == "Email"]

    all_phones = []
    seen_normalized = set()
    for p in phones_raw:
        raw = str(p.get("value") or "").strip()
        normalized = "".join(ch for ch in raw if ch.isdigit())[-10:]
        if len(normalized) != 10 or normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)
        all_phones.append({"raw": raw, "normalized": normalized,
                           "kind": p.get("type") or ""})

    return {
        "primary_phone": phones_raw[0]["value"] if phones_raw else "",
        "primary_email": emails[0] if emails else "",
        "all_phones": all_phones,
    }


# ---------- data loaders ----------

def load_membership_opps(conn) -> list[dict]:
    """Install customers in the last 180 days with no active membership.

    Wide window so leads don't age out of the source before Fey can act —
    if she doesn't call today, the same customer shows up tomorrow. They
    only fall off when she calls OR the customer enrolls naturally.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH installs AS (
              SELECT
                i.id          AS invoice_id,
                i.customer_id,
                i.customer_name,
                i.invoice_date,
                i.sub_total   AS install_value,
                i.business_unit_name,
                i.summary     AS install_summary,
                -- Combine sku_name + description for richer equipment context.
                -- Many shops put detail in one or the other; concatenating gives
                -- Claude the most specific text to lift verbatim.
                (
                  SELECT string_agg(
                    DISTINCT NULLIF(TRIM(
                      COALESCE(it.sku_name, '') || ' '
                      || COALESCE(it.description, '')
                    ), ''),
                    '; '
                  )
                  FROM invoice_items it
                  WHERE it.invoice_id = i.id AND it.item_type = 'Equipment'
                ) AS equipment
              FROM invoices i
              WHERE i.invoice_date >= CURRENT_DATE - INTERVAL '180 days'
                AND i.customer_id IS NOT NULL
                AND COALESCE(i.sub_total, 0) > 0
                AND (
                  i.business_unit_name ILIKE '%%install%%'
                  OR EXISTS (
                    SELECT 1 FROM invoice_items it
                    WHERE it.invoice_id = i.id AND it.item_type = 'Equipment'
                  )
                )
            ),
            active_mem AS (
              SELECT DISTINCT customer_id FROM memberships
              WHERE status = 'Active' AND customer_id IS NOT NULL
            ),
            history AS (
              SELECT customer_id, COUNT(*) AS invoices,
                     SUM(total) AS lifetime_revenue,
                     MIN(invoice_date) AS first_visit
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            )
            -- Per customer: most recent install plus all install values
            SELECT
              ins.customer_id, ins.customer_name,
              MAX(ins.invoice_date) AS install_date,
              SUM(ins.install_value) AS install_value,
              MIN(ins.business_unit_name) AS business_unit_name,
              string_agg(DISTINCT ins.equipment, '; ') AS equipment,
              string_agg(DISTINCT NULLIF(TRIM(ins.install_summary), ''), ' | ')
                AS install_summary,
              MIN(h.invoices) AS lifetime_invoices,
              MIN(h.lifetime_revenue) AS lifetime_revenue,
              MIN(h.first_visit) AS first_visit
            FROM installs ins
            LEFT JOIN active_mem am ON am.customer_id = ins.customer_id
            LEFT JOIN history h ON h.customer_id = ins.customer_id
            WHERE am.customer_id IS NULL
            GROUP BY ins.customer_id, ins.customer_name
            ORDER BY MAX(ins.invoice_date) DESC, SUM(ins.install_value) DESC
            LIMIT 25
            """
        )
        return [dict(r) for r in cur.fetchall()]


def load_sleeping_customers(conn, limit: int = 15) -> list[dict]:
    """High-LTV customers loyal in the last 24mo but silent in the last 6mo."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH loyal AS (
              SELECT
                customer_id,
                MIN(customer_name) AS customer_name,
                COUNT(*) AS loyal_invoices,
                SUM(total) AS loyal_revenue,
                MAX(invoice_date) AS last_visit
              FROM invoices
              WHERE customer_id IS NOT NULL
                AND invoice_date BETWEEN
                  CURRENT_DATE - INTERVAL '24 months'
                  AND CURRENT_DATE - INTERVAL '6 months'
                AND total > 0
              GROUP BY customer_id
              HAVING SUM(total) >= 500
            ),
            quiet AS (
              SELECT DISTINCT customer_id
              FROM invoices
              WHERE customer_id IS NOT NULL
                AND invoice_date >= CURRENT_DATE - INTERVAL '6 months'
            ),
            recent_summary AS (
              SELECT DISTINCT ON (i.customer_id)
                i.customer_id,
                i.summary,
                i.invoice_date,
                i.id AS invoice_id,
                -- The line items from that last invoice — more specific
                -- than the summary alone, lets Claude reference exact work.
                (
                  SELECT string_agg(
                    DISTINCT NULLIF(TRIM(
                      COALESCE(it.sku_name, '') || ' '
                      || COALESCE(it.description, '')
                    ), ''),
                    '; '
                    ORDER BY NULLIF(TRIM(
                      COALESCE(it.sku_name, '') || ' '
                      || COALESCE(it.description, '')
                    ), '')
                  )
                  FROM invoice_items it
                  WHERE it.invoice_id = i.id
                    AND COALESCE(it.item_type, '') <> 'Discount'
                ) AS last_items
              FROM invoices i
              WHERE i.customer_id IS NOT NULL AND i.total > 0
              ORDER BY i.customer_id, i.invoice_date DESC
            )
            SELECT
              l.customer_id, l.customer_name,
              l.loyal_invoices, l.loyal_revenue, l.last_visit,
              rs.summary AS last_summary,
              rs.last_items
            FROM loyal l
            LEFT JOIN quiet q ON q.customer_id = l.customer_id
            LEFT JOIN recent_summary rs ON rs.customer_id = l.customer_id
            WHERE q.customer_id IS NULL
            ORDER BY l.loyal_revenue DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def load_open_estimates(conn, min_age_days: int = 30,
                        max_age_days: int | None = None,
                        min_value: float = 0) -> list[dict]:
    """Open estimates in a given age window.

    Defaults match Fey's bucket on the Call List (>30d). Jake's Todo
    page passes `min_age_days=0, max_age_days=30` to get the fresh
    side of the funnel.

    Sorted by subtotal DESC then age DESC — biggest dollar
    opportunities first, with the oldest as the tiebreaker.
    """
    upper_clause = ""
    params: list = [min_age_days]
    if max_age_days is not None:
        upper_clause = "AND e.created_on >= NOW() - (%s || ' days')::interval"
        params.append(max_age_days)
    params.append(min_value)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH first_tech AS (
              SELECT DISTINCT ON (aa.job_id)
                aa.job_id,
                aa.technician_name
              FROM appointment_assignments aa
              WHERE aa.technician_name IS NOT NULL
                AND aa.technician_name <> 'Imported Default Technician'
              ORDER BY aa.job_id, aa.assigned_on
            ),
            history AS (
              SELECT customer_id, COUNT(*) AS invoices,
                     SUM(total) AS lifetime_revenue
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            )
            SELECT
              e.id,
              e.name        AS estimate_name,
              e.summary,
              e.subtotal,
              e.created_on,
              e.business_unit_name,
              e.customer_id,
              e.job_id,
              e.job_number,
              COALESCE(
                (SELECT MIN(customer_name) FROM invoices
                 WHERE customer_id = e.customer_id AND customer_name IS NOT NULL),
                'Customer ' || e.customer_id::text
              ) AS customer_name,
              EXTRACT(DAY FROM (NOW() - e.created_on))::int AS age_days,
              ft.technician_name AS originating_tech,
              h.invoices AS lifetime_invoices,
              h.lifetime_revenue
            FROM estimates e
            LEFT JOIN first_tech ft ON ft.job_id = e.job_id
            LEFT JOIN history h    ON h.customer_id = e.customer_id
            WHERE e.status_name = 'Open'
              AND e.active = TRUE
              AND e.created_on < NOW() - (%s || ' days')::interval
              {upper_clause}
              AND e.subtotal  >= %s
              AND e.customer_id IS NOT NULL
            ORDER BY e.subtotal DESC, e.created_on ASC
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def load_missed_calls(conn) -> list[dict]:
    """Inbound abandoned + unbooked calls from the last 30 days.

    Wide window so unactioned missed calls keep showing daily until
    Fey responds. Naturally drops calls where the customer subsequently
    booked a job (new invoice after the missed call) — that means they
    got handled some other way.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH history AS (
              SELECT customer_id, COUNT(*) AS invoices,
                     SUM(total) AS lifetime_revenue,
                     MAX(invoice_date) AS last_visit
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            ),
            last_invoice AS (
              -- Most recent paid invoice per customer: gives Claude context
              -- about what their last interaction with us was about.
              SELECT DISTINCT ON (customer_id)
                customer_id, summary AS last_invoice_summary
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
                AND summary IS NOT NULL AND TRIM(summary) <> ''
              ORDER BY customer_id, invoice_date DESC
            )
            SELECT
              c.id, c.received_on, c.from_phone, c.customer_id, c.customer_name,
              c.call_type, c.reason, c.duration_seconds, c.agent_name,
              h.invoices AS lifetime_invoices,
              h.lifetime_revenue,
              h.last_visit,
              li.last_invoice_summary
            FROM calls c
            LEFT JOIN history h ON h.customer_id = c.customer_id
            LEFT JOIN last_invoice li ON li.customer_id = c.customer_id
            WHERE c.direction = 'Inbound'
              AND c.call_type IN ('Abandoned', 'Unbooked')
              AND c.created_on >= NOW() - INTERVAL '30 days'
              -- Drop calls where the customer booked something after the
              -- missed call — they've been handled (by Fey, another rep,
              -- or by calling back themselves).
              AND NOT EXISTS (
                SELECT 1 FROM invoices i
                WHERE i.customer_id = c.customer_id
                  AND i.total > 0
                  AND i.invoice_date >= (c.created_on AT TIME ZONE 'UTC')::date
              )
              -- Also drop calls where the customer has an active future-
              -- looking job created on/after the missed call — they're
              -- on the schedule, a tech is coming, no need to chase.
              AND NOT EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.customer_id = c.customer_id
                  AND j.job_status IN ('Scheduled', 'Dispatched',
                                        'InProgress', 'Working', 'Hold')
                  AND j.completed_on IS NULL
                  AND j.created_on >= c.created_on
              )
            ORDER BY c.received_on DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- HTML rendering ----------

CARD_STYLES = {
    "membership": ("#0066EE", "🤝 Membership opportunities", "Install customers in the last 180 days who haven't enrolled. They stay on this list every day until you call them, leave a voicemail, or they enroll."),
    "sleeping":   ("#F2A93B", "💤 Sleeping customers",      "High-value customers who've gone quiet. They already know us; a friendly call wins them back."),
    "missed":     ("#F34039", "📞 Missed calls — call back", "Inbound calls in the last 30 days that didn't book — and where the customer hasn't booked something since. They stay on this list daily until you call them or they book."),
    "estimate":   ("#8B5CF6", "📋 Aging estimates (30d+)",   "Open estimates older than 30 days — Jake covers the fresh ones, these are yours. Sorted by value: biggest dollar opportunities first."),
}

# Quick call scripts shown at the top of each section. Keep them short —
# Fey should be able to read them while the phone rings. Edit freely as
# you tune the pitch; nothing else in the codebase depends on the text.
CALL_SCRIPTS = {
    "missed": [
        ("Opener", "\"Hi [name], this is Fey from Pure Comfort — I'm calling you back, sorry we missed you earlier today.\""),
        ("Discover", "\"What were you hoping we could help with?\" — let them talk; resist the urge to pitch."),
        ("Close", "\"Let me get someone out to take a look. What works better for you, morning or afternoon this week?\""),
        ("If they already booked elsewhere", "\"Totally understand. We're always here if anything comes up — and we usually have same-day availability for emergencies.\""),
    ],
    "membership": [
        ("Opener", "\"Hi [name], this is Fey at Pure Comfort — just checking in on your new system from [install date]. How's it running for you?\""),
        ("Pivot", "\"The reason I called: most manufacturers require an annual tune-up to keep your warranty valid. A lot of our install customers join our maintenance plan so we handle it automatically — covers three tune-ups a year (two HVAC and one plumbing), priority scheduling, and 10% off any services.\""),
        ("Special offer", "\"And because you just installed, I can get you 50% off your first year. That makes it a no-brainer — and after that you can keep it or cancel anytime.\""),
        ("Close", "\"Want me to lock that in today, or send you the details to look over first?\""),
        ("If \"too expensive\"", "\"At 50% off the first year, it pays for itself the first time anything goes wrong — and it keeps your warranty in force, which is usually the bigger number.\""),
        ("If \"let me think about it\"", "\"Totally fair. Can I send you the plan details and the first-year discount? That way you've got it whenever you're ready.\""),
    ],
    "sleeping": [
        ("Opener", "\"Hi [name], this is Fey at Pure Comfort. I was going through our records and saw it's been a while — last time we were out we [last service]. Just wanted to check in: how's the system been holding up?\""),
        ("Listen first", "Don't go straight to a sale. Let them talk about the system, the weather, whatever. The reconnect matters more than the pitch."),
        ("Soft offer", "\"We're about to head into [cooling/heating] season — most of our regulars are getting a tune-up about now to catch anything before it turns into a breakdown. Want to get on the calendar?\""),
        ("Plumbing FYI", "\"And while I have you — one thing that's new since we last talked: we now do full-service plumbing too. So next time you've got a leaky faucet, a slow drain, or the water heater's getting up there in age, give us a call instead of hunting for a plumber. Same team, same standards.\""),
        ("If \"not right now\"", "\"No problem at all. Mind if I send a reminder in a couple of months? You know where to find us — HVAC or plumbing — anytime something comes up.\""),
    ],
    "estimate": [
        ("Opener", "\"Hi [name], this is Fey at Pure Comfort. I'm following up on the estimate [tech] put together for you on [estimate date] — [estimate name] for [estimate value]. Have you had a chance to look it over?\""),
        ("Discover", "\"Anything specific holding things up, or just need a little more time to think it through?\" — listen; don't pitch yet."),
        ("If \"still deciding\"", "\"Totally understand — it's a real decision. If you can lock it in this week we can usually pull your install forward. Want me to pencil you in?\""),
        ("If \"too expensive\"", "\"I hear you. Want me to have someone walk through it with you again? Sometimes there's a smaller-scope option that gets you most of the way there for less.\""),
        ("If \"chose someone else\"", "\"No problem — appreciate you letting me know. Mind if I ask what made the difference? Helps us stay sharp. And remember, we're here if anything changes.\""),
        ("Close", "\"Want me to get you on the calendar this week? We can usually be out within a few days.\""),
    ],
}


def render_call_script(kind: str) -> str:
    """Render the per-section call script as a compact list. Returns empty
    string if no script is defined for this kind."""
    items = CALL_SCRIPTS.get(kind, [])
    if not items:
        return ""
    rows = "".join(
        f"<div style='margin-top:6px'>"
        f"<div style='font-size:11px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:0.04em'>{escape(label)}</div>"
        f"<div style='font-size:13px;color:#1f2937;margin-top:1px;line-height:1.45'>{escape(line)}</div>"
        f"</div>"
        for label, line in items
    )
    return f"""
<div style="background:#fff8e1;border:1px solid #fde68a;border-radius:6px;padding:10px 12px;margin-bottom:12px">
  <div style="font-size:12px;font-weight:700;color:#92400E;text-transform:uppercase;letter-spacing:0.05em">📋 Call script</div>
  {rows}
</div>
"""

# Cooldown windows AFTER Fey has taken some action on a lead.
#   "full"  → real conversation happened (outbound call ≥ CONVERSATION_DURATION) —
#             she did her job, give the customer time to think before re-pitching.
#   "short" → only voicemail/no-answer — try again sooner.
# Recommendations with NO outreach detected are NEVER time-suppressed; they
# stay on Fey's list every day until she acts on them or natural drop-off
# removes them from the source query (booking, enrollment, customer responds).
SUPPRESS_DAYS_FULL = {
    "membership": 14,
    "sleeping":   45,
    "missed":     7,
    "estimate":   30,   # had a real convo about the quote — give them time to decide
}
SUPPRESS_DAYS_SHORT = {  # used when only voicemails/no-answers detected
    "membership": 4,
    "sleeping":   10,
    "missed":     2,
    "estimate":   4,
}

# Hard caps per section so a backlog doesn't produce a 100-row email/page.
# Same number across all keeps Fey's daily workload predictable and the
# page scannable in one viewport on mobile.
SECTION_CAPS = {
    "missed":     10,
    "membership": 10,
    "sleeping":   10,
    "estimate":   10,
}

# Base URL for the Streamlit app (used to build outcome-action links in the
# email). Set STREAMLIT_BASE_URL in the workflow secret; falls back to a
# placeholder that just hides the action links if unset.
APP_BASE_URL = (os.environ.get("STREAMLIT_BASE_URL") or "").rstrip("/")

# Action links shown under each row. Each entry is (outcome_key, label, color).
ACTION_LINKS: dict[str, list[tuple[str, str, str]]] = {
    "membership": [
        ("enrolled",     "✅ Enrolled",      "#10B981"),
        ("declined",     "❌ Declined",      "#EF4444"),
        ("try_later",    "🔁 Try later",     "#F59E0B"),
        ("wrong_number", "📵 Wrong #",       "#6B7280"),
    ],
    "sleeping": [
        ("reactivated",  "✅ Reactivated",   "#10B981"),
        ("declined",     "❌ Declined",      "#EF4444"),
        ("try_later",    "🔁 Try later",     "#F59E0B"),
        ("wrong_number", "📵 Wrong #",       "#6B7280"),
    ],
    "missed": [
        ("followed_up",  "✅ Followed up",   "#10B981"),
        ("voicemail",    "📨 Voicemail",     "#F59E0B"),
        ("try_later",    "🔁 Try later",     "#F59E0B"),
        ("wrong_number", "📵 Wrong #",       "#6B7280"),
    ],
    "estimate": [
        ("sold",         "✅ Sold",          "#10B981"),
        ("declined",     "❌ Declined",      "#EF4444"),
        ("voicemail",    "📨 Voicemail",     "#F59E0B"),
        ("try_later",    "🔁 Try later",     "#F59E0B"),
        ("wrong_number", "📵 Wrong #",       "#6B7280"),
    ],
}


def render_action_links(kind: str, customer_id: int | None, call_id: int | None = None) -> str:
    """Render the per-row action links Fey taps after the call.

    Returns empty string when STREAMLIT_BASE_URL isn't configured —
    rather than rendering broken links.
    """
    if not APP_BASE_URL or not customer_id:
        return ""
    actions = ACTION_LINKS.get(kind, [])
    links = []
    for outcome_key, label, color in actions:
        params = [f"cust={customer_id}", f"kind={kind}", f"outcome={outcome_key}"]
        if kind == "missed" and call_id:
            params.append(f"call={call_id}")
        url = f"{APP_BASE_URL}/Outcomes?" + "&".join(params)
        links.append(
            f"<a href='{url}' style='display:inline-block;padding:3px 9px;"
            f"border:1px solid {color};color:{color};text-decoration:none;"
            f"border-radius:12px;font-size:11px;font-weight:600;"
            f"margin-right:6px;margin-top:2px'>{label}</a>"
        )
    return (
        f"<div style='margin-top:6px;padding-top:6px;border-top:1px dashed #f3f4f6'>"
        f"<span style='font-size:11px;color:#9ca3af;margin-right:8px'>After the call:</span>"
        f"{''.join(links)}</div>"
    )

# Duration thresholds (seconds) for inferring call outcome from outbound calls.
CONVERSATION_DURATION = 90   # >= 90s = real conversation
VOICEMAIL_MIN_DURATION = 15  # >= 15s and < 90s = likely voicemail left
# < 15s = ring-out / no answer


def load_recommendation_state(conn) -> dict:
    """Return suppression + history + observed outreach for the recommendation log.

    Returns a dict with:
      suppress    — dict[kind, set[dedup_key]] of keys we should NOT re-recommend
      first_seen  — dict[dedup_key, datetime] for rendering "pending Xd"
      outreach    — dict[customer_id, dict] of observed signals since first rec:
                      {"called_at": dt, "called_back_at": dt}

    Smart suppression rule: if the customer **called back** after we recommended
    them (inbound call from their phone), do NOT suppress — surface them again
    as a hot lead so Fey doesn't lose them in the noise.
    """
    suppress: dict[str, set[str]] = {k: set() for k in SUPPRESS_DAYS_FULL}
    first_seen: dict[str, datetime] = {}
    outreach: dict[int, dict] = {}
    lookback = max(90, max(SUPPRESS_DAYS_FULL.values()))
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        # 1. Pull recommendation history (grouped by dedup_key).
        cur.execute(
            """
            SELECT kind, dedup_key, customer_id,
                   MIN(sent_at) AS first_seen,
                   MAX(sent_at) AS last_seen
            FROM csr_recommendations
            WHERE sent_at >= %s
            GROUP BY kind, dedup_key, customer_id
            """,
            (cutoff,),
        )
        rec_rows = [dict(r) for r in cur.fetchall()]
        for r in rec_rows:
            first_seen[r["dedup_key"]] = r["first_seen"]

        # 2. For customers with prior recs, look up outbound/inbound calls
        #    since their reference point. Reference = the earlier of:
        #      (a) when they first showed up on a recommendation list, OR
        #      (b) the earliest missed call from them in our lookback window
        #    Using (b) when earlier catches the case where Fey calls back the
        #    same day a customer missed us — that outbound happens BEFORE the
        #    next morning's email is sent, but we still want to detect it as
        #    outreach against the missed-call recommendation.
        cur.execute(
            f"""
            WITH first_recs AS (
              SELECT customer_id, MIN(sent_at) AS first_rec_at
              FROM csr_recommendations
              WHERE sent_at >= %s AND customer_id IS NOT NULL
              GROUP BY customer_id
            ),
            first_missed AS (
              SELECT customer_id, MIN(created_on) AS first_missed_at
              FROM calls
              WHERE direction = 'Inbound'
                AND call_type IN ('Abandoned', 'Unbooked')
                AND customer_id IS NOT NULL
                AND created_on >= NOW() - INTERVAL '30 days'
              GROUP BY customer_id
            ),
            reference AS (
              SELECT fr.customer_id,
                     LEAST(fr.first_rec_at,
                           COALESCE(fm.first_missed_at, fr.first_rec_at)) AS reference_at
              FROM first_recs fr
              LEFT JOIN first_missed fm ON fm.customer_id = fr.customer_id
            )
            SELECT
              c.customer_id,
              COUNT(*) FILTER (WHERE c.direction = 'Outbound')                    AS ob_attempts,
              COUNT(*) FILTER (WHERE c.direction = 'Outbound'
                               AND c.duration_seconds >= {CONVERSATION_DURATION}) AS ob_conversations,
              COUNT(*) FILTER (WHERE c.direction = 'Outbound'
                               AND c.duration_seconds >= {VOICEMAIL_MIN_DURATION}
                               AND c.duration_seconds <  {CONVERSATION_DURATION}) AS ob_voicemails,
              COUNT(*) FILTER (WHERE c.direction = 'Outbound'
                               AND COALESCE(c.duration_seconds, 0) < {VOICEMAIL_MIN_DURATION}) AS ob_no_answers,
              MAX(c.created_on) FILTER (WHERE c.direction = 'Outbound')           AS last_outbound,
              MAX(c.created_on) FILTER (WHERE c.direction = 'Inbound')            AS last_inbound
            FROM calls c
            JOIN reference r ON r.customer_id = c.customer_id
            WHERE c.created_on >= r.reference_at
            GROUP BY c.customer_id
            """,
            (cutoff,),
        )
        for row in cur.fetchall():
            cid = int(row["customer_id"])
            info = {
                "attempts":      int(row["ob_attempts"] or 0),
                "conversations": int(row["ob_conversations"] or 0),
                "voicemails":    int(row["ob_voicemails"] or 0),
                "no_answers":    int(row["ob_no_answers"] or 0),
                "last_outbound": row["last_outbound"],
                "called_back_at": row["last_inbound"],
            }
            if info["attempts"] > 0 or info["called_back_at"]:
                outreach[cid] = info

    # 3. Decide suppression. Policy: a recommendation stays on Fey's list
    #    every day UNTIL she takes action (or the customer responds). We
    #    only suppress when there's evidence something happened:
    #      - called_back   → NEVER suppress (hot lead, surface immediately)
    #      - real conversation (≥ CONVERSATION_DURATION outbound) → full window
    #      - voicemail / no-answer attempts only → short window
    #      - NO outreach detected → do NOT suppress (keep showing daily)
    for r in rec_rows:
        kind = r["kind"]
        key = r["dedup_key"]
        cid = r.get("customer_id")
        info = outreach.get(cid, {}) if cid else {}

        if info.get("called_back_at"):
            continue  # un-suppressed — they reached out

        if info.get("conversations", 0) > 0:
            window = SUPPRESS_DAYS_FULL.get(kind)
        elif info.get("attempts", 0) > 0:
            window = SUPPRESS_DAYS_SHORT.get(kind)
        else:
            continue  # no action taken → keep showing daily

        if window is None:
            continue
        if (now - r["last_seen"]).days < window:
            suppress.setdefault(kind, set()).add(key)

    # 4. Explicit outcomes Fey logged via the action links beat everything
    #    above. The most recent non-undone outcome whose expires_at is in
    #    the future (or NULL = permanent) suppresses that dedup_key.
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
              SELECT id, kind, dedup_key, outcome, expires_at,
                     ROW_NUMBER() OVER (PARTITION BY dedup_key
                                        ORDER BY recorded_at DESC) AS rn
              FROM csr_customer_outcomes
            )
            SELECT kind, dedup_key, outcome, expires_at
            FROM ranked
            WHERE rn = 1
              AND outcome <> 'undone'
              AND (expires_at IS NULL OR expires_at > NOW())
            """
        )
        for row in cur.fetchall():
            suppress.setdefault(row["kind"], set()).add(row["dedup_key"])

    return {
        "suppress": suppress,
        "first_seen": first_seen,
        "outreach": outreach,
    }


def dedup_key(kind: str, customer_id: int | None, call_id: int | None = None) -> str:
    """Stable key for de-duping recommendations across days.

    Must stay in sync with lib/csr_outcomes.dedup_key — the third arg
    doubles as a generic "secondary id": call_id for missed calls,
    estimate_id for aging-estimates rows.
    """
    if kind == "missed" and call_id is not None:
        return f"{kind}:call:{call_id}"
    if kind == "estimate" and call_id is not None:
        return f"{kind}:est:{call_id}"
    return f"{kind}:cust:{customer_id}"


def record_recommendations(conn, rows: list[tuple]) -> None:
    """Persist today's batch to csr_recommendations. Caller passes
    (kind, customer_id, call_id, dedup_key, payload_json) tuples."""
    if not rows:
        return
    from psycopg2.extras import execute_values
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO csr_recommendations "
            "(kind, customer_id, call_id, dedup_key, payload) VALUES %s",
            rows,
            template="(%s,%s,%s,%s,%s::jsonb)",
            page_size=200,
        )
    conn.commit()


def html_section(kind: str, rows_html: str, count: int,
                 suppressed_count: int = 0, more_pending: int = 0) -> str:
    color, title, sub = CARD_STYLES[kind]
    badge = f"<span style='background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:13px;margin-left:8px'>{count}</span>"
    script_html = render_call_script(kind) if count > 0 else ""

    if rows_html:
        body = rows_html
        if more_pending > 0:
            body += (
                f"<p style='font-size:12px;color:#6b7280;margin-top:10px;font-style:italic'>"
                f"+ {more_pending} more pending — capped at {SECTION_CAPS[kind]} to keep the email readable. "
                f"They'll show up here once she clears today's list.</p>"
            )
    elif suppressed_count > 0:
        body = (
            f"<p style='color:#92400E;margin:6px 0 0;font-size:13px'>"
            f"All {suppressed_count} pending lead{'s' if suppressed_count != 1 else ''} "
            f"in this section have already had a call attempt — Fey's done her job. "
            f"They'll come back into rotation once their cooldown expires.</p>"
        )
    else:
        body = '<p style="color:#888;margin:6px 0 0">Nothing new — focus on the other sections today.</p>'

    return f"""
<div style="border:1px solid #e5e7eb;border-left:6px solid {color};border-radius:8px;padding:16px 20px;margin-bottom:24px;background:white">
  <h2 style="margin:0 0 4px 0;color:{color};font-size:18px">{title} {badge}</h2>
  <p style="margin:0 0 12px 0;color:#555;font-size:13px">{sub}</p>
  {script_html}
  {body}
</div>
"""


def _badge_html(text: str, bg: str, fg: str) -> str:
    return (
        f"<span style='display:inline-block;background:{bg};color:{fg};"
        f"padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;"
        f"margin-left:8px;vertical-align:1px'>{text}</span>"
    )


def _status_badge(*, pending_days: int | None, outreach_info: dict | None) -> str:
    """Pick the most informative badge for this row. Hot leads first."""
    info = outreach_info or {}

    # 1. They called us back — hottest signal
    if info.get("called_back_at"):
        days = _days_since(info["called_back_at"])
        label = "today" if days == 0 else f"{days}d ago"
        return _badge_html(f"📥 Called us back {label}", "#FED7AA", "#9A3412")

    # 2. Real conversation happened — long outbound call
    if info.get("conversations", 0) > 0:
        days = _days_since(info.get("last_outbound"))
        label = "today" if days == 0 else f"{days}d ago"
        return _badge_html(f"📞 Spoke {label} — follow up", "#D1FAE5", "#065F46")

    # 3. Only voicemails / no-answers — show attempt count so she can decide
    #    to switch channel (text/email) if she's tried too many times.
    attempts = info.get("attempts", 0)
    if attempts > 0:
        days = _days_since(info.get("last_outbound"))
        last = "today" if days == 0 else f"{days}d ago"
        voicemails = info.get("voicemails", 0)
        no_answers = info.get("no_answers", 0)
        if voicemails > 0 and no_answers == 0:
            text = f"📨 Voicemail{'s' if voicemails > 1 else ''} ×{attempts}, last {last}"
        elif no_answers > 0 and voicemails == 0:
            text = f"🔕 No answer ×{attempts}, last {last}"
        else:
            text = f"📨 Tried ×{attempts}, no conversation yet ({last})"
        # Color shifts orange-ish to convey "needs another touch"
        return _badge_html(text, "#FEE2E2", "#991B1B")

    # 4. Recommended before but no outreach detected
    if pending_days and pending_days > 0:
        return _badge_html(f"🔁 Pending {pending_days}d", "#FEF3C7", "#92400E")

    # 5. Brand new
    return ""


def html_customer_row(*, name: str, phone: str, email: str, primary_line: str,
                      history_line: str, pending_days: int | None = None,
                      outreach_info: dict | None = None,
                      action_links_html: str = "",
                      opener_text: str | None = None) -> str:
    phone_disp = fmt_phone(phone)
    phone_html = (
        f"<a href='{escape(tel_href(phone))}' style='color:#0066EE;text-decoration:none;font-weight:600'>{escape(phone_disp)}</a>"
        if phone else "<span style='color:#999'>no phone on file</span>"
    )
    email_html = (
        f" · <a href='mailto:{escape(email)}' style='color:#666;text-decoration:none'>{escape(email)}</a>"
        if email else ""
    )
    badge = _status_badge(pending_days=pending_days, outreach_info=outreach_info)
    opener_html = ""
    if opener_text:
        opener_html = (
            f"<div style='margin-top:8px;padding:8px 10px;background:#EFF6FF;"
            f"border-left:3px solid #0066EE;border-radius:4px;font-size:13px;"
            f"color:#1E3A8A;line-height:1.45'>"
            f"<span style='font-size:11px;font-weight:700;color:#1E40AF;"
            f"text-transform:uppercase;letter-spacing:0.04em'>✨ Opener</span> "
            f"<i>{escape(opener_text)}</i></div>"
        )
    return f"""
<div style="border-top:1px solid #f0f0f0;padding:10px 0">
  <div style="font-size:15px;font-weight:600;color:#111">{escape(name)}{badge}</div>
  <div style="font-size:14px;margin:2px 0 4px">{phone_html}{email_html}</div>
  <div style="font-size:13px;color:#333">{primary_line}</div>
  <div style="font-size:12px;color:#777;margin-top:2px">{history_line}</div>
  {opener_html}
  {action_links_html}
</div>
"""


def _days_since(then: datetime | None) -> int | None:
    if not then:
        return None
    delta = (datetime.now(timezone.utc) - then).days
    return delta if delta >= 0 else None


def _pending_days(first_seen: datetime | None) -> int | None:
    """Days since first_seen, or None if this is the first appearance."""
    if not first_seen:
        return None
    delta = (datetime.now(timezone.utc) - first_seen).days
    return delta if delta > 0 else None


# ---------- main ----------



# ---------- main: send a SHORT notification email linking to the Call List page ----------

# Where the live Call List page lives. From the same secret used by the
# outcome-action links — graceful fallback if unset.
_CALL_LIST_URL = (os.environ.get("STREAMLIT_BASE_URL") or "").rstrip("/")


def _render_notification_html(
    counts: dict[str, int],
    hot_leads: list[dict],
    today_str: str,
    call_list_url: str,
) -> str:
    """Short HTML notification — counts, hot leads, prominent CTA to the live page."""
    total = counts["missed"] + counts["membership"] + counts["sleeping"] + counts.get("estimate", 0)

    hot_html = ""
    if hot_leads:
        items = "".join(
            f"<li style='margin:4px 0'><b>{escape(h['customer_name'] or 'Unknown')}</b> "
            f"called us back — <i>{escape(h['kind'])}</i> section</li>"
            for h in hot_leads[:5]
        )
        hot_html = (
            f"<div style='background:#FED7AA;border-left:4px solid #9A3412;"
            f"padding:12px 14px;border-radius:6px;margin:16px 0'>"
            f"<div style='font-size:13px;font-weight:700;color:#9A3412;"
            f"text-transform:uppercase;letter-spacing:0.04em;margin-bottom:6px'>"
            f"🔥 Hot leads — call these first</div>"
            f"<ul style='margin:0;padding-left:20px;font-size:14px;color:#7C2D12'>{items}</ul>"
            f"</div>"
        )

    cta = ""
    if call_list_url:
        cta = (
            f"<div style='text-align:center;margin:24px 0'>"
            f"<a href='{call_list_url}/Call_List' "
            f"style='display:inline-block;background:#0066EE;color:white;"
            f"padding:14px 32px;border-radius:8px;text-decoration:none;"
            f"font-size:16px;font-weight:700'>📞 Open your call list</a>"
            f"</div>"
            f"<p style='text-align:center;font-size:12px;color:#888'>"
            f"Tip: bookmark the page — it's live all day. New missed calls and "
            f"hot leads appear automatically.</p>"
        )
    else:
        cta = (
            f"<p style='color:#EF4444;margin:16px 0;font-size:13px'>"
            f"⚠️ STREAMLIT_BASE_URL isn't configured — the dashboard URL "
            f"isn't linked. Add it as a GitHub Actions secret to enable.</p>"
        )

    return f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f7f7;margin:0;padding:24px">
<div style="max-width:560px;margin:0 auto;background:white;border-radius:8px;padding:28px 32px;border:1px solid #e5e7eb">
  <h1 style="margin:0 0 6px 0;color:#00214D;font-size:22px">Good morning, Fey 👋</h1>
  <p style="margin:0 0 20px 0;color:#555;font-size:14px">{today_str}</p>

  <p style="font-size:15px;color:#111;line-height:1.55">
    Your call list is ready —
    <b style="color:#F34039">{counts['missed']}</b> missed call{'s' if counts['missed'] != 1 else ''},
    <b style="color:#0066EE">{counts['membership']}</b> membership opportunit{'ies' if counts['membership'] != 1 else 'y'},
    <b style="color:#8B5CF6">{counts.get('estimate', 0)}</b> aging estimate{'s' if counts.get('estimate', 0) != 1 else ''},
    and <b style="color:#F2A93B">{counts['sleeping']}</b> sleeping customer{'s' if counts['sleeping'] != 1 else ''}
    to reach out to today.
  </p>

  {hot_html}

  {cta}

  <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
  <p style="font-size:12px;color:#888;margin:0">
    Action buttons live inline on the page now — tap <b>✅ Enrolled</b>, <b>❌ Declined</b>,
    or <b>🔁 Try later</b> after each call and the list updates instantly.
    No more separate email link per row.
  </p>
</div>
</body></html>"""


def main() -> int:
    # Run env-var checks here (not at import time) so the Call List page
    # can import this module's helpers without crashing on a Streamlit
    # Cloud deploy that doesn't have SMTP/EMAIL_TO configured.
    _check_email_env_or_exit()

    client = ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"], tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"], client_secret=os.environ["ST_CLIENT_SECRET"],
    )

    with db() as conn:
        if should_skip_for_retry(conn, "csr_daily", hours=6):
            print("CSR daily email was already sent in the last 6h — skipping retry.")
            return 0

    print("Pre-email sync (incremental, all entities)…")
    with db() as conn:
        sync_for_email(client, conn, progress=lambda m: print(f"  · {m}"))

        # Load the same data the page will load so counts match and the
        # recommendation log is populated (suppression depends on it).
        state = load_recommendation_state(conn)
        suppress = state["suppress"]
        outreach_map = state["outreach"]

        memberships_all = load_membership_opps(conn)
        memberships = [r for r in memberships_all
                       if dedup_key("membership", r.get("customer_id")) not in suppress["membership"]
                      ][:SECTION_CAPS["membership"]]

        sleeping_all = load_sleeping_customers(conn, limit=SECTION_CAPS["sleeping"] * 4)
        sleeping = [r for r in sleeping_all
                    if dedup_key("sleeping", r.get("customer_id")) not in suppress["sleeping"]
                   ][:SECTION_CAPS["sleeping"]]

        missed_all = load_missed_calls(conn)
        missed = [r for r in missed_all
                  if dedup_key("missed", r.get("customer_id"), r.get("id")) not in suppress["missed"]
                 ][:SECTION_CAPS["missed"]]

        estimates_all = load_open_estimates(conn, min_age_days=30)
        estimates = [r for r in estimates_all
                     if dedup_key("estimate", r.get("customer_id"), r.get("id")) not in suppress["estimate"]
                    ][:SECTION_CAPS["estimate"]]

    counts = {
        "missed": len(missed),
        "membership": len(memberships),
        "sleeping": len(sleeping),
        "estimate": len(estimates),
    }
    print(f"Today's counts — missed: {counts['missed']}, "
          f"membership: {counts['membership']}, sleeping: {counts['sleeping']}, "
          f"estimate: {counts['estimate']}")

    # Hot leads — customers who called us back since first rec
    hot_leads: list[dict] = []
    for kind_label, rows in (("missed", missed), ("membership", memberships),
                              ("sleeping", sleeping), ("estimate", estimates)):
        for r in rows:
            cid = r.get("customer_id")
            if cid and (outreach_map.get(cid) or {}).get("called_back_at"):
                hot_leads.append({"customer_name": r.get("customer_name"), "kind": kind_label})
    print(f"Hot leads detected: {len(hot_leads)}")

    today_str = date.today().strftime("%A, %B %d, %Y")
    html_body = _render_notification_html(counts, hot_leads, today_str, _CALL_LIST_URL)

    # Plain-text fallback
    total = sum(counts.values())
    text_lines = [
        f"Good morning, Fey — {today_str}.",
        "",
        f"Your call list: {counts['missed']} missed calls, "
        f"{counts['membership']} membership opps, {counts['sleeping']} sleeping customers, "
        f"{counts.get('estimate', 0)} aging estimates ({total} total).",
    ]
    if hot_leads:
        text_lines.append("")
        text_lines.append("HOT LEADS — call these first:")
        for h in hot_leads[:5]:
            text_lines.append(f"  · {h['customer_name']} ({h['kind']})")
    if _CALL_LIST_URL:
        text_lines.append("")
        text_lines.append(f"Open your live call list: {_CALL_LIST_URL}/Call_List")
    text_body = "\n".join(text_lines)

    # ---- Build + send the email ----
    _fey_list = parse_recipients(os.environ.get("FEY_EMAIL_TO"))
    _brett_list = parse_recipients(os.environ.get("EMAIL_TO"))
    if _fey_list:
        TO_LIST = _fey_list
        CC_LIST = exclude(_brett_list, _fey_list)
    else:
        TO_LIST = _brett_list
        CC_LIST = []
    if not TO_LIST:
        print("No recipients configured — skipping send.")
        return 0

    subject = f"📞 Call list ready ({total}) — {today_str}"
    all_recipients = TO_LIST + CC_LIST
    print(f"Sending notification to {fmt_recipients(all_recipients)}…")
    from lib.email_send import send_email
    result = send_email(
        to=all_recipients,
        subject=subject,
        text=text_body,
        html=html_body,
    )
    print(f"Sent via {result['provider']}.")

    with db() as conn:
        record_send(conn, "csr_daily", all_recipients)

    # Record today's recommendations so the page's "Pending Xd" badges and
    # suppression work properly tomorrow. CSR_DRY_RUN bypasses for local
    # testing without polluting suppression state.
    log_rows: list[tuple] = []
    for r in memberships:
        cid = r.get("customer_id")
        key = dedup_key("membership", cid)
        log_rows.append(("membership", cid, None, key,
                         json.dumps({"customer_name": r.get("customer_name"),
                                     "install_value": float(r.get("install_value") or 0),
                                     "install_date": str(r.get("install_date") or "")})))
    for r in sleeping:
        cid = r.get("customer_id")
        key = dedup_key("sleeping", cid)
        log_rows.append(("sleeping", cid, None, key,
                         json.dumps({"customer_name": r.get("customer_name"),
                                     "loyal_revenue": float(r.get("loyal_revenue") or 0),
                                     "last_visit": str(r.get("last_visit") or "")})))
    for r in missed:
        cid = r.get("customer_id")
        call_id = r.get("id")
        key = dedup_key("missed", cid, call_id)
        log_rows.append(("missed", cid, call_id, key,
                         json.dumps({"customer_name": r.get("customer_name"),
                                     "call_type": r.get("call_type"),
                                     "received_on": str(r.get("received_on") or "")})))
    for r in estimates:
        cid = r.get("customer_id")
        est_id = r.get("id")
        key = dedup_key("estimate", cid, est_id)
        # call_id column re-used to hold estimate_id — dedup_key embeds
        # the type ("est" vs "call") so they don't collide.
        log_rows.append(("estimate", cid, est_id, key,
                         json.dumps({"customer_name": r.get("customer_name"),
                                     "estimate_id": est_id,
                                     "subtotal": float(r.get("subtotal") or 0),
                                     "age_days": int(r.get("age_days") or 0),
                                     "estimate_name": r.get("estimate_name")})))
    is_dry_run = os.environ.get("CSR_DRY_RUN", "").lower() in ("1", "true", "yes")
    if log_rows and not is_dry_run:
        with db() as conn:
            record_recommendations(conn, log_rows)
        print(f"Logged {len(log_rows)} recommendations to csr_recommendations.")
    elif log_rows:
        print("Skipping recommendation log (CSR_DRY_RUN env var) — suppression state unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
