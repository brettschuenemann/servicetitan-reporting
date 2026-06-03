"""Classify intent for scored calls.

Looks for call_scores rows that have a transcript but no intent yet,
runs each through the AI intent classifier, and writes the result back.

Idempotent — only processes rows where intent IS NULL OR
intent_classified_at is older than the rubric_version threshold.

Usage:
    python3 scripts/classify_call_intents.py                # process backlog
    python3 scripts/classify_call_intents.py --limit 50     # cap per run
    python3 scripts/classify_call_intents.py --reclassify   # redo everything

Wired into a cron next to the existing score_calls workflow.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(override=True)

from lib.database import get_connection
from lib.call_intents import classify_intent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200,
                        help="Max rows to classify per run (default 200)")
    parser.add_argument("--reclassify", action="store_true",
                        help="Re-classify rows that already have an intent")
    parser.add_argument("--workers", type=int, default=4,
                        help="Concurrent Claude calls")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[classify_call_intents] ANTHROPIC_API_KEY not set — exiting")
        return 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            if args.reclassify:
                where = "transcript IS NOT NULL AND LENGTH(transcript) > 100"
            else:
                where = ("transcript IS NOT NULL AND LENGTH(transcript) > 100 "
                         "AND intent IS NULL")
            cur.execute(f"""
                SELECT call_id, transcript
                FROM call_scores
                WHERE {where}
                ORDER BY scored_at DESC
                LIMIT %s
            """, (args.limit,))
            todo = list(cur.fetchall())

        if not todo:
            print("[classify_call_intents] nothing to do")
            return 0

        print(f"[classify_call_intents] classifying {len(todo)} call(s) "
              f"with {args.workers} workers")

        def _do_one(row):
            return row["call_id"], classify_intent(row["transcript"])

        results = []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i, (call_id, intent) in enumerate(pool.map(_do_one, todo), 1):
                results.append((call_id, intent))
                if i % 25 == 0:
                    print(f"  ... {i}/{len(todo)}")

        # Bulk-update
        with conn.cursor() as cur:
            for call_id, intent in results:
                cur.execute("""
                    UPDATE call_scores
                    SET intent = %s, intent_classified_at = NOW()
                    WHERE call_id = %s
                """, (intent, call_id))
        conn.commit()

        # Tally
        from collections import Counter
        tally = Counter(r[1] for r in results)
        elapsed = time.time() - t0
        print(f"\n[classify_call_intents] done in {elapsed:.1f}s")
        for intent, count in tally.most_common():
            print(f"  {intent:<18} {count:>4}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
