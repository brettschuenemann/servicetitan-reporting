"""Send a batch SMS outreach campaign — sleeping customers or cold reactivation.

Manual trigger, not cron. You decide when to send each batch.

Two pools supported:

  --kind sleeping
      Paid customers whose last visit was 12+ months ago. Default
      orders by lifetime_revenue DESC so we hit our highest-value
      relationships first.

  --kind reactivation
      Customers who got an estimate but never paid, OR called inbound
      but never became a paying customer. Lower-conversion pool but
      still legitimate prior interaction (low-medium TCPA risk).

Personalization: each message inserts the customer's first name and (for
sleeping) the last-service date. Template is plain Python format-string —
no LLM in V1. Add AI templating later if Phase 1 results justify it.

Usage:
  python3 scripts/sms_outreach_batch.py --kind sleeping --batch-size 5 --dry-run
  python3 scripts/sms_outreach_batch.py --kind sleeping --batch-size 50 \
      --campaign-name "Spring tune-up reminder 2026-06"
  python3 scripts/sms_outreach_batch.py --kind reactivation --batch-size 25
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
from lib.sms import (
    send_sms, normalize_phone, last_ten, is_opted_out, dry_run_enabled,
)


# ── templates (one place; edit to taste) ────────────────────────────

SLEEPING_TEMPLATE = (
    "Hi {first_name} — Fey at Pure Comfort. "
    "Noticed it's been a while since {last_visit_phrase}. "
    "Just checking in — system running OK? "
    "If you want to get on the calendar for a quick once-over, we have "
    "openings this week. Reply STOP to opt out."
)

REACTIVATION_TEMPLATE = (
    "Hi {first_name} — this is Fey at Pure Comfort. "
    "I noticed we connected a while back but never wrapped up. "
    "Anything we can help with now? Happy to get someone out this week. "
    "Reply STOP to opt out."
)


# ── helpers ────────────────────────────────────────────────────────

def _first_name(full_name: str | None) -> str:
    if not full_name:
        return "there"
    name = full_name.strip()
    if "," in name:
        # "Last, First & Spouse" → "First"
        first_part = name.split(",", 1)[1].strip()
        return first_part.split()[0] if first_part else "there"
    parts = name.split()
    return parts[0] if parts else "there"


def _last_visit_phrase(last_visit) -> str:
    """e.g. 'we were out in March 2025' (no exact day — feels natural)."""
    if not last_visit:
        return "we were last in touch"
    return f"we were out in {last_visit.strftime('%B %Y')}"


# ── pool selection ──────────────────────────────────────────────────

def select_sleeping(conn, limit: int, min_ltv: float = 0) -> list[dict]:
    """High-value sleeping customers: paid 12+ months ago, ranked by LTV."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH agg AS (
              SELECT customer_id,
                     MIN(customer_name) AS customer_name,
                     SUM(total) AS lifetime_revenue,
                     MAX(invoice_date) AS last_visit
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            ),
            phones AS (
              SELECT customer_id, MIN(phone) AS phone
              FROM customer_contacts
              WHERE customer_id IS NOT NULL AND phone IS NOT NULL
              GROUP BY customer_id
            )
            SELECT a.customer_id, a.customer_name, a.lifetime_revenue,
                   a.last_visit, p.phone
            FROM agg a
            LEFT JOIN phones p ON p.customer_id = a.customer_id
            WHERE a.last_visit < CURRENT_DATE - INTERVAL '12 months'
              AND a.lifetime_revenue >= %s
              AND p.phone IS NOT NULL
              -- Don't text if we already texted them in last 90d
              AND NOT EXISTS (
                SELECT 1 FROM sms_messages s
                WHERE s.customer_id = a.customer_id
                  AND s.direction = 'outbound'
                  AND s.sent_at >= NOW() - INTERVAL '90 days'
              )
              -- Skip opted-out phones
              AND NOT EXISTS (
                SELECT 1 FROM sms_opt_outs o
                WHERE RIGHT(REGEXP_REPLACE(o.phone, '\\D','','g'), 10)
                    = RIGHT(REGEXP_REPLACE(p.phone, '\\D','','g'), 10)
              )
            ORDER BY a.lifetime_revenue DESC
            LIMIT %s
            """,
            (min_ltv, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def select_reactivation(conn, limit: int) -> list[dict]:
    """Got an estimate or called us but never paid."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH paying AS (
              SELECT DISTINCT customer_id FROM invoices WHERE total > 0
            ),
            never_paid_estimates AS (
              SELECT DISTINCT e.customer_id, e.created_on AS interaction_at,
                     'estimate' AS source
              FROM estimates e
              LEFT JOIN paying p ON p.customer_id = e.customer_id
              WHERE p.customer_id IS NULL
                AND e.customer_id IS NOT NULL
                AND e.created_on >= NOW() - INTERVAL '18 months'
            ),
            unmatched_inbound AS (
              SELECT DISTINCT c.customer_id, MAX(c.received_on) AS interaction_at,
                     'inbound_call' AS source
              FROM calls c
              LEFT JOIN paying p ON p.customer_id = c.customer_id
              WHERE c.direction = 'Inbound'
                AND c.customer_id IS NOT NULL
                AND p.customer_id IS NULL
                AND c.received_on >= NOW() - INTERVAL '12 months'
              GROUP BY c.customer_id
            ),
            pool AS (
              SELECT customer_id, MAX(interaction_at) AS interaction_at,
                     MIN(source) AS source
              FROM (
                SELECT * FROM never_paid_estimates
                UNION ALL SELECT * FROM unmatched_inbound
              ) u
              GROUP BY customer_id
            ),
            cust_meta AS (
              SELECT customer_id, MIN(customer_name) AS customer_name
              FROM (
                SELECT customer_id, customer_name FROM invoices
                  WHERE customer_id IS NOT NULL
                UNION ALL
                SELECT customer_id, customer_name FROM calls
                  WHERE customer_id IS NOT NULL
              ) u
              WHERE customer_name IS NOT NULL
              GROUP BY customer_id
            ),
            phones AS (
              SELECT customer_id, MIN(phone) AS phone
              FROM customer_contacts
              WHERE customer_id IS NOT NULL AND phone IS NOT NULL
              GROUP BY customer_id
            )
            SELECT p.customer_id, cm.customer_name, p.interaction_at,
                   p.source, ph.phone, NULL::date AS last_visit,
                   0 AS lifetime_revenue
            FROM pool p
            LEFT JOIN cust_meta cm ON cm.customer_id = p.customer_id
            LEFT JOIN phones ph ON ph.customer_id = p.customer_id
            WHERE ph.phone IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM sms_messages s
                WHERE s.customer_id = p.customer_id
                  AND s.direction = 'outbound'
                  AND s.sent_at >= NOW() - INTERVAL '90 days'
              )
              AND NOT EXISTS (
                SELECT 1 FROM sms_opt_outs o
                WHERE RIGHT(REGEXP_REPLACE(o.phone,'\\D','','g'), 10)
                    = RIGHT(REGEXP_REPLACE(ph.phone,'\\D','','g'), 10)
              )
            ORDER BY p.interaction_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


