"""Call coaching pipeline — download + transcribe + score recorded calls.

Three external services:
  - ServiceTitan: download MP3 via /telecom/v2/calls/{id}/recording
  - OpenAI Whisper: transcribe (~$0.006/min)
  - Anthropic Claude Sonnet 4.5: score against a rubric (~$0.005/call)

Total per-call cost: ~$0.013. At ~500 recorded calls/month this is
~$6-7/month all-in.

Each call gets one row in `call_scores`. Reruns are idempotent — calls
with an existing row (success or error) are skipped. To force a re-score,
delete the row by hand.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Optional

import requests
from anthropic import Anthropic
from psycopg2.extras import execute_values

from .servicetitan import ServiceTitanClient


# ---------- constants ----------

RUBRIC_VERSION = "v1"

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
ST_RECORDING_URL = (
    "https://api.servicetitan.io/telecom/v2/tenant/{tenant}/calls/{call_id}/recording"
)
SCORING_MODEL = "claude-sonnet-4-5"

# Skip very short calls — usually ring-outs, accidental dials, or
# 30-second "I'll call back" hangups. Below 30s there isn't enough
# substance to coach on.
MIN_DURATION_SECONDS = 30

# Hard cap per cron run as a cost safety. ~50 × $0.013 = $0.65 max per fire.
DEFAULT_BATCH_LIMIT = 50

# How far back to look for unscored calls. Wide enough that a cron miss
# of 1-2 days still gets caught next run; narrow enough that we don't
# keep retrying ancient errors.
LOOKBACK_DAYS = 14


# ---------- rubrics ----------

_INBOUND_RUBRIC = """You are a sales coach for Pure Comfort, an HVAC + plumbing
service company in Chicagoland. Review the following transcript of an INBOUND
call between a Pure Comfort CSR and a customer who called us. The transcript
has NO speaker labels — infer who's speaking from context (questions, content,
tone).

Score each dimension 1-10 with a one-line evidence quote from the transcript:

1. Discovery — did the CSR understand the customer's situation before pricing?
2. Empathy — did the CSR acknowledge the customer's pain (heat in their home, broken pipe, etc.)?
3. Urgency — did the CSR convey "we can help today/soon"?
4. Pricing framing — was the diagnostic / service fee positioned with value, or hit cold?
5. Close — did the CSR ask for the appointment explicitly?
6. Save attempt — when the caller hesitated, did the CSR try to save the booking?

Then provide:
- OVERALL SCORE (1-10 integer): your holistic read of the call quality
- VERDICT: "bookable" (call quality was strong / customer likely booked or will)
           | "coachable" (decent but specific gaps a 1:1 could fix)
           | "fundamentally broken" (lost a winnable deal through clear mistakes)
- KEY MISS: the single most important thing that, done differently, would have changed the outcome
- NEXT TIME: a specific, repeatable behavior the CSR should try on similar calls
- WINS: 1-3 things the CSR did well (so coaching reinforces, not just critiques)
- COACHING SUMMARY: 2-3 sentence note Brett can share verbatim with the CSR

Return ONLY valid JSON with this exact shape:
{
  "overall_score": <int 1-10>,
  "verdict": "bookable" | "coachable" | "fundamentally broken",
  "dimensions": {
    "discovery":       {"score": <int>, "evidence": "..."},
    "empathy":         {"score": <int>, "evidence": "..."},
    "urgency":         {"score": <int>, "evidence": "..."},
    "pricing_framing": {"score": <int>, "evidence": "..."},
    "close":           {"score": <int>, "evidence": "..."},
    "save_attempt":    {"score": <int>, "evidence": "..."}
  },
  "key_miss": "...",
  "next_time": "...",
  "wins": ["...", "..."],
  "coaching_summary": "..."
}
"""

_OUTBOUND_RUBRIC = """You are a sales coach for Pure Comfort, an HVAC + plumbing
service company in Chicagoland. Review the following transcript of an OUTBOUND
call where a Pure Comfort CSR called a customer (warm-lead follow-up, missed-call
return, estimate follow-up, etc.). The transcript has NO speaker labels — infer
from context.

