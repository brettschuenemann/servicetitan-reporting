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


# ---------- audience classifier (csr vs after_hours) ----------

# Pure Comfort's daytime CSR coverage: M-F 08:30-16:30 Chicago time.
# Anything outside that window is the after-hours service / AI bot handler.
_BUSINESS_START_HHMM = (8, 30)   # 8:30 AM
_BUSINESS_END_HHMM   = (16, 30)  # 4:30 PM


def classify_audience(received_on) -> str:
    """Return 'csr' or 'after_hours' for a given call timestamp.

    Treats Saturday + Sunday as after-hours all day; weekday timestamps
    outside 08:30-16:30 Chicago time are after-hours.
    """
    if received_on is None:
        return "csr"
    try:
        from zoneinfo import ZoneInfo
        local = received_on.astimezone(ZoneInfo("America/Chicago"))
    except Exception:
        local = received_on
    # weekday(): Mon=0 ... Sun=6
    if local.weekday() >= 5:  # Sat / Sun
        return "after_hours"
    minutes = local.hour * 60 + local.minute
    start = _BUSINESS_START_HHMM[0] * 60 + _BUSINESS_START_HHMM[1]
    end   = _BUSINESS_END_HHMM[0]   * 60 + _BUSINESS_END_HHMM[1]
    if start <= minutes < end:
        return "csr"
    return "after_hours"


# ---------- rubrics (simple prose now that tool_use enforces structure) ----------

_INBOUND_RUBRIC = """You are a sales coach for Pure Comfort, an HVAC + plumbing
service company in Chicagoland. Review the following transcript of an INBOUND
call between a Pure Comfort CSR and a customer who called us. The transcript
has NO speaker labels — infer who's speaking from context.

Score each dimension 1-10 with a one-line evidence quote from the transcript:

1. Discovery — did the CSR understand the customer's situation before pricing?
2. Empathy — did the CSR acknowledge the customer's pain (heat in their home, broken pipe, etc.)?
3. Urgency — did the CSR convey "we can help today/soon"?
4. Pricing framing — was the diagnostic / service fee positioned with value, or hit cold?
5. Close — did the CSR ask for the appointment explicitly?
6. Save attempt — when the caller hesitated, did the CSR try to save the booking?

Then judge OVERALL SCORE, VERDICT (bookable / coachable / fundamentally broken),
the single KEY MISS, what to try NEXT TIME, 1-3 WINS, and a 2-3 sentence
COACHING SUMMARY Brett can share with the CSR.

Submit your analysis via the submit_coaching tool.
"""

_OUTBOUND_RUBRIC = """You are a sales coach for Pure Comfort, an HVAC + plumbing
service company in Chicagoland. Review the following transcript of an OUTBOUND
call where a Pure Comfort CSR called a customer. The transcript has NO speaker
labels — infer from context.

Score each dimension 1-10 with a one-line evidence quote:

1. Opener — did the CSR identify themselves and the reason for calling quickly + warmly?
2. Discovery — did the CSR listen and ask about the customer's situation before pitching?
3. Value framing — did they connect the call to a customer benefit, not just push a sale?
4. Objection handling — when the customer pushed back, did the CSR bridge gracefully?
5. Close / next step — did the CSR ask for the appointment, decision, or specific follow-up?
6. Tone — professional, warm, paced well, not pushy?

Then judge OVERALL SCORE, VERDICT (strong / coachable / weak), the single
KEY MISS, what to try NEXT TIME, 1-3 WINS, and a 2-3 sentence COACHING
SUMMARY Brett can share with the CSR.

Submit your analysis via the submit_coaching tool.
"""


# ---------- tool_use schemas (force valid JSON via Anthropic structured output) ----------

_DIM_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "evidence": {"type": "string"},
    },
    "required": ["score", "evidence"],
}


