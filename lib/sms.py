"""Twilio SMS layer for Pure Comfort.

Wraps Twilio's API behind a small surface so the rest of the codebase
doesn't import the SDK directly. Handles:

- Phone normalization to E.164 (last-10 → +1NNNNNNNNNN)
- Opt-out check before every outbound send (TCPA compliance)
- STOP-keyword detection on inbound (auto-add to sms_opt_outs)
- Customer lookup by phone (matches against invoices/calls history)
- Persisting every message to sms_messages
- Optional push to ServiceTitan customer notes (controlled per send)

All sends are no-ops in DRY-RUN mode (env: SMS_DRY_RUN=1). The body
gets composed and logged; nothing leaves the box. Use that for the
first ~24 hours after wiring this up to confirm we'd be sending what
we expect.

Required env vars (production):
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER       (E.164, e.g. +18475550100)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

# Twilio is imported lazily so test scripts can use the helpers
# (e.g. normalize_phone, is_opted_out) without requiring the SDK.

# STOP-keyword set per TCPA / CTIA guidelines.
_STOP_KEYWORDS = frozenset({
    "stop", "stopall", "unsubscribe", "cancel", "end", "quit",
})


# ---------- phone helpers ----------

def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Normalize any phone string to E.164 (+1NNNNNNNNNN for US/CA).

    Returns None if we can't get to 10 digits.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"+1{digits}"


def last_ten(raw: Optional[str]) -> Optional[str]:
    """Extract last-10 digits for fuzzy matching against stored phones."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    return digits[-10:] if len(digits) >= 10 else None


def is_stop_message(body: str) -> bool:
    """Detect TCPA stop keywords. Conservative — single-word match only."""
    if not body:
        return False
    cleaned = body.strip().lower()
    # Single-word check: "STOP" / "stop." / "Stop!" all count; full
    # sentences like "stop calling me please" do not auto-opt-out.
    word = re.sub(r"[^a-z]", "", cleaned)
    return word in _STOP_KEYWORDS


# ---------- opt-out check ----------

def is_opted_out(conn, phone: str) -> bool:
    """Return True if this phone has opted out and must NOT receive sends."""
    normalized = normalize_phone(phone)
    if not normalized:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sms_opt_outs WHERE phone = %s LIMIT 1",
            (normalized,),
        )
        return cur.fetchone() is not None