Score each dimension 1-10 with a one-line evidence quote:

1. Opener — did the CSR identify themselves and the reason for calling quickly + warmly?
2. Discovery — did the CSR listen and ask about the customer's situation before pitching?
3. Value framing — did they connect the call to a customer benefit, not just push a sale?
4. Objection handling — when the customer pushed back, did the CSR bridge gracefully?
5. Close / next step — did the CSR ask for the appointment, decision, or specific follow-up?
6. Tone — professional, warm, paced well, not pushy?

Then provide:
- OVERALL SCORE (1-10 integer)
- VERDICT: "strong" (converted or set up a strong next step)
           | "coachable" (decent but specific gaps)
           | "weak" (the customer disengaged because of how the call went)
- KEY MISS: single most important thing that would have changed the outcome
- NEXT TIME: specific, repeatable behavior for similar calls
- WINS: 1-3 things the CSR did well
- COACHING SUMMARY: 2-3 sentence note Brett can share with the CSR

Return ONLY valid JSON with this exact shape:
{
  "overall_score": <int 1-10>,
  "verdict": "strong" | "coachable" | "weak",
  "dimensions": {
    "opener":             {"score": <int>, "evidence": "..."},
    "discovery":          {"score": <int>, "evidence": "..."},
    "value_framing":      {"score": <int>, "evidence": "..."},
    "objection_handling": {"score": <int>, "evidence": "..."},
    "close":              {"score": <int>, "evidence": "..."},
    "tone":               {"score": <int>, "evidence": "..."}
  },
  "key_miss": "...",
  "next_time": "...",
  "wins": ["...", "..."],
  "coaching_summary": "..."
}
"""


# ---------- tech filtering ----------
# Internal calls (CSR ↔ tech, tech-to-customer with no CSR involved, etc.)
# shouldn't be scored against the CSR rubric — different dynamics. We pull
# the active technician roster from ST once per cron run and exclude any
# call where:
#   - inbound: from_phone matches a tech's phone   (tech calling in)
#   - outbound: to_phone matches a tech's phone    (CSR dialing a tech)
#   - any direction: agent_name matches a tech     (tech-mediated call)


def _normalize_phone(s) -> str:
    """Strip to last 10 digits, drop country code + formatting."""
    digits = re.sub(r"\D", "", str(s or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _normalize_name(s) -> str:
    """First word, lowercase — matches how tech names usually appear in
    agent_name on calls (e.g. 'Bud Smith' → 'bud', 'Peter' → 'peter')."""
    s = (s or "").strip()
    if not s:
        return ""
    return s.split()[0].lower()


def load_tech_filters(st_client: ServiceTitanClient) -> tuple[list[str], list[str]]:
    """Pull active technicians from ST and build (phones, first_names) lists.

    Both lists are normalized for downstream matching:
      - phones: last 10 digits, no formatting
      - names: first word, lowercase
    """
    techs = st_client.get_technicians()
    phones: set[str] = set()
    names: set[str] = set()
    for t in techs:
        if not t.get("active"):
            continue
        for fld in ("phoneNumber", "mobilePhone", "outboundCallerId"):
            n = _normalize_phone(t.get(fld))
            if len(n) == 10:
                phones.add(n)
        nm = _normalize_name(t.get("name") or t.get("firstName"))
        if nm:
            names.add(nm)
    return sorted(phones), sorted(names)


# ---------- pipeline steps ----------

def find_unscored_calls(
    conn,
    limit: int = DEFAULT_BATCH_LIMIT,
    exclude_phones: Optional[list[str]] = None,
    exclude_names: Optional[list[str]] = None,
) -> list[dict]:
    """Calls with a recording but no score yet, within LOOKBACK_DAYS.

    Filters out tech-related calls via SQL (no wasted fetches):
      - Inbound from_phone matching a tech                → skip
      - Outbound to_phone matching a tech                 → skip
      - Any direction where agent_name's first word
        matches an active tech                            → skip

    Skips short calls (< MIN_DURATION_SECONDS) and abandoned ones
    (no recording exists for those — caller hung up before pickup).
    """
    # Postgres ARRAY can't be empty for ANY() comparison reliably across
    # versions — pass a sentinel non-match so the clause is always valid.
    phones = list(exclude_phones) if exclude_phones else ["__none__"]
    names = list(exclude_names) if exclude_names else ["__none__"]

    with conn.cursor() as cur:
        cur.execute(
            r"""
            SELECT c.id, c.direction, c.call_type, c.duration_seconds,
                   c.agent_name, c.customer_name, c.received_on
            FROM calls c
            LEFT JOIN call_scores s ON s.call_id = c.id
            WHERE c.recording_url IS NOT NULL
              AND c.duration_seconds >= %s
              AND c.received_on >= NOW() - (%s || ' day')::interval
              AND s.call_id IS NULL
              -- exclude inbound calls from a tech's phone
              AND NOT (
                c.direction = 'Inbound'
                AND RIGHT(REGEXP_REPLACE(COALESCE(c.from_phone, ''), '\D', '', 'g'), 10)
                    = ANY(%s)
              )
              -- exclude outbound calls to a tech's phone
              AND NOT (
                c.direction = 'Outbound'
                AND RIGHT(REGEXP_REPLACE(COALESCE(c.to_phone, ''), '\D', '', 'g'), 10)
                    = ANY(%s)
              )
              -- exclude calls where the agent is a tech (e.g. Bud's outbound)
              AND NOT (
                LOWER(SPLIT_PART(TRIM(COALESCE(c.agent_name, '')), ' ', 1))
                    = ANY(%s)
              )
            ORDER BY c.received_on DESC
            LIMIT %s
            """,
            (MIN_DURATION_SECONDS, LOOKBACK_DAYS, phones, phones, names, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def download_recording(st_client: ServiceTitanClient, call_id: int) -> bytes:
    """Fetch the MP3 from ServiceTitan via the telecom v2 endpoint."""
    url = ST_RECORDING_URL.format(tenant=st_client.tenant_id, call_id=call_id)
    headers = {
        "Authorization": f"Bearer {st_client._get_token()}",
        "ST-App-Key": st_client.app_key,
    }
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r.content


def transcribe(mp3_bytes: bytes, api_key: Optional[str] = None) -> str:
    """OpenAI Whisper transcription. ~$0.006/min, mono phone audio is its sweet spot."""
    key = api_key or os.environ["OPENAI_API_KEY"]
    r = requests.post(
        WHISPER_URL,
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("call.mp3", mp3_bytes, "audio/mpeg")},
        data={"model": WHISPER_MODEL, "response_format": "text"},
        timeout=180,
    )
    r.raise_for_status()
    return r.text.strip()


def score_transcript(
    transcript: str,
    direction: str,
    agent_name: Optional[str] = None,
    customer_name: Optional[str] = None,
    api_key: Optional[str] = None,
) -> tuple[dict, int, int]:
    """Score one transcript via Claude. Returns (report_dict, tokens_in, tokens_out)."""
    rubric = _INBOUND_RUBRIC if (direction or "").lower() == "inbound" else _OUTBOUND_RUBRIC
    client = Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    user_msg = (
        f"Agent: {agent_name or 'Unknown'}\n"
        f"Customer: {customer_name or 'Unknown'}\n\n"
        f"Transcript:\n{transcript}"
    )

    resp = client.messages.create(
        model=SCORING_MODEL,
        max_tokens=1500,
        system=rubric,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = resp.content[0].text.strip()
    # Strip ```json … ``` fencing if Claude added it
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    report = json.loads(raw)
    return report, resp.usage.input_tokens, resp.usage.output_tokens


def _persist_success(conn, call_id: int, transcript: str, report: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO call_scores (
              call_id, transcript, rubric_version,
              overall_score, verdict, key_miss, next_time,
              wins, coaching_summary, dimensions, raw_response
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s,
              %s::jsonb, %s, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (call_id) DO UPDATE SET
              transcript       = EXCLUDED.transcript,
              rubric_version   = EXCLUDED.rubric_version,
              overall_score    = EXCLUDED.overall_score,
              verdict          = EXCLUDED.verdict,
              key_miss         = EXCLUDED.key_miss,
              next_time        = EXCLUDED.next_time,
              wins             = EXCLUDED.wins,
              coaching_summary = EXCLUDED.coaching_summary,
              dimensions       = EXCLUDED.dimensions,
              raw_response     = EXCLUDED.raw_response,
              error            = NULL,
              scored_at        = NOW()
            """,
            (
                call_id, transcript, RUBRIC_VERSION,
                report.get("overall_score"),
                report.get("verdict"),
                report.get("key_miss"),
                report.get("next_time"),
                json.dumps(report.get("wins") or []),
                report.get("coaching_summary"),
                json.dumps(report.get("dimensions") or {}),
                json.dumps(report),
            ),
        )
    conn.commit()