def _coaching_tool(dim_keys: list[str], verdict_enum: list[str]) -> dict:
    """Build the submit_coaching tool definition for either direction."""
    return {
        "name": "submit_coaching",
        "description": (
            "Submit your coaching analysis of the call. All fields are "
            "required. Scores are 1-10 integers. Evidence should be a short "
            "quote or paraphrase from the transcript."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "overall_score": {"type": "integer", "minimum": 1, "maximum": 10},
                "verdict": {"type": "string", "enum": verdict_enum},
                "dimensions": {
                    "type": "object",
                    "properties": {k: _DIM_SCHEMA for k in dim_keys},
                    "required": dim_keys,
                },
                "key_miss": {"type": "string"},
                "next_time": {"type": "string"},
                "wins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
                "coaching_summary": {"type": "string"},
            },
            "required": [
                "overall_score", "verdict", "dimensions",
                "key_miss", "next_time", "wins", "coaching_summary",
            ],
        },
    }


_INBOUND_TOOL = _coaching_tool(
    dim_keys=["discovery", "empathy", "urgency", "pricing_framing",
              "close", "save_attempt"],
    verdict_enum=["bookable", "coachable", "fundamentally broken"],
)

_OUTBOUND_TOOL = _coaching_tool(
    dim_keys=["opener", "discovery", "value_framing", "objection_handling",
              "close", "tone"],
    verdict_enum=["strong", "coachable", "weak"],
)


# ---------- after-hours rubric (AI bot / after-hours service) ----------

_AFTER_HOURS_RUBRIC = """You are a coach evaluating Pure Comfort's after-hours
call-handling service (a mix of an AI bot and a human after-hours service).
Pure Comfort is an HVAC + plumbing company in Chicagoland; daytime calls
go to a CSR, but everything outside M-F 8:30am-4:30pm CST hits this
after-hours layer.

Goal: not closing sales — the after-hours handler's job is *triage and
intake*. Capture the customer's problem cleanly, route emergencies to a
human / dispatcher, and set clear expectations about when someone real
will call back.

Score each dimension 1-10 with a one-line evidence quote from the transcript:

1. Introduction — did the handler identify itself + the company + that
   it's after-hours? Setting context up front matters.
2. Issue capture — was the customer's actual problem captured clearly
   (system type, symptom, urgency, address)?
3. Emergency routing — did the handler correctly escalate or flag the
   call when it should have? No-heat in winter, water leak, gas smell,
   no-AC in summer heat — those need a human now, not a callback tomorrow.
4. Expectations — did the handler tell the customer when someone will
   call back, or what to do next? "Someone will call you back" with no
   timeframe is worse than nothing.
5. Tone — was the handler warm and human-sounding enough that the
   customer didn't feel like they were talking to a robot or a script?

Then judge OVERALL SCORE, VERDICT (well_handled / acceptable / poorly_handled),
the single KEY MISS, what to FIX (specific instruction for tuning the bot
or coaching the after-hours service), 1-3 WINS, and a 2-3 sentence
COACHING SUMMARY Brett can share with the team or use to tune the bot.

Submit your analysis via the submit_coaching tool.
"""


_AFTER_HOURS_TOOL = _coaching_tool(
    dim_keys=["introduction", "issue_capture", "emergency_routing",
              "expectations", "tone"],
    verdict_enum=["well_handled", "acceptable", "poorly_handled"],
)


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


