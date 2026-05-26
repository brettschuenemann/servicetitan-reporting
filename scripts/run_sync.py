"""Hourly incremental sync — pulls new invoices, calls, jobs, etc. from ST.

Runs the same `sync_for_email()` helper the email scripts use, but with no
email side effects. Designed to fire hourly during business hours so the
dashboard (Call List, home page, all reports) stays at most ~1 hour stale.

Triggered by:
- GitHub Actions cron in .github/workflows/hourly_sync.yml
- cron-job.org calling the same workflow's workflow_dispatch endpoint
- Manual run from the Actions tab

Required env vars: ST_APP_KEY, ST_TENANT_ID, ST_CLIENT_ID, ST_CLIENT_SECRET,
DATABASE_URL.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from lib.database import db  # noqa: E402
from lib.servicetitan import ServiceTitanClient  # noqa: E402
from lib.sync import sync_for_email  # noqa: E402

REQUIRED = ("ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET", "DATABASE_URL")
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    sys.exit(f"Missing env vars: {', '.join(missing)}")


def main() -> int:
    client = ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"],
        tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"],
        client_secret=os.environ["ST_CLIENT_SECRET"],
    )
    print("Hourly incremental sync — all entities…")
    with db() as conn:
        results = sync_for_email(client, conn, progress=lambda m: print(f"  · {m}"))
    print("Sync complete.")
    # Quick rollup of upserts per entity for log readability
    for entity, stats in results.items():
        if isinstance(stats, dict):
            if "error" in stats:
                print(f"  ⚠️  {entity}: {stats['error']}")
            elif "upserted" in stats:
                print(f"  ✓ {entity}: {stats['upserted']} touched, {stats.get('total', '?')} total in cache")
    return 0


if __name__ == "__main__":
    sys.exit(main())
