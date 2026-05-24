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

from lib.database import db  # noqa: E402
from lib.email_utils import exclude, header as fmt_recipients, parse_recipients  # noqa: E402
from lib.servicetitan import ServiceTitanClient  # noqa: E402
from lib.sync import sync_calls  # noqa: E402

REQUIRED = (
    "SMTP_USER", "SMTP_PASSWORD",
    "ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET",
    "DATABASE_URL",
)
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    sys.exit(f"Missing env vars: {', '.join(missing)}")

# Recipients (both env vars accept comma- or semicolon-separated lists):
#   FEY_EMAIL_TO  → primary To: (Fey, can be multiple)
#   EMAIL_TO      → CC: (Brett + anyone else for visibility)
# If FEY_EMAIL_TO isn't set, fall back to EMAIL_TO as the To: with no CC.
_fey_list = parse_recipients(os.environ.get("FEY_EMAIL_TO"))
_brett_list = parse_recipients(os.environ.get("EMAIL_TO"))
if _fey_list:
    TO_LIST = _fey_list
    CC_LIST = exclude(_brett_list, _fey_list)  # don't duplicate if Fey's in both
else:
    TO_LIST = _brett_list
    CC_LIST = []
if not TO_LIST:
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


# ---------- contact lookup with cache ----------

_contact_cache: dict[int, tuple[str, str]] = {}


def lookup_contact(client: ServiceTitanClient, cid: int | None) -> tuple[str, str]:
    """Returns (phone, email). Best-effort, per-customer cached for this run."""
    if not cid:
        return "", ""
    if cid in _contact_cache:
        return _contact_cache[cid]
    try:
        contacts = client.get_customer_contacts(cid)
    except Exception:
        contacts = []
    phones = sorted(
        (c for c in contacts
         if c.get("value") and c.get("type") in ("MobilePhone", "Phone")),
        key=lambda c: 0 if c["type"] == "MobilePhone" else 1,
    )
    emails = [c["value"] for c in contacts
              if c.get("value") and c.get("type") == "Email"]
    out = (phones[0]["value"] if phones else "", emails[0] if emails else "")
    _contact_cache[cid] = out
    return out


# ---------- data loaders ----------

def load_membership_opps(conn) -> list[dict]:
    """Install customers in the last 14 days with no active membership."""
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
                (
                  SELECT string_agg(DISTINCT it.description, '; '
                                    ORDER BY it.description)
                  FROM invoice_items it
                  WHERE it.invoice_id = i.id AND it.item_type = 'Equipment'
                ) AS equipment
              FROM invoices i
              WHERE i.invoice_date >= CURRENT_DATE - INTERVAL '14 days'
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
              SELECT DISTINCT ON (customer_id)
                customer_id, summary, invoice_date
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              ORDER BY customer_id, invoice_date DESC
            )
            SELECT
              l.customer_id, l.customer_name,
              l.loyal_invoices, l.loyal_revenue, l.last_visit,
              rs.summary AS last_summary
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


def load_missed_calls(conn) -> list[dict]:
    """Inbound abandoned + unbooked calls from the last 24 hours."""
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
            )
            SELECT
              c.id, c.received_on, c.from_phone, c.customer_id, c.customer_name,
              c.call_type, c.reason, c.duration_seconds, c.agent_name,
              h.invoices AS lifetime_invoices,
              h.lifetime_revenue,
              h.last_visit
            FROM calls c
            LEFT JOIN history h ON h.customer_id = c.customer_id
            WHERE c.direction = 'Inbound'
              AND c.call_type IN ('Abandoned', 'Unbooked')
              AND c.created_on >= NOW() - INTERVAL '24 hours'
            ORDER BY c.received_on DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- HTML rendering ----------