def compute_conversion_stats(
    conn,
    audience: str = "after_hours",
    lookback_days: int = 60,
    attribution_days: int = 30,
) -> dict:
    """How effective is this audience at converting calls into paid invoices?

    For each inbound call in `audience` over the last `lookback_days`,
    attribute it to a customer (either via `calls.customer_id` directly,
    or by looking up `customer_contacts.phone` matching `calls.from_phone`)
    and check whether that customer had a paid invoice within
    `attribution_days` after the call.

    Phone matching: last-10-digits normalization on both sides — strips
    country code, dashes, parens, etc. so '(847) 555-1234' matches
    '+18475551234' matches '8475551234'. Pre-warmed customer_contacts
    table is the lookup source (980+ active customers cached).

    Calls that still can't be resolved to any customer (new prospects
    who've never appeared in ST) are excluded from the denominator.
    The `match_rate` field surfaces what's reachable.
    """
    with conn.cursor() as cur:
        # Coverage: how many inbound calls have *some* customer attribution
        # — either direct from calls.customer_id, or via phone-match against
        # customer_contacts. The CTE produces effective_customer_id per call.
        cur.execute(
            r"""
            WITH resolved AS (
              SELECT
                c.id,
                COALESCE(
                  c.customer_id,
                  (
                    SELECT cc.customer_id FROM customer_contacts cc
                    WHERE RIGHT(REGEXP_REPLACE(COALESCE(cc.phone,''), '\D', '', 'g'), 10) =
                          RIGHT(REGEXP_REPLACE(COALESCE(c.from_phone,''), '\D', '', 'g'), 10)
                      AND LENGTH(RIGHT(REGEXP_REPLACE(COALESCE(c.from_phone,''), '\D', '', 'g'), 10)) = 10
                    LIMIT 1
                  )
                ) AS effective_customer_id,
                CASE WHEN c.customer_id IS NOT NULL THEN 'direct'
                     WHEN c.from_phone IS NOT NULL THEN 'phone_match_attempt'
                     ELSE 'no_signal' END AS resolution_method
              FROM calls c JOIN call_scores s ON s.call_id = c.id
              WHERE s.audience = %s
                AND c.direction = 'Inbound'
                AND c.received_on >= NOW() - (%s || ' day')::interval
                AND s.error IS NULL
            )
            SELECT
              COUNT(*) AS total_inbound,
              COUNT(effective_customer_id) AS matched,
              COUNT(*) FILTER (WHERE resolution_method = 'direct'
                               AND effective_customer_id IS NOT NULL) AS matched_direct,
              COUNT(*) FILTER (WHERE resolution_method = 'phone_match_attempt'
                               AND effective_customer_id IS NOT NULL) AS matched_via_phone
            FROM resolved
            """,
            (audience, lookback_days),
        )
        coverage = dict(cur.fetchone())

        # Conversion: did a paid invoice land in the attribution window?
        # Also classify each matched customer as net_new (first invoice ever
        # was at-or-after the call) vs existing (first invoice was before)
        # — surfaces brand-new customer acquisitions distinctly.
        cur.execute(
            r"""
            WITH resolved AS (
              SELECT
                c.id,
                c.received_on,
                COALESCE(
                  c.customer_id,
                  (
                    SELECT cc.customer_id FROM customer_contacts cc
                    WHERE RIGHT(REGEXP_REPLACE(COALESCE(cc.phone,''), '\D', '', 'g'), 10) =
                          RIGHT(REGEXP_REPLACE(COALESCE(c.from_phone,''), '\D', '', 'g'), 10)
                      AND LENGTH(RIGHT(REGEXP_REPLACE(COALESCE(c.from_phone,''), '\D', '', 'g'), 10)) = 10
                    LIMIT 1
                  )
                ) AS effective_customer_id
              FROM calls c JOIN call_scores s ON s.call_id = c.id
              WHERE s.audience = %s
                AND c.direction = 'Inbound'
                AND c.received_on >= NOW() - (%s || ' day')::interval
                AND s.error IS NULL
            ),
            first_invoice AS (
              SELECT customer_id, MIN(invoice_date) AS first_inv_date
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            ),
            attribution AS (
              SELECT
                r.id,
                EXISTS (
                  SELECT 1 FROM invoices i
                  WHERE i.customer_id = r.effective_customer_id
                    AND i.invoice_date >= (r.received_on AT TIME ZONE 'UTC')::date
                    AND i.invoice_date <  (r.received_on AT TIME ZONE 'UTC')::date
                                          + (%s || ' day')::interval
                    AND i.total > 0
                ) AS converted,
                COALESCE((
                  SELECT SUM(i.total) FROM invoices i
                  WHERE i.customer_id = r.effective_customer_id
                    AND i.invoice_date >= (r.received_on AT TIME ZONE 'UTC')::date
                    AND i.invoice_date <  (r.received_on AT TIME ZONE 'UTC')::date
                                          + (%s || ' day')::interval
                    AND i.total > 0
                ), 0) AS revenue,
                CASE
                  WHEN fi.first_inv_date IS NULL THEN 'never_paid'
                  WHEN fi.first_inv_date >= (r.received_on AT TIME ZONE 'UTC')::date
                       THEN 'net_new'
                  ELSE 'existing'
                END AS customer_kind
              FROM resolved r
              LEFT JOIN first_invoice fi ON fi.customer_id = r.effective_customer_id
              WHERE r.effective_customer_id IS NOT NULL
            )
            SELECT
              COUNT(*) FILTER (WHERE converted) AS converted_count,
              COALESCE(SUM(revenue), 0) AS total_revenue,
              COUNT(*) FILTER (WHERE converted AND customer_kind = 'net_new')
                AS net_new_converted,
              COUNT(*) FILTER (WHERE customer_kind = 'net_new')
                AS net_new_total,
              COALESCE(SUM(revenue) FILTER (WHERE customer_kind = 'net_new'), 0)
                AS net_new_revenue,
              COUNT(*) FILTER (WHERE converted AND customer_kind = 'existing')
                AS existing_converted,
              COUNT(*) FILTER (WHERE customer_kind = 'existing')
                AS existing_total
            FROM attribution
            """,
            (audience, lookback_days, attribution_days, attribution_days),
        )
        result = dict(cur.fetchone())

    matched = coverage["matched"] or 0
    total = coverage["total_inbound"] or 0
    converted = result["converted_count"] or 0
    revenue = float(result["total_revenue"] or 0)

    net_new_total = result["net_new_total"] or 0
    net_new_converted = result["net_new_converted"] or 0
    existing_total = result["existing_total"] or 0
    existing_converted = result["existing_converted"] or 0

    return {
        "audience": audience,
        "lookback_days": lookback_days,
        "attribution_days": attribution_days,
        "total_inbound": total,
        "matched_calls": matched,
        "matched_direct": coverage["matched_direct"] or 0,
        "matched_via_phone": coverage["matched_via_phone"] or 0,
        "match_rate": (matched / total) if total else 0,
        "converted_calls": converted,
        "conversion_rate": (converted / matched) if matched else 0,
        "attributed_revenue": revenue,
        "revenue_per_matched_call": (revenue / matched) if matched else 0,
        "revenue_per_inbound_call": (revenue / total) if total else 0,
        # Net-new vs existing breakdown
        "net_new_total": net_new_total,
        "net_new_converted": net_new_converted,
        "net_new_revenue": float(result["net_new_revenue"] or 0),
        "net_new_conversion_rate": (net_new_converted / net_new_total) if net_new_total else 0,
        "existing_total": existing_total,
        "existing_converted": existing_converted,
        "existing_conversion_rate": (existing_converted / existing_total) if existing_total else 0,
    }


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
    lookback_days: int = LOOKBACK_DAYS,
    exclude_phones: Optional[list[str]] = None,
    exclude_names: Optional[list[str]] = None,
) -> list[dict]:
    """Calls with a recording but no score yet, within `lookback_days`.

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
              AND NOT (
                c.direction = 'Inbound'
                AND RIGHT(REGEXP_REPLACE(COALESCE(c.from_phone, ''), '\D', '', 'g'), 10)
                    = ANY(%s)
              )
              AND NOT (
                c.direction = 'Outbound'
                AND RIGHT(REGEXP_REPLACE(COALESCE(c.to_phone, ''), '\D', '', 'g'), 10)
                    = ANY(%s)
              )
              AND NOT (
                LOWER(SPLIT_PART(TRIM(COALESCE(c.agent_name, '')), ' ', 1))
                    = ANY(%s)
              )
            ORDER BY c.received_on DESC
            LIMIT %s
            """,
            (MIN_DURATION_SECONDS, lookback_days, phones, phones, names, limit),
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
    """OpenAI Whisper transcription. ~$0.006/min, mono phone audio is its sweet spot.

    Passes a Pure-Comfort-specific prompt + language hint to nudge Whisper
    on marginal recordings (it sometimes returns empty on silent/noisy
    audio without the hint).
    """
    key = api_key or os.environ["OPENAI_API_KEY"]
    r = requests.post(
        WHISPER_URL,
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("call.mp3", mp3_bytes, "audio/mpeg")},
        data={
            "model": WHISPER_MODEL,
            "response_format": "text",
            "language": "en",
            "prompt": (
                "This is a phone call between a Pure Comfort HVAC and "
                "plumbing customer service representative (Feyzan) and a "
                "customer in the Chicago area. Topics include furnaces, "
                "air conditioning, water heaters, drain cleaning, and "
                "service appointments."
            ),
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.text.strip()


def score_transcript(
    transcript: str,
    direction: str,
    audience: str = "csr",
    agent_name: Optional[str] = None,
    customer_name: Optional[str] = None,
    api_key: Optional[str] = None,
) -> tuple[dict, int, int]:
    """Score one transcript via Claude tool_use. Returns (report_dict, tokens_in, tokens_out).

    Uses Anthropic's structured-output pattern: defines a `submit_coaching`
    tool with a strict JSON schema and forces Claude to call it. The
    response comes back as a parsed dict — no manual JSON parsing means
    no JSONDecodeError on malformed output.

    Routing:
      audience='csr' + direction='Inbound'  → inbound CSR rubric
      audience='csr' + direction='Outbound' → outbound CSR rubric
      audience='after_hours'                → after-hours triage rubric
                                              (direction ignored — bot
                                              dynamics are direction-agnostic)
    """
    if audience == "after_hours":
        rubric = _AFTER_HOURS_RUBRIC
        tool = _AFTER_HOURS_TOOL
    elif (direction or "").lower() == "inbound":
        rubric = _INBOUND_RUBRIC
        tool = _INBOUND_TOOL
    else:
        rubric = _OUTBOUND_RUBRIC
        tool = _OUTBOUND_TOOL
    client = Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    user_msg = (
        f"Agent: {agent_name or 'Unknown'}\n"
        f"Customer: {customer_name or 'Unknown'}\n\n"
        f"Transcript:\n{transcript}"
    )

    resp = client.messages.create(
        model=SCORING_MODEL,
        max_tokens=2000,
        system=rubric,
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_coaching"},
        messages=[{"role": "user", "content": user_msg}],
    )

    # Tool_use mode returns a tool_use block whose .input is the
    # already-parsed-and-schema-validated dict.
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input, resp.usage.input_tokens, resp.usage.output_tokens

    # Should never happen with tool_choice forced — but defensive
    raise RuntimeError("Claude returned no tool_use block")


def _persist_success(conn, call_id: int, audience: str,
                     transcript: str, report: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO call_scores (
              call_id, audience, transcript, rubric_version,
              overall_score, verdict, key_miss, next_time,
              wins, coaching_summary, dimensions, raw_response
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s,
              %s::jsonb, %s, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (call_id) DO UPDATE SET
              audience         = EXCLUDED.audience,
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
                call_id, audience, transcript, RUBRIC_VERSION,
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


def _persist_error(conn, call_id: int, audience: str, message: str) -> None:
    """Record the error so we don't loop on a poison call."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO call_scores (call_id, audience, error)
            VALUES (%s, %s, %s)
            ON CONFLICT (call_id) DO UPDATE SET
              audience  = EXCLUDED.audience,
              error     = EXCLUDED.error,
              scored_at = NOW()
            """,
            (call_id, audience, message[:1000]),
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
    lookback_days: int = LOOKBACK_DAYS,
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
        conn, limit=limit, lookback_days=lookback_days,
        exclude_phones=tech_phones, exclude_names=tech_names,
    )
    progress(f"Found {len(pending)} unscored calls (≥{MIN_DURATION_SECONDS}s, ≤{lookback_days}d old)")

    scored = 0
    errors = 0
    tokens_in = 0
    tokens_out = 0
    whisper_minutes = 0.0

    for c in pending:
        cid = c["id"]
        audience = classify_audience(c.get("received_on"))
        label = (
            f"call {cid} {audience}/{c['direction']}/{c['call_type'] or '—'} "
            f"{c['duration_seconds']}s agent={c['agent_name'] or '—'}"
        )
        t0 = time.time()
        try:
            mp3 = download_recording(st_client, cid)
            transcript = transcribe(mp3)
            if not transcript or len(transcript) < 20:
                raise RuntimeError("Transcript empty or too short — likely silent / noisy call")
            report, tin, tout = score_transcript(
                transcript, c["direction"], audience=audience,
                agent_name=c.get("agent_name"),
                customer_name=c.get("customer_name"),
            )
            _persist_success(conn, cid, audience, transcript, report)

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
                _persist_error(conn, cid, audience, msg)
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