def _persist_error(conn, call_id: int, message: str) -> None:
    """Record the error so we don't loop on a poison call."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO call_scores (call_id, error)
            VALUES (%s, %s)
            ON CONFLICT (call_id) DO UPDATE SET
              error     = EXCLUDED.error,
              scored_at = NOW()
            """,
            (call_id, message[:1000]),
        )
    conn.commit()


# ---------- batch entrypoint ----------

ProgressCallback = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def score_calls_batch(
    conn,
    st_client: ServiceTitanClient,
    limit: int = DEFAULT_BATCH_LIMIT,
    progress: ProgressCallback = _noop,
) -> dict:
    """Find unscored calls, download + transcribe + score each one.

    Filters out internal CSR ↔ tech calls and tech-mediated customer
    calls before scoring (different coaching dynamics; not what the
    CSR rubric was designed for).

    Returns {scored: N, errors: N, attempted: N, cost_usd: float}.
    """
    tech_phones, tech_names = load_tech_filters(st_client)
    progress(
        f"Tech filter loaded: {len(tech_phones)} phones, "
        f"{len(tech_names)} names — calls matching these will be skipped"
    )

    pending = find_unscored_calls(
        conn, limit=limit,
        exclude_phones=tech_phones, exclude_names=tech_names,
    )
    progress(f"Found {len(pending)} unscored calls (≥{MIN_DURATION_SECONDS}s, ≤{LOOKBACK_DAYS}d old)")

    scored = 0
    errors = 0
    tokens_in = 0
    tokens_out = 0
    whisper_minutes = 0.0

    for c in pending:
        cid = c["id"]
        label = (
            f"call {cid} {c['direction']}/{c['call_type'] or '—'} "
            f"{c['duration_seconds']}s agent={c['agent_name'] or '—'}"
        )
        t0 = time.time()
        try:
            mp3 = download_recording(st_client, cid)
            transcript = transcribe(mp3)
            if not transcript or len(transcript) < 20:
                raise RuntimeError("Transcript empty or too short — likely silent / noisy call")
            report, tin, tout = score_transcript(
                transcript, c["direction"], c.get("agent_name"), c.get("customer_name")
            )
            _persist_success(conn, cid, transcript, report)

            scored += 1
            tokens_in += tin
            tokens_out += tout
            whisper_minutes += (c["duration_seconds"] or 0) / 60.0
            progress(
                f"  ✓ {label}  score={report.get('overall_score')} "
                f"verdict={report.get('verdict')} ({time.time()-t0:.1f}s)"
            )
        except Exception as exc:
            errors += 1
            msg = f"{type(exc).__name__}: {exc}"
            progress(f"  ✗ {label}  {msg}")
            try:
                _persist_error(conn, cid, msg)
            except Exception:
                # Don't let a logging failure crash the batch
                pass

    # Rough cost: Sonnet 4.5 is $3/M in, $15/M out. Whisper is $0.006/min.
    sonnet_cost = (tokens_in * 3 / 1_000_000) + (tokens_out * 15 / 1_000_000)
    whisper_cost = whisper_minutes * 0.006
    total_cost = sonnet_cost + whisper_cost

    return {
        "attempted": len(pending),
        "scored": scored,
        "errors": errors,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "whisper_minutes": round(whisper_minutes, 1),
        "cost_usd": round(total_cost, 4),
    }
