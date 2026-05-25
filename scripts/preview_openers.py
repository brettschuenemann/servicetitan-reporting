"""Preview personalized call openers against real customer data.

Pulls a small sample from each section of the morning CSR call list,
calls Claude to generate openers, and prints them to stdout for review.

Use it to see what Fey would actually read tomorrow morning before
deploying the change to the email.

Usage (locally, with .env containing DATABASE_URL + ANTHROPIC_API_KEY):
  python scripts/preview_openers.py
  python scripts/preview_openers.py --limit 5     # 5 per section
  python scripts/preview_openers.py --kind membership  # one section only
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from lib.call_openers import generate_openers  # noqa: E402
from lib.database import db  # noqa: E402
from scripts.send_csr_daily_email import (  # noqa: E402
    load_membership_opps, load_missed_calls, load_sleeping_customers,
)


def build_customer_dicts(memberships: list[dict], sleeping: list[dict],
                        missed: list[dict]) -> list[dict]:
    """Shape DB rows into the format lib.call_openers expects."""
    today = date.today()
    out: list[dict] = []

    for r in memberships:
        install_date = r.get("install_date")
        days = (today - install_date).days if install_date else None
        first_visit = r.get("first_visit")
        out.append({
            "customer_id":      r.get("customer_id"),
            "customer_name":    r.get("customer_name"),
            "kind":             "membership",
            "equipment":        r.get("equipment"),
            "install_summary":  r.get("install_summary"),
            "install_days_ago": days,
            "install_value":    float(r.get("install_value") or 0),
            "lifetime_revenue": float(r.get("lifetime_revenue") or 0),
            "lifetime_invoices": int(r.get("lifetime_invoices") or 0),
            "first_visit_year": first_visit.year if first_visit else None,
        })

    for r in sleeping:
        last_visit = r.get("last_visit")
        days = (today - last_visit).days if last_visit else None
        out.append({
            "customer_id":         r.get("customer_id"),
            "customer_name":       r.get("customer_name"),
            "kind":                "sleeping",
            "last_visit_days_ago": days,
            "last_summary":        r.get("last_summary"),
            "last_items":          r.get("last_items"),
            "loyal_revenue":       float(r.get("loyal_revenue") or 0),
            "loyal_invoices":      int(r.get("loyal_invoices") or 0),
        })

    for r in missed:
        received = r.get("received_on")
        last_visit = r.get("last_visit")
        out.append({
            "customer_id":           r.get("customer_id"),
            "customer_name":         r.get("customer_name") or "Unknown",
            "kind":                  "missed",
            "call_type":             r.get("call_type"),
            "call_when":             received.strftime("%a %I:%M %p") if received else "earlier",
            "lifetime_revenue":      float(r.get("lifetime_revenue") or 0),
            "lifetime_invoices":     int(r.get("lifetime_invoices") or 0),
            "last_visit_days_ago":   (today - last_visit).days if last_visit else None,
            "last_invoice_summary":  r.get("last_invoice_summary"),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=3,
                    help="Max customers per section (default 3)")
    ap.add_argument("--kind", choices=["membership", "sleeping", "missed"],
                    help="Only show one section (default: all)")
    args = ap.parse_args()

    print(f"Pulling top {args.limit} from each section…")
    with db() as conn:
        memberships = []
        sleeping = []
        missed = []
        if args.kind in (None, "membership"):
            memberships = load_membership_opps(conn)[:args.limit]
        if args.kind in (None, "sleeping"):
            sleeping = load_sleeping_customers(conn, limit=args.limit)
        if args.kind in (None, "missed"):
            missed = load_missed_calls(conn)[:args.limit]

    customers = build_customer_dicts(memberships, sleeping, missed)
    eligible = [c for c in customers if c.get("customer_id")]
    print(f"Generating openers for {len(eligible)} customers with Claude…")

    openers = generate_openers(customers)
    if not openers:
        print("\n⚠️  No openers returned. Check that ANTHROPIC_API_KEY is set.")
        return 1

    print("\n" + "=" * 80)
    print("PERSONALIZED OPENERS")
    print("=" * 80)

    for c in customers:
        cid = c.get("customer_id")
        kind = c.get("kind", "")
        name = c.get("customer_name", "Customer")
        print(f"\n[{kind:11s}] {name} (id={cid})")
        # One-line context for readability
        if kind == "membership":
            eq = (c.get("equipment") or "(no equipment)")[:65]
            d = c.get("install_days_ago")
            v = c.get("install_value", 0)
            print(f"  · Context: install {d}d ago ${v:,.0f} — {eq}")
        elif kind == "sleeping":
            d = c.get("last_visit_days_ago")
            s = (c.get("last_summary") or "(no notes)")[:65]
            r = c.get("loyal_revenue", 0)
            print(f"  · Context: last visit {d}d ago, ${r:,.0f} loyal spend — {s}")
        elif kind == "missed":
            ct = c.get("call_type")
            cw = c.get("call_when")
            r = c.get("lifetime_revenue", 0)
            print(f"  · Context: {ct} call {cw}, ${r:,.0f} lifetime")
        opener = openers.get(cid, "(no opener — Claude skipped)")
        print(f"  ✨ Opener: {opener}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