# ── main ───────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("sleeping", "reactivation"),
                        required=True)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--min-ltv", type=float, default=0,
                        help="Only used for --kind sleeping")
    parser.add_argument("--campaign-name", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["SMS_DRY_RUN"] = "1"

    template = SLEEPING_TEMPLATE if args.kind == "sleeping" else REACTIVATION_TEMPLATE
    name = args.campaign_name or (
        f"{args.kind}-{datetime.now().strftime('%Y%m%d-%H%M')}"
    )

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
    print(f"[sms_outreach_batch] {mode} | kind={args.kind} "
          f"batch={args.batch_size} campaign='{name}'")

    with get_connection() as conn:
        # Create campaign row
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sms_campaigns
                  (name, kind, template, started_at, dry_run)
                VALUES (%s, %s, %s, NOW(), %s)
                RETURNING id
                """,
                (name, args.kind, template, dry_run_enabled()),
            )
            campaign_id = cur.fetchone()["id"]
        conn.commit()

        # Pick the pool
        if args.kind == "sleeping":
            recipients = select_sleeping(conn, args.batch_size, args.min_ltv)
        else:
            recipients = select_reactivation(conn, args.batch_size)
        print(f"  Selected {len(recipients)} recipient(s)")

        sent = 0
        skipped = 0
        for r in recipients:
            phone = normalize_phone(r.get("phone"))
            if not phone:
                if args.verbose:
                    print(f"    skip — unparseable phone for cust {r['customer_id']}")
                skipped += 1
                continue

            first = _first_name(r.get("customer_name"))
            last_visit_phrase = _last_visit_phrase(r.get("last_visit"))
            body = template.format(
                first_name=first,
                last_visit_phrase=last_visit_phrase,
            )

            try:
                row = send_sms(
                    conn,
                    to_phone=phone,
                    body=body,
                    channel=args.kind,  # 'sleeping' or 'reactivation'
                    customer_id=r.get("customer_id"),
                    related_call_id=None,
                    sent_by=f"campaign:{name}",
                    post_to_st=bool(r.get("customer_id")),
                    st_client=st_client,
                )
                status = row.get("status", "?")
                name_disp = (r.get("customer_name") or "?")[:28]
                print(f"  → {phone}  cust={r['customer_id']:>10}  "
                      f"LTV ${float(r.get('lifetime_revenue') or 0):>8,.0f}  "
                      f"{name_disp:<28}  [{status}]")
                if status not in ("opted_out", "failed"):
                    sent += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"    ERROR on {phone}: {exc}")
                skipped += 1

        # Update campaign stats
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sms_campaigns SET
                  recipient_count = %s,
                  sent_count      = %s,
                  completed_at    = NOW()
                WHERE id = %s
                """,
                (len(recipients), sent, campaign_id),
            )
        conn.commit()

        print(f"\n[sms_outreach_batch] done — sent={sent} skipped={skipped} "
              f"campaign_id={campaign_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
