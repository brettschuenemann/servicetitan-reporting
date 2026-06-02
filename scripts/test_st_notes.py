"""Verify the ServiceTitan customer-notes POST endpoint works.

Posts ONE note to ONE test customer and reports success/failure.
This needs to work before we commit to the SMS-→-ST-notes architecture.

Usage:
    python3 scripts/test_st_notes.py --customer-id 151125572
    python3 scripts/test_st_notes.py --customer-id 151125572 --note "custom text"

You can pick any real customer_id from your data — the script tags the
note with "[TEST]" prefix and a timestamp so it's clearly identifiable
if you want to delete it in ST afterward.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from lib.servicetitan import ServiceTitanClient, ServiceTitanError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer-id", type=int, required=True,
                        help="Real ST customer ID to attach the test note to")
    parser.add_argument("--note", default=None,
                        help="Custom note text (defaults to a timestamped marker)")
    args = parser.parse_args()

    client = ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"],
        tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"],
        client_secret=os.environ["ST_CLIENT_SECRET"],
    )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    note_text = args.note or (
        f"[TEST] SMS infrastructure connectivity check at {ts}. "
        f"If you see this, the ST notes API is working. "
        f"Safe to delete."
    )

    print(f"Posting note to customer {args.customer_id}...")
    print(f"  Text: {note_text}")
    print()

    try:
        response = client.create_customer_note(args.customer_id, note_text)
        print("✅ SUCCESS")
        print(f"  Response: {response}")
        return 0
    except ServiceTitanError as exc:
        print(f"❌ FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
