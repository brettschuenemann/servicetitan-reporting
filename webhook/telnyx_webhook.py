"""Telnyx inbound-SMS webhook receiver.

Telnyx delivers inbound messages ONLY via webhooks (no polling endpoint),
so this tiny FastAPI app is the receiving side of the SMS stack. It:

  1. Accepts POST /webhooks/telnyx/{token} from Telnyx
     (`event_type == "message.received"`)
  2. Verifies the URL token (set WEBHOOK_TOKEN; configure the same token
     in the Telnyx messaging-profile webhook URL)
  3. De-dupes by Telnyx message id (Telnyx retries until it gets a 2xx)
  4. Records the message via lib.sms.record_inbound — which handles STOP
     opt-outs, phone→customer matching, the unmatched bucket, and the
     ServiceTitan customer-note push

Deploy: Render free web service (render.yaml at repo root), env vars:
  DATABASE_URL, WEBHOOK_TOKEN — nothing else. ServiceTitan credentials
  stay OFF this host by design: the ST customer-note push happens in the
  GitHub Actions cron (scripts/sms_poll_inbound.py sweeps posted_to_st=false
  rows every 5 minutes), so a webhook-host compromise never exposes ST
  write access. If ST_* vars happen to be present, notes post inline.

Then point Telnyx at it:
  PATCH https://api.telnyx.com/v2/messaging_profiles/{profile_id}
  {"webhook_url": "https://<render-app>.onrender.com/webhooks/telnyx/<token>"}

Note on Render free tier: the instance sleeps when idle and cold-starts in
~30-60s. Telnyx retries failed webhook deliveries with backoff, and we
de-dupe by message id, so messages arriving during a cold start are
delivered on retry rather than lost.
"""
from __future__ import annotations

import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Request

from lib.database import get_connection
from lib.servicetitan import ServiceTitanClient
from lib.sms import record_inbound

app = FastAPI(docs_url=None, redoc_url=None)


def _st_client():
    keys = ("ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET")
    if not all(os.environ.get(k) for k in keys):
        return None
    return ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"],
        tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"],
        client_secret=os.environ["ST_CLIENT_SECRET"],
    )


@app.get("/")
def health():
    return {"ok": True, "service": "pure-comfort-sms-webhook"}


@app.post("/webhooks/telnyx/{token}")
async def telnyx_webhook(token: str, request: Request):
    expected = os.environ.get("WEBHOOK_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="bad token")

    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="not json")

    data = (event.get("data") or {})
    event_type = data.get("event_type") or ""
    payload = data.get("payload") or {}

    # Only inbound messages create rows; ack everything else so Telnyx
    # stops retrying (delivery receipts, etc. — not tracked yet).
    if event_type != "message.received":
        return {"ok": True, "ignored": event_type}

    msg_id = str(payload.get("id") or "")
    from_phone = ((payload.get("from") or {}).get("phone_number")) or ""
    to_list = payload.get("to") or [{}]
    to_phone = (to_list[0] or {}).get("phone_number") or ""
    body = payload.get("text") or ""

    if not (msg_id and from_phone):
        return {"ok": True, "ignored": "missing id/from"}

    with get_connection() as conn:
        # Telnyx retries until 2xx — de-dupe by provider message id.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sms_messages WHERE twilio_sid = %s LIMIT 1",
                (msg_id,),
            )
            if cur.fetchone():
                return {"ok": True, "duplicate": True}

        row = record_inbound(
            conn,
            from_phone=from_phone,
            to_phone=to_phone,
            body=body,
            twilio_sid=msg_id,   # column stores provider message id
            raw={"provider": "telnyx", "id": msg_id,
                 "received_at": payload.get("received_at")},
            st_client=_st_client(),
        )

    return {"ok": True, "id": row.get("id"),
            "matched_customer": bool(row.get("customer_id"))}
