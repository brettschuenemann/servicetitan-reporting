"""Audit ST's call classification — find discarded leads.

ServiceTitan CSRs tag inbound calls Excused/NotLead, which removes them
from every lead metric. This script gives those calls a second opinion:

- Excused calls: already transcribed + intent-classified by the nightly
  coaching pipeline — nothing to do here, the Calls page cross-checks
  them directly.
- NotLead calls: deliberately EXCLUDED from coaching scoring (solicitors,
  wrong numbers — scoring them against the CSR rubric is noise). But that
  means nobody ever reads them. This script transcribes NotLead calls
  ≥60s (short ones really are junk), stores a transcript-only row in
  call_scores with audience='intent_audit' (invisible to the coaching
  page, which filters audience='csr'), and runs intent classification.

Any NotLead/Excused call whose transcript classifies as schedule_new /
emergency / accept_quote / reschedule surfaces in the "Possibly
misclassified" section of the Calls page.

Cost: ~$0.006/min Whisper + ~$0.001 Claude per call. The full NotLead
backlog (~66 calls) is about $1.

Usage:
    python3 scripts/audit_call_classification.py            # backlog
    python3 scripts/audit_call_classification.py --limit 20
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(override=True)

from lib.database import get_connection
from lib.servicetitan import ServiceTitanClient
from lib.call_coaching import download_recording, transcribe
from lib.call_intents import classify_intent

MIN_DURATION = 60          # seconds; shorter NotLead calls are genuinely junk
RUBRIC = "intent_audit_v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    if not (os.environ.get("OPENAI_API_KEY") and os.environ.get("ANTHROPIC_API_KEY")):
        print("[audit_call_classification] needs OPENAI_API_KEY + ANTHROPIC_API_KEY — exiting")
        return 0

    st_client = ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"],
        tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"],
        client_secret=os.environ["ST_CLIENT_SECRET"],
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.duration_seconds, c.customer_name
                FROM calls c
                LEFT JOIN call_scores s ON s.call_id = c.id
                WHERE c.direction = 'Inbound'
                  AND c.call_type = 'NotLead'
                  AND c.recording_url IS NOT NULL
                  AND COALESCE(c.duration_seconds, 0) >= %s
                  AND s.call_id IS NULL
                ORDER BY c.received_on DESC
                LIMIT %s
                """,
                (MIN_DURATION, args.limit),
            )
            todo = list(cur.fetchall())

        print(f"[audit_call_classification] {len(todo)} NotLead call(s) to transcribe")

        done = 0
        lead_like = 0
        for row in todo:
            call_id = row["id"]
            try:
                mp3 = download_recording(st_client, call_id)
                transcript = transcribe(mp3)
            except Exception as exc:
                print(f"  call {call_id}: transcription failed — {exc}")
                continue

            intent = classify_intent(transcript)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO call_scores
                      (call_id, scored_at, rubric_version, transcript,
                       audience, intent, intent_classified_at)
                    VALUES (%s, NOW(), %s, %s, 'intent_audit', %s, NOW())
                    ON CONFLICT (call_id) DO NOTHING
                    """,
                    (call_id, RUBRIC, transcript, intent),
                )
            conn.commit()
            done += 1
            flag = ""
            if intent in ("schedule_new", "emergency", "accept_quote", "reschedule"):
                lead_like += 1
                flag = "  ⚠️ LEAD-LIKE"
            print(f"  call {call_id}  {int(row['duration_seconds'] or 0)}s  "
                  f"{(row['customer_name'] or '?')[:24]:<24} → {intent}{flag}")

        print(f"\n[audit_call_classification] done — {done} transcribed, "
              f"{lead_like} lead-like NotLead call(s) found")

    return 0


if __name__ == "__main__":
    sys.exit(main())