def record_opt_out(conn, phone: str, source: str = "inbound_stop") -> None:
    normalized = normalize_phone(phone)
    if not normalized:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sms_opt_outs (phone, source)
            VALUES (%s, %s) ON CONFLICT (phone) DO NOTHING
            """,
            (normalized, source),
        )
    conn.commit()


# ---------- customer matching ----------

def match_customer_by_phone(conn, phone: str) -> Optional[int]:
    """Find a customer_id whose phone matches (last-10 fuzzy)."""
    ten = last_ten(phone)
    if not ten:
        return None
    with conn.cursor() as cur:
        # Try customer_contacts table first (has dedicated phone column),
        # then fall back to inbound call history (from_phone → customer_id).
        cur.execute(
            """
            SELECT customer_id FROM customer_contacts
            WHERE customer_id IS NOT NULL
              AND RIGHT(REGEXP_REPLACE(phone, '\\D', '', 'g'), 10) = %s
            ORDER BY customer_id LIMIT 1
            """,
            (ten,),
        )
        row = cur.fetchone()
        if row and row["customer_id"]:
            return int(row["customer_id"])
        cur.execute(
            """
            SELECT customer_id FROM calls
            WHERE customer_id IS NOT NULL
              AND RIGHT(REGEXP_REPLACE(from_phone, '\\D', '', 'g'), 10) = %s
            ORDER BY received_on DESC LIMIT 1
            """,
            (ten,),
        )
        row = cur.fetchone()
        return int(row["customer_id"]) if row and row["customer_id"] else None


# ---------- send ----------

class SmsError(RuntimeError):
    pass


def _get_twilio_client():
    """Lazy-import Twilio. Returns a configured Client or raises."""
    try:
        from twilio.rest import Client  # type: ignore
    except ImportError as exc:
        raise SmsError(
            "twilio package not installed — add `twilio` to requirements.txt"
        ) from exc
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        raise SmsError(
            "TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN must be set"
        )
    return Client(sid, token)


def _from_number() -> str:
    n = os.environ.get("TWILIO_FROM_NUMBER")
    if not n:
        raise SmsError("TWILIO_FROM_NUMBER not set")
    norm = normalize_phone(n)
    if not norm:
        raise SmsError(f"TWILIO_FROM_NUMBER not parseable: {n}")
    return norm


def dry_run_enabled() -> bool:
    return os.environ.get("SMS_DRY_RUN", "0") == "1"


def send_sms(
    conn,
    *,
    to_phone: str,
    body: str,
    channel: str = "manual",
    customer_id: Optional[int] = None,
    related_call_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
    sent_by: str = "system",
    post_to_st: bool = True,
    st_client: Any = None,
) -> dict:
    """Send an SMS via Twilio; persist to sms_messages; push to ST notes.

    Returns the persisted sms_messages row (as dict).

    - Opt-out check: returns early without sending if recipient is opted out.
    - Dry-run: persists row with status='queued' + twilio_sid=NULL.
    - ST notes push: only attempted if customer_id is set AND post_to_st.
    """
    to_norm = normalize_phone(to_phone)
    if not to_norm:
        raise SmsError(f"to_phone not parseable: {to_phone}")

    if is_opted_out(conn, to_norm):
        return _persist(
            conn, direction="outbound", channel=channel,
            from_phone=_from_number_safe(), to_phone=to_norm, body=body,
            customer_id=customer_id, related_call_id=related_call_id,
            campaign_id=campaign_id,
            twilio_sid=None, status="opted_out",
            error_code="OPTED_OUT", error_message="recipient on sms_opt_outs",
            sent_by=sent_by, posted_to_st=False, raw=None,
        )

    from_n = _from_number_safe()

    if dry_run_enabled():
        return _persist(
            conn, direction="outbound", channel=channel,
            from_phone=from_n, to_phone=to_norm, body=body,
            customer_id=customer_id, related_call_id=related_call_id,
            campaign_id=campaign_id,
            twilio_sid=None, status="dry_run",
            error_code=None, error_message=None,
            sent_by=sent_by, posted_to_st=False,
            raw={"dry_run": True, "body": body},
        )

    # Real send
    client = _get_twilio_client()
    try:
        msg = client.messages.create(
            to=to_norm,
            from_=_from_number(),
            body=body,
        )
    except Exception as exc:
        return _persist(
            conn, direction="outbound", channel=channel,
            from_phone=from_n, to_phone=to_norm, body=body,
            customer_id=customer_id, related_call_id=related_call_id,
            campaign_id=campaign_id,
            twilio_sid=None, status="failed",
            error_code=type(exc).__name__, error_message=str(exc)[:500],
            sent_by=sent_by, posted_to_st=False, raw=None,
        )

    row = _persist(
        conn, direction="outbound", channel=channel,
        from_phone=from_n, to_phone=to_norm, body=body,
        customer_id=customer_id, related_call_id=related_call_id,
        campaign_id=campaign_id,
        twilio_sid=msg.sid, status=str(msg.status or "queued"),
        error_code=None, error_message=None,
        sent_by=sent_by, posted_to_st=False,
        raw={"sid": msg.sid, "status": str(msg.status)},
    )

    if post_to_st and customer_id and st_client is not None:
        try:
            note = _format_st_note("OUT", body, sent_by)
            st_client.create_customer_note(customer_id, note)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sms_messages SET posted_to_st = TRUE WHERE id = %s",
                    (row["id"],),
                )
            conn.commit()
            row["posted_to_st"] = True
        except Exception as exc:
            # Don't fail the send if note-push fails; log it.
            print(f"[sms] ST note push failed for msg {row['id']}: {exc}")

    return row


def record_inbound(
    conn,
    *,
    from_phone: str,
    to_phone: str,
    body: str,
    twilio_sid: Optional[str] = None,
    raw: Optional[dict] = None,
    st_client: Any = None,
) -> dict:
    """Record an inbound SMS; handle STOP; match to customer; push to ST.

    Returns the persisted sms_messages row.
    """
    from_norm = normalize_phone(from_phone) or from_phone
    to_norm = normalize_phone(to_phone) or to_phone

    # STOP detection — log opt-out BEFORE persisting the message so
    # any concurrent send is blocked.
    if is_stop_message(body):
        record_opt_out(conn, from_norm, source="inbound_stop")

    customer_id = match_customer_by_phone(conn, from_norm)

    # Reply attribution: was this inbound preceded by an outbound from a
    # campaign in the last 14 days? If so, tag this inbound with that
    # campaign_id and increment the campaign's reply_count.
    attributed_campaign_id = None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT campaign_id FROM sms_messages
            WHERE direction = 'outbound'
              AND campaign_id IS NOT NULL
              AND RIGHT(REGEXP_REPLACE(to_phone, '\\D', '', 'g'), 10)
                  = RIGHT(REGEXP_REPLACE(%s, '\\D', '', 'g'), 10)
              AND sent_at >= NOW() - INTERVAL '14 days'
            ORDER BY sent_at DESC LIMIT 1
            """,
            (from_norm,),
        )
        result = cur.fetchone()
        if result:
            attributed_campaign_id = result["campaign_id"]

    row = _persist(
        conn, direction="inbound", channel="inbound_reply",
        from_phone=from_norm, to_phone=to_norm, body=body,
        customer_id=customer_id, related_call_id=None,
        campaign_id=attributed_campaign_id,
        twilio_sid=twilio_sid, status="received",
        error_code=None, error_message=None,
        sent_by=None, posted_to_st=False, raw=raw,
    )

    # Bump the campaign's reply_count for visibility on SMS admin page
    if attributed_campaign_id:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sms_campaigns SET reply_count = reply_count + 1 WHERE id = %s",
                (attributed_campaign_id,),
            )
        conn.commit()

    # Unmatched bucket
    if customer_id is None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sms_unmatched (message_id, from_phone)
                VALUES (%s, %s)
                """,
                (row["id"], from_norm),
            )
        conn.commit()

    # Push to ST if matched
    if customer_id and st_client is not None:
        try:
            note = _format_st_note("IN", body, sender=None)
            st_client.create_customer_note(customer_id, note)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sms_messages SET posted_to_st = TRUE WHERE id = %s",
                    (row["id"],),
                )
            conn.commit()
            row["posted_to_st"] = True
        except Exception as exc:
            print(f"[sms] ST note push failed for inbound {row['id']}: {exc}")

    return row


# ---------- internal helpers ----------

def _from_number_safe() -> Optional[str]:
    try:
        return _from_number()
    except SmsError:
        return None


def _format_st_note(direction: str, body: str, sender: Optional[str]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    who = f" by {sender}" if sender else ""
    return f"[SMS {direction} {ts}{who}]\n{body}"


def _persist(conn, **fields) -> dict:
    raw_val = fields.pop("raw", None)
    raw_json = json.dumps(raw_val) if raw_val is not None else None
    fields.setdefault("campaign_id", None)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sms_messages
              (direction, channel, from_phone, to_phone, body,
               customer_id, related_call_id, campaign_id, twilio_sid, status,
               error_code, error_message, sent_by, posted_to_st, raw)
            VALUES (%(direction)s, %(channel)s, %(from_phone)s, %(to_phone)s, %(body)s,
                    %(customer_id)s, %(related_call_id)s, %(campaign_id)s,
                    %(twilio_sid)s, %(status)s,
                    %(error_code)s, %(error_message)s, %(sent_by)s, %(posted_to_st)s, %(raw)s)
            RETURNING id, sent_at
            """,
            {**fields, "raw": raw_json},
        )
        result = cur.fetchone()
    conn.commit()
    return {**fields, "id": result["id"], "sent_at": result["sent_at"]}