CARD_STYLES = {
    "membership": ("#0066EE", "🤝 Membership opportunities", "Recent install customers without a maintenance plan — warmest possible upsell."),
    "sleeping":   ("#F2A93B", "💤 Sleeping customers",      "High-value customers who've gone quiet. They already know us; a friendly call wins them back."),
    "missed":     ("#F34039", "📞 Missed calls — call back today", "Inbound calls in the last 24h that didn't book. These people called US."),
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
        ("Pivot", "\"The reason I called: most manufacturers require an annual tune-up to keep your warranty valid. A lot of our install customers join our maintenance plan so we handle it automatically — covers two tune-ups a year, priority scheduling, and 10% off any services.\""),
        ("Special offer", "\"And because you just installed, I can get you 50% off your first year. That makes it a no-brainer — and after that you can keep it or cancel anytime.\""),
        ("Close", "\"Want me to lock that in today, or text you the details to look over first?\""),
        ("If \"too expensive\"", "\"At 50% off the first year, it pays for itself the first time anything goes wrong — and it keeps your warranty in force, which is usually the bigger number.\""),
        ("If \"let me think about it\"", "\"Totally fair. Can I text you the plan details and the first-year discount? That way you've got it whenever you're ready.\""),
    ],
    "sleeping": [
        ("Opener", "\"Hi [name], this is Fey at Pure Comfort. I was going through our records and saw it's been a while — last time we were out we [last service]. Just wanted to check in: how's the system been holding up?\""),
        ("Listen first", "Don't go straight to a sale. Let them talk about the system, the weather, whatever. The reconnect matters more than the pitch."),
        ("Soft offer", "\"We're about to head into [cooling/heating] season — most of our regulars are getting a tune-up about now to catch anything before it turns into a breakdown. Want to get on the calendar?\""),
        ("Plumbing FYI", "\"And while I have you — one thing that's new since we last talked: we now do full-service plumbing too. So next time you've got a leaky faucet, a slow drain, or the water heater's getting up there in age, give us a call instead of hunting for a plumber. Same team, same standards.\""),
        ("If \"not right now\"", "\"No problem at all. Mind if I send a text in a couple of months as a reminder? You know where to find us — HVAC or plumbing — anytime something comes up.\""),
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

# How long after a recommendation goes out before we'll re-recommend the same
# customer/call. Two-tier:
#   "full"  → we have evidence a real conversation happened (long outbound call)
#   "short" → outbound attempts exist but were voicemail/no-answer; nudge sooner
SUPPRESS_DAYS_FULL = {
    "membership": 14,
    "sleeping":   45,
    "missed":     2,
}
SUPPRESS_DAYS_SHORT = {  # used when only voicemails/no-answers detected
    "membership": 4,
    "sleeping":   10,
    "missed":     1,
}

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
        #    since their first recommendation. We compute attempt count + a
        #    conversation flag (long-enough outbound call) so we can tell
        #    voicemails apart from real conversations.
        cur.execute(
            f"""
            WITH first_recs AS (
              SELECT customer_id, MIN(sent_at) AS first_rec_at
              FROM csr_recommendations
              WHERE sent_at >= %s AND customer_id IS NOT NULL
              GROUP BY customer_id
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
            JOIN first_recs fr ON fr.customer_id = c.customer_id
            WHERE c.created_on >= fr.first_rec_at
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

    # 3. Decide suppression.
    #    - called_back: NEVER suppress — hot lead, surface immediately
    #    - had a real conversation: full window (she did her job)
    #    - only voicemails / no-answers: SHORT window (nudge to try again)
    #    - no outreach detected: full window (standard time-based)
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
            window = SUPPRESS_DAYS_FULL.get(kind)

        if window is None:
            continue
        if (now - r["last_seen"]).days < window:
            suppress.setdefault(kind, set()).add(key)

    return {
        "suppress": suppress,
        "first_seen": first_seen,
        "outreach": outreach,
    }


def dedup_key(kind: str, customer_id: int | None, call_id: int | None = None) -> str:
    """Stable key for de-duping recommendations across days."""
    if kind == "missed" and call_id is not None:
        return f"{kind}:call:{call_id}"
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


def html_section(kind: str, rows_html: str, count: int) -> str:
    color, title, sub = CARD_STYLES[kind]
    badge = f"<span style='background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:13px;margin-left:8px'>{count}</span>"
    # Only render the script when there's at least one row to call — saves
    # the "nothing new today" section from being a wall of unused script.
    script_html = render_call_script(kind) if count > 0 else ""
    return f"""
<div style="border:1px solid #e5e7eb;border-left:6px solid {color};border-radius:8px;padding:16px 20px;margin-bottom:24px;background:white">
  <h2 style="margin:0 0 4px 0;color:{color};font-size:18px">{title} {badge}</h2>
  <p style="margin:0 0 12px 0;color:#555;font-size:13px">{sub}</p>
  {script_html}
  {rows_html or '<p style="color:#888;margin:6px 0 0">Nothing new — focus on the other sections today.</p>'}
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
                      outreach_info: dict | None = None) -> str:
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
    return f"""
<div style="border-top:1px solid #f0f0f0;padding:10px 0">
  <div style="font-size:15px;font-weight:600;color:#111">{escape(name)}{badge}</div>
  <div style="font-size:14px;margin:2px 0 4px">{phone_html}{email_html}</div>
  <div style="font-size:13px;color:#333">{primary_line}</div>
  <div style="font-size:12px;color:#777;margin-top:2px">{history_line}</div>
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

def main() -> int:
    client = ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"], tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"], client_secret=os.environ["ST_CLIENT_SECRET"],
    )

    print("Syncing latest calls so missed-call section reflects last 24h…")
    with db() as conn:
        try:
            sync_calls(client, conn, progress=lambda m: print(f"  · {m}"))
        except Exception as exc:
            print(f"  · sync_calls failed (non-fatal): {exc}")

        print("Loading recommendation history + outreach signals…")
        state = load_recommendation_state(conn)
        suppress = state["suppress"]
        first_seen_map = state["first_seen"]
        outreach_map = state["outreach"]  # customer_id -> {called_at, called_back_at}
        print(f"  · suppressing — memberships: {len(suppress['membership'])}, "
              f"sleeping: {len(suppress['sleeping'])}, missed: {len(suppress['missed'])}")
        print(f"  · outreach detected for {len(outreach_map)} customers since first rec")

        print("Loading membership opportunities…")
        memberships_all = load_membership_opps(conn)
        memberships = [
            r for r in memberships_all
            if dedup_key("membership", r.get("customer_id")) not in suppress["membership"]
        ]
        print(f"  · {len(memberships)} non-member install customers (filtered from {len(memberships_all)})")

        print("Loading sleeping customers…")
        # Pull a wider pool so suppression doesn't shrink the visible list.
        sleeping_all = load_sleeping_customers(conn, limit=40)
        sleeping = [
            r for r in sleeping_all
            if dedup_key("sleeping", r.get("customer_id")) not in suppress["sleeping"]
        ][:15]
        print(f"  · {len(sleeping)} sleeping customers (filtered from {len(sleeping_all)})")

        print("Loading missed calls…")
        missed_all = load_missed_calls(conn)
        missed = [
            r for r in missed_all
            if dedup_key("missed", r.get("customer_id"), r.get("id")) not in suppress["missed"]
        ]
        print(f"  · {len(missed)} missed calls in last 24h (filtered from {len(missed_all)})")

    # ---- Enrich with contact info ----
    print("Looking up contact info…")
    for r in memberships:
        r["phone"], r["email"] = lookup_contact(client, r.get("customer_id"))
    for r in sleeping:
        r["phone"], r["email"] = lookup_contact(client, r.get("customer_id"))
    for r in missed:
        # Use call's from_phone first; only API-lookup when customer matched
        if r.get("customer_id"):
            r["phone"], r["email"] = lookup_contact(client, r["customer_id"])
            if not r["phone"]:
                r["phone"] = r.get("from_phone") or ""
        else:
            r["phone"] = r.get("from_phone") or ""
            r["email"] = ""

    # ---- Build HTML sections ----
    def _row_status(customer_id: int | None, dkey: str) -> dict:
        """Compute badge inputs for one row."""
        return {
            "pending_days": _pending_days(first_seen_map.get(dkey)),
            "outreach_info": outreach_map.get(customer_id) if customer_id else None,
        }

    mem_rows_html = "".join(
        html_customer_row(
            name=r.get("customer_name") or "Customer",
            phone=r.get("phone") or "",
            email=r.get("email") or "",
            **_row_status(r.get("customer_id"), dedup_key("membership", r.get("customer_id"))),
            primary_line=(
                f"<b>{fmt_money(r['install_value'])}</b> install on "
                f"{r['install_date']:%a %b %d} ({days_ago(r['install_date'])}) "
                f"&middot; {escape(r.get('business_unit_name') or 'no BU')}"
                + (f" &middot; <i>{escape(short(r['equipment'], 80))}</i>" if r.get('equipment') else "")
            ),
            history_line=(
                f"Lifetime: {fmt_money(r.get('lifetime_revenue'))} across "
                f"{int(r.get('lifetime_invoices') or 0)} visits"
                + (f" · customer since {r['first_visit']:%b %Y}" if r.get('first_visit') else "")
                + " — pitch the maintenance plan; they just spent and trust us"
            ),
        )
        for r in memberships
    )

    sleep_rows_html = "".join(
        html_customer_row(
            name=r.get("customer_name") or "Customer",
            phone=r.get("phone") or "",
            email=r.get("email") or "",
            **_row_status(r.get("customer_id"), dedup_key("sleeping", r.get("customer_id"))),
            primary_line=(
                f"Last visit <b>{days_ago(r['last_visit'])}</b> ({r['last_visit']:%b %d, %Y})"
                + (f" &middot; <i>{escape(short(r['last_summary'], 80))}</i>" if r.get('last_summary') else "")
            ),
            history_line=(
                f"Loyal-period spend: {fmt_money(r.get('loyal_revenue'))} across "
                f"{int(r.get('loyal_invoices') or 0)} visits "
                "— offer a tune-up or seasonal check-in"
            ),
        )
        for r in sleeping
    )

    missed_rows_html = "".join(
        html_customer_row(
            name=r.get("customer_name") or "Unknown caller",
            phone=r.get("phone") or "",
            email=r.get("email") or "",
            **_row_status(r.get("customer_id"), dedup_key("missed", r.get("customer_id"), r.get("id"))),
            primary_line=(
                f"<b>{escape(r['call_type'])}</b> at "
                f"{r['received_on']:%-I:%M %p} on {r['received_on']:%a %b %d}"
                + (f" &middot; reason: <i>{escape(r['reason'])}</i>" if r.get('reason') else "")
                + (f" &middot; CSR: {escape(r['agent_name'])}" if r.get('agent_name') else "")
            ),
            history_line=(
                (f"Existing customer · lifetime {fmt_money(r.get('lifetime_revenue'))} "
                 f"across {int(r.get('lifetime_invoices') or 0)} visits · "
                 f"last visit {days_ago(r.get('last_visit'))}")
                if r.get("customer_id")
                else "No customer match — new lead from this number"
            ),
        )
        for r in missed
    )

    today_str = date.today().strftime("%A, %B %d, %Y")
    total_calls = len(memberships) + len(sleeping) + len(missed)
    subject = f"☎️  Daily call list ({total_calls}) — {today_str}"

    suppressed_total = (
        len(suppress["membership"]) + len(suppress["sleeping"]) + len(suppress["missed"])
    )
    suppression_note = (
        f" <span style='color:#888'>· skipped {suppressed_total} already on recent lists</span>"
        if suppressed_total else ""
    )

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f7f7;margin:0;padding:24px">
<div style="max-width:720px;margin:0 auto">
  <h1 style="margin:0 0 4px 0;color:#00214D;font-size:22px">Good morning, Fey 👋</h1>
  <p style="margin:0 0 20px 0;color:#555;font-size:14px">
    Here's your call list for {today_str}.
    <b>{len(memberships)}</b> membership opportunities ·
    <b>{len(sleeping)}</b> sleeping customers ·
    <b>{len(missed)}</b> missed calls.{suppression_note}<br>
    Tap-to-dial phones. Badges (inferred from call duration):
    <span style="color:#9A3412">📥 Called us back</span> (hottest, surfaced even within cooldown) ·
    <span style="color:#065F46">📞 Spoke</span> (real conversation, full cooldown) ·
    <span style="color:#991B1B">📨 Voicemail / 🔕 No answer</span> (try again sooner) ·
    <span style="color:#92400E">🔁 Pending</span> (rec'd before, no outreach detected).
  </p>
  {html_section('missed', missed_rows_html, len(missed))}
  {html_section('membership', mem_rows_html, len(memberships))}
  {html_section('sleeping', sleep_rows_html, len(sleeping))}
  <p style="color:#888;font-size:11px;margin-top:24px;text-align:center">
    Generated automatically from ServiceTitan. Questions? Check the dashboard.
  </p>
</div>
</body></html>"""

    # ---- Plain-text fallback ----
    def text_section(label: str, rows: list[dict], render) -> str:
        body = "\n".join(render(r) for r in rows) if rows else "  (nothing new today)"
        return f"\n=== {label} ({len(rows)}) ===\n{body}\n"

    text_parts = [
        f"Good morning, Fey — call list for {today_str}.\n"
        f"{len(memberships)} membership opps · {len(sleeping)} sleeping · {len(missed)} missed calls.\n",
    ]
    text_parts.append(text_section(
        "MISSED CALLS (last 24h)", missed,
        lambda r: (
            f"- {fmt_phone(r.get('phone'))}  "
            f"{(r.get('customer_name') or 'Unknown'):28s}  "
            f"{r['call_type']:9s}  {r['received_on']:%-I:%M%p %a} "
            + (f"  lifetime {fmt_money(r.get('lifetime_revenue'))}" if r.get('customer_id') else "  (new lead)")
        ),
    ))
    text_parts.append(text_section(
        "MEMBERSHIP OPPORTUNITIES (installs last 14d, no plan)", memberships,
        lambda r: (
            f"- {fmt_phone(r.get('phone'))}  "
            f"{(r.get('customer_name') or '?')[:32]:32s}  "
            f"{fmt_money(r['install_value'])} install {days_ago(r['install_date']):>10s}  "
            f"lifetime {fmt_money(r.get('lifetime_revenue'))}"
        ),
    ))
    text_parts.append(text_section(
        "SLEEPING CUSTOMERS (top 15 by lifetime value)", sleeping,
        lambda r: (
            f"- {fmt_phone(r.get('phone'))}  "
            f"{(r.get('customer_name') or '?')[:32]:32s}  "
            f"last {days_ago(r['last_visit']):>10s}  "
            f"loyal spend {fmt_money(r.get('loyal_revenue'))}"
        ),
    ))
    text = "\n".join(text_parts)

    # ---- Send ----
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = fmt_recipients(TO_LIST)
    if CC_LIST:
        msg["Cc"] = fmt_recipients(CC_LIST)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    all_recipients = TO_LIST + CC_LIST
    print(f"Sending to {fmt_recipients(all_recipients)}…")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg, to_addrs=all_recipients)
    print("Sent.")

    # Record today's batch AFTER the email successfully sends, so suppression
    # only kicks in for recommendations that actually reached the inbox.
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
    if log_rows:
        with db() as conn:
            record_recommendations(conn, log_rows)
        print(f"Logged {len(log_rows)} recommendations to csr_recommendations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
