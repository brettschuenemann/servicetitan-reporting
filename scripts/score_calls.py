"""Nightly call coaching pipeline.

Finds calls with a recording but no score yet (last 14 days), downloads
the MP3 from ServiceTitan, transcribes via OpenAI Whisper, scores via
Claude Sonnet 4.5, persists to `call_scores`.

Bounded by SCORE_CALLS_LIMIT env var (default 50). Each call is ~$0.013
all-in, so a 50-call batch caps at ~$0.65.
"""
from __future__ import annotations

import os
import sys

# Allow `from lib.* import ...` when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env_file() -> None:
    """Load .env if present in repo root. Treats empty shell env values the
    same as missing (so a stale `export ANTHROPIC_API_KEY=` in your shell
    doesn't shadow the real value in .env)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(repo_root, ".env")
    if not os.path.exists(env_path):
        return  # CI / production sets env directly
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not os.environ.get(k):  # not set OR empty string
                os.environ[k] = v


_load_env_file()

from lib.call_coaching import score_calls_batch, DEFAULT_BATCH_LIMIT, LOOKBACK_DAYS
from lib.database import db
from lib.servicetitan import ServiceTitanClient


REQUIRED_ENV = (
    "ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABASE_URL",
)


def main() -> int:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"Missing required env: {', '.join(missing)}")
        return 1

    # Both knobs are env-overridable so the same script can run as the
    # 50-call nightly cron OR a one-off backfill (e.g. 500 calls / 60 days).
    limit = int(os.environ.get("SCORE_CALLS_LIMIT", DEFAULT_BATCH_LIMIT))
    lookback = int(os.environ.get("SCORE_CALLS_LOOKBACK_DAYS", LOOKBACK_DAYS))

    client = ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"],
        tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"],
        client_secret=os.environ["ST_CLIENT_SECRET"],
    )

    with db() as conn:
        result = score_calls_batch(
            conn, client, limit=limit, lookback_days=lookback,
            progress=lambda m: print(m, flush=True),
        )

    print("")
    print("=" * 60)
    print(f"Attempted:        {result['attempted']}")
    print(f"Scored:           {result['scored']}")
    print(f"Errors:           {result['errors']}")
    print(f"Whisper minutes:  {result['whisper_minutes']}")
    print(f"Claude tokens:    {result['tokens_in']} in / {result['tokens_out']} out")
    print(f"Total cost:       ${result['cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
