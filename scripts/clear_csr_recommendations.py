"""Delete recent rows from csr_recommendations to expire the suppression cooldown.

Used to clean up after manual testing — once the recent rows are gone, the
next morning CSR email starts from a fresh slate (no suppression).

Usage:
  # Locally with .env:
  python scripts/clear_csr_recommendations.py --hours 24
  python scripts/clear_csr_recommendations.py --hours 24 --dry-run
  python scripts/clear_csr_recommendations.py --all          # nukes everything

  # Via GitHub Actions: trigger the "Clear CSR recommendation cooldown"
  workflow from the Actions tab and pass the `hours` input.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from lib.database import db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=24,
                    help="Delete rows whose sent_at is within the last N hours (default 24)")
    ap.add_argument("--all", action="store_true",
                    help="Delete ALL rows (ignores --hours). Use with care.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be deleted without actually deleting")
    args = ap.parse_args()

    with db() as conn, conn.cursor() as cur:
        if args.all:
            where = "TRUE"
            params: tuple = ()
            window = "all rows"
        else:
            where = "sent_at >= NOW() - %s * INTERVAL '1 hour'"
            params = (args.hours,)
            window = f"last {args.hours} hour{'s' if args.hours != 1 else ''}"

        # Preview
        cur.execute(
            f"""
            SELECT kind, COUNT(*) AS n,
                   MIN(sent_at) AS first, MAX(sent_at) AS last
            FROM csr_recommendations
            WHERE {where}
            GROUP BY kind
            ORDER BY kind
            """,
            params,
        )
        preview = [dict(r) for r in cur.fetchall()]
        total = sum(int(r["n"]) for r in preview)

        if total == 0:
            print(f"Nothing to delete in {window}.")
            return 0

        print(f"Would delete {total} row{'s' if total != 1 else ''} from csr_recommendations ({window}):")
        for r in preview:
            print(f"  · {r['kind']:12s}  {r['n']:>4} rows  ({r['first']:%Y-%m-%d %H:%M} – {r['last']:%Y-%m-%d %H:%M} UTC)")

        if args.dry_run:
            print("Dry run — no rows actually deleted.")
            return 0

        cur.execute(f"DELETE FROM csr_recommendations WHERE {where}", params)
        deleted = cur.rowcount
        conn.commit()
        print(f"Deleted {deleted} row{'s' if deleted != 1 else ''}. Next CSR email will start with a fresh suppression slate.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
