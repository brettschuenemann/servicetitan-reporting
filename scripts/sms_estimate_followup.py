"""SMS follow-up for aging estimates.

Estimates that have been sitting open for 14 / 30 / 45 days get a
progressively warmer SMS check-in. Drives that $226k+ open-quote
pipeline that otherwise just decays.

Three stages:
  Day 14  — soft check-in, "any questions?"
  Day 30  — gentle urgency, "openings filling up"
  Day 45  — final touch, "are you still considering?" (subtle exit ramp)

After day 45 the estimate is considered cold; we stop texting and let
the quarterly reactivation campaign pick them up.

Safety:
- Skips customers opted out
- Skips customers we already SMS'd in the last 7 days
- Min estimate value $1,000 (sub-$1k quotes aren't worth the friction)
- Dry-run via SMS_DRY_RUN=1

Cron: daily 10am CT via GitHub Actions.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from lib.database import get_connection
from lib.servicetitan import ServiceTitanClient
from lib.sms import send_sms, normalize_phone, dry_run_enabled


# ── stage templates ────────────────────────────────────────────────

STAGE_TEMPLATES = {
    14: (
        "Hi {first_name} — Fey at Pure Comfort. Following up on the ${amount} "
        "quote our team put together. Any questions I can answer, or would it "
        "help to schedule a quick walkthrough? Reply STOP to opt out."
    ),
    30: (
        "Hi {first_name} — Fey at Pure Comfort. The ${amount} estimate is "
        "about a month old now. If you're still thinking it over, I can hold "
        "your slot — just say the word. Reply STOP to opt out."
    ),
    45: (
        "Hi {first_name} — Fey at Pure Comfort. Want to make sure we're not "
        "letting this slip — still considering the ${amount} estimate from us, "
        "or did you go a different direction? No worries either way. "
        "Reply STOP to opt out."
    ),
}

STAGES = sorted(STAGE_TEMPLATES.keys())  # [14, 30, 45]


# ── helpers ────────────────────────────────────────────────────────

def _first_name(full_name: str | None) -> str:
    if not full_name:
        return "there"
    name = full_name.strip()
    if "," in name:
        first_part = name.split(",", 1)[1].strip()
        return first_part.split()[0] if first_part else "there"
    parts = name.split()
    return parts[0] if parts else "there"


def find_candidates(conn) -> list[dict]:
    """Find estimates at exactly 14/30/45 days old (±1 day window so we
    catch them if the cron misses a day) that haven't gotten a recent SMS.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH age_buckets AS (
              SELECT 14 AS stage_days UNION ALL SELECT 30 UNION ALL SELECT 45
            ),
            candidates AS (
              SELECT
                e.id           AS estimate_id,
                e.customer_id,
                e.subtotal,
                e.name         AS estimate_name,
                e.created_on,
                (CURRENT_DATE - e.created_on::date) AS days_old,
                ab.stage_days
              FROM estimates e
              CROSS JOIN age_buckets ab
              WHERE e.status_name = 'Open' AND e.active = TRUE
                AND e.subtotal >= 1000
                AND e.customer_id IS NOT NULL
                -- The "exactly this stage" window (±1 day for cron jitter)
                AND (CURRENT_DATE - e.created_on::date) BETWEEN ab.stage_days - 1 AND ab.stage_days + 1
            ),
            with_phone AS (
              SELECT c.*,
                     (SELECT MIN(phone) FROM customer_contacts cc
                      WHERE cc.customer_id = c.customer_id AND cc.phone IS NOT NULL
                     ) AS phone,
                     (SELECT MIN(customer_name) FROM invoices i
                      WHERE i.customer_id = c.customer_id AND i.customer_name IS NOT NULL
                     ) AS customer_name
              FROM candidates c
            )
            SELECT * FROM with_phone wp
            WHERE wp.phone IS NOT NULL
              -- Don't double-text within 7 days
              AND NOT EXISTS (
                SELECT 1 FROM sms_messages s
                WHERE s.customer_id = wp.customer_id
                  AND s.direction = 'outbound'
                  AND s.sent_at >= NOW() - INTERVAL '7 days'
              )
              -- Skip opt-outs
              AND NOT EXISTS (
                SELECT 1 FROM sms_opt_outs o
                WHERE RIGHT(REGEXP_REPLACE(o.phone, '\\D','','g'), 10)
                    = RIGHT(REGEXP_REPLACE(wp.phone, '\\D','','g'), 10)
              )
              -- Skip if customer already booked something since the estimate
              AND NOT EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.customer_id = wp.customer_id
                  AND j.job_status IN ('Scheduled','Dispatched','InProgress','Working','Completed')
                  AND j.created_on >= wp.created_on
              )
            ORDER BY wp.subtotal DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


# ── main ───────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["SMS_DRY_RUN"] = "1"

    st_client = None
    if all(os.environ.get(k) for k in (
        "ST_APP_KEY","ST_TENANT_ID","ST_CLIENT_ID","ST_CLIENT_SECRET"
    )):
        st_client = ServiceTitanClient(
            app_key=os.environ["ST_APP_KEY"],
            tenant_id=os.environ["ST_TENANT_ID"],
            client_id=os.environ["ST_CLIENT_ID"],
            client_secret=os.environ["ST_CLIENT_SECRET"],
        )

    mode = "DRY-RUN" if dry_run_enabled() else "LIVE"
    print(f"[sms_estimate_followup] {mode} mode")

    with get_connection() as conn:
        candidates = find_candidates(conn)
        print(f"  Found {len(candidates)} candidate estimate(s)")

        sent = 0
        skipped = 0
        for r in candidates:
            phone = normalize_phone(r.get("phone"))
            if not phone:
                if args.verbose:
                    print(f"    skip — bad phone for cust {r['customer_id']}")
                skipped += 1
                continue

            stage = int(r["stage_days"])
            template = STAGE_TEMPLATES[stage]
            body = template.format(
                first_name=_first_name(r.get("customer_name")),
                amount=f"{float(r['subtotal']):,.0f}",
            )

            try:
                row = send_sms(
                    conn,
                    to_phone=phone, body=body,
                    channel=f"estimate_followup_{stage}d",
                    customer_id=r.get("customer_id"),
                    sent_by="system_estimate_followup",
                    post_to_st=True,
                    st_client=st_client,
                )
                status = row.get("status", "?")
                name = (r.get("customer_name") or "?")[:24]
                print(f"  → stage {stage}d  ${float(r['subtotal']):>7,.0f}  "
                      f"{name:<24}  [{status}]")
                if status in ("opted_out", "failed"):
                    skipped += 1
                else:
                    sent += 1
            except Exception as exc:
                print(f"    ERROR on cust {r['customer_id']}: {exc}")
                skipped += 1

        print(f"\n[sms_estimate_followup] done — sent={sent} skipped={skipped}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
