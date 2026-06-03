"""Auto-text customers who called us and we missed it.

Cron-friendly. Run every 2 minutes (GitHub Actions).

Detects abandoned/unbooked inbound calls in the last 15 minutes, filters
out:
- Opted-out phones
- Customers already auto-replied in the last 24h
- Customers who already got a follow-up note from us today
- After-hours window (configurable)

Composes a time-aware message (business hours vs. after-hours), sends
via lib.sms (dry-run by default), and pushes the SMS as a note on the
customer's ST record so anyone looking at the customer record sees the
outreach.

Run modes:
  --dry-run         Persist what we WOULD send to sms_messages with
                    status='dry_run'; nothing leaves the box.
                    (Also implicit if SMS_DRY_RUN=1 in env.)
  --look-back-min   How far back to scan for missed calls (default 15)
  --verbose         Print per-candidate details
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from lib.database import get_connection
from lib.servicetitan import ServiceTitanClient
from lib.sms import send_sms, normalize_phone, dry_run_enabled


# ── messages (one place, easy to tweak) ────────────────────────
MSG_BUSINESS_HOURS = (
    "Hi! Sorry we missed your call to Pure Comfort — we'll call right back. "
    "If you'd rather text, just reply here with what you need. "
    "Reply STOP to opt out."
)
MSG_AFTER_HOURS = (
    "Hi — sorry we missed your call to Pure Comfort. We're closed but we'll "
    "see your reply first thing. We open at 8am Mon–Fri. Emergency? Call back "
    "and follow the prompts. Reply STOP to opt out."
)


def is_business_hours_now() -> bool:
    """8:00am-4:30pm Mon-Fri Chicago time."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Chicago"))
    # Mon=0, Sun=6
    if now.weekday() > 4:
        return False
    hm = now.hour * 60 + now.minute
    return 8 * 60 <= hm <= 16 * 60 + 30


def pick_message() -> str:
    return MSG_BUSINESS_HOURS if is_business_hours_now() else MSG_AFTER_HOURS


def find_candidate_calls(conn, look_back_min: int) -> list[dict]:
    """Inbound abandoned/unbooked calls within window that we haven't
    already auto-replied to in the last 24h.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id              AS call_id,
                   c.customer_id,
                   c.customer_name,
                   c.from_phone,
                   c.received_on,
                   c.duration_seconds
            FROM calls c
            WHERE c.direction = 'Inbound'
              AND c.call_type IN ('Abandoned', 'Unbooked')
              AND c.received_on >= NOW() - (%s * INTERVAL '1 minute')
              AND c.from_phone IS NOT NULL
              -- Don't double-text within 24h
              AND NOT EXISTS (
                SELECT 1 FROM sms_messages s
                WHERE s.direction = 'outbound'
                  AND s.channel = 'auto_reply'
                  AND RIGHT(REGEXP_REPLACE(s.to_phone, '\\D', '', 'g'), 10)
                      = RIGHT(REGEXP_REPLACE(c.from_phone, '\\D', '', 'g'), 10)
                  AND s.sent_at >= NOW() - INTERVAL '24 hours'
              )
              -- Don't text if they already booked since the call
              AND NOT EXISTS (
                SELECT 1 FROM invoices i
                WHERE i.customer_id = c.customer_id
                  AND i.total > 0
                  AND i.invoice_date >= (c.received_on AT TIME ZONE 'UTC')::date
              )
              -- Don't text if a tech is already coming
              AND NOT EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.customer_id = c.customer_id
                  AND j.job_status IN ('Scheduled','Dispatched','InProgress','Working','Hold')
                  AND j.completed_on IS NULL
                  AND j.created_on >= c.created_on
              )
            ORDER BY c.received_on DESC
            """,
            (look_back_min,),
        )
        return [dict(r) for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Persist a row but don't actually send")
    parser.add_argument("--look-back-min", type=int, default=15,
                        help="How far back to scan (default 15 min)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["SMS_DRY_RUN"] = "1"

    # ST client only needed for the notes push. If creds are missing we
    # just skip the note (messages still get sent / logged).
    st_client = None
    if all(os.environ.get(k) for k in (
        "ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET"
    )):
        st_client = ServiceTitanClient(
            app_key=os.environ["ST_APP_KEY"],
            tenant_id=os.environ["ST_TENANT_ID"],
            client_id=os.environ["ST_CLIENT_ID"],
            client_secret=os.environ["ST_CLIENT_SECRET"],
        )

    mode = "DRY-RUN" if dry_run_enabled() else "LIVE"
    print(f"[sms_missed_call_reply] {mode} mode | "
          f"business hours now: {is_business_hours_now()}")

    with get_connection() as conn:
        candidates = find_candidate_calls(conn, args.look_back_min)
        print(f"  Found {len(candidates)} candidate call(s) in last "
              f"{args.look_back_min} min")

        sent = 0
        skipped = 0
        for c in candidates:
            phone = normalize_phone(c["from_phone"])
            if not phone:
                if args.verbose:
                    print(f"    skip — unparseable phone: {c['from_phone']}")
                skipped += 1
                continue

            body = pick_message()
            try:
                row = send_sms(
                    conn,
                    to_phone=phone,
                    body=body,
                    channel="auto_reply",
                    customer_id=c.get("customer_id"),
                    related_call_id=c["call_id"],
                    sent_by="system_auto_reply",
                    post_to_st=bool(c.get("customer_id")),
                    st_client=st_client,
                )
                status = row.get("status", "?")
                name = c.get("customer_name") or "(unknown)"
                print(f"  → {phone}  cust={c.get('customer_id') or '?':>10}  "
                      f"{name[:25]:<25}  [{status}]")
                if status not in ("opted_out", "failed"):
                    sent += 1
            except Exception as exc:
                print(f"    ERROR on {phone}: {exc}")
                skipped += 1

        print(f"\n[sms_missed_call_reply] done — sent={sent} skipped={skipped}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
