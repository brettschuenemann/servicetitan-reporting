"""Poll Twilio for new inbound SMS, record + push to ST notes.

Cron-friendly. Run every 2 minutes (GitHub Actions).

Tracks the high-water mark of received messages in `sync_state` (entity
key: 'sms_inbound_last_sid_received'), so each run only pulls messages
strictly newer than the previous run's most-recent.

For each inbound:
- Handle STOP keyword (auto-add to sms_opt_outs)
- Match phone → customer (last-10 fuzzy)
- Insert into sms_messages
- If matched: push as note to the customer's ST record
- If unmatched: drop into sms_unmatched bucket for Fey to link

Twilio polling note: their /Messages endpoint supports DateSentAfter
filtering. We use DateSentAfter with the sync_state cursor.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from lib.database import get_connection, get_sync_state, set_sync_state
from lib.servicetitan import ServiceTitanClient
from lib.sms import record_inbound


SYNC_STATE_KEY = "sms_inbound"


def _twilio_client():
    """Lazy-import; raises if creds missing."""
    from twilio.rest import Client  # type: ignore
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        raise RuntimeError(
            "TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN required for inbound poll"
        )
    return Client(sid, token)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--look-back-min", type=int, default=10,
                        help="Look back this many minutes on first poll "
                             "(after that, uses sync_state cursor).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Quick env check — bail early if Twilio not configured (cron-safe)
    if not (os.environ.get("TWILIO_ACCOUNT_SID")
            and os.environ.get("TWILIO_AUTH_TOKEN")):
        print("[sms_poll_inbound] Twilio creds not set — exiting cleanly")
        return 0

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

    client = _twilio_client()

    with get_connection() as conn:
        # Determine the cutoff
        state = get_sync_state(conn, SYNC_STATE_KEY)
        if state and state.get("last_modified_on"):
            since = datetime.fromisoformat(state["last_modified_on"])
        else:
            since = datetime.now(timezone.utc) - timedelta(minutes=args.look_back_min)

        print(f"[sms_poll_inbound] polling Twilio since {since.isoformat()}")

        # Twilio list_messages auto-paginates
        # date_sent_after expects datetime (UTC)
        messages = list(client.messages.list(
            date_sent_after=since,
            page_size=100,
        ))
        # Twilio returns NEW first; reverse for chronological
        messages = sorted(
            [m for m in messages if str(m.direction).startswith("inbound")],
            key=lambda m: m.date_sent or datetime.now(timezone.utc),
        )

        print(f"  Found {len(messages)} inbound message(s)")

        new_count = 0
        latest_ts = since
        for m in messages:
            # Dedup by twilio_sid
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM sms_messages WHERE twilio_sid = %s LIMIT 1",
                    (m.sid,),
                )
                if cur.fetchone():
                    continue

            row = record_inbound(
                conn,
                from_phone=m.from_ or "",
                to_phone=m.to or "",
                body=m.body or "",
                twilio_sid=m.sid,
                raw={
                    "sid": m.sid,
                    "status": str(m.status),
                    "num_segments": m.num_segments,
                    "price": m.price,
                    "date_sent": str(m.date_sent),
                },
                st_client=st_client,
            )

            matched = "MATCHED" if row.get("customer_id") else "UNMATCHED"
            print(f"  ← {m.from_}  {matched}  body='{(m.body or '')[:50]}'")
            new_count += 1
            if m.date_sent and m.date_sent > latest_ts:
                latest_ts = m.date_sent

        if new_count:
            # Bump cursor +1 second past the latest to avoid re-pulling
            set_sync_state(
                conn,
                SYNC_STATE_KEY,
                (latest_ts + timedelta(seconds=1)).isoformat(),
                new_count,
            )

        print(f"[sms_poll_inbound] done — new={new_count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
