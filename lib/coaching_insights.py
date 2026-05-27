"""Cross-call coaching insights — Opus 4.7 synthesis of `call_scores`.

The per-call Sonnet 4.5 reports are tactical (one call, what went wrong,
what to try next time). This layer is strategic: looking across N days
of scored calls, what patterns are worth Brett's attention?

Persisted to `coaching_insights` so the page doesn't pay per render.
Cost per generation: ~$0.05-0.08 on Opus 4.7 (the brief is small —
aggregated stats + sample of low/high scorers, not full transcripts).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from anthropic import Anthropic


MODEL = "claude-opus-4-7"

_SYSTEM_PROMPT = """You are a sales coach analyzing call performance for Pure Comfort
(HVAC + plumbing service company in Chicagoland). You'll receive aggregated
stats from the AI scoring pipeline plus a sample of low-scoring and
high-scoring calls. Your job is to find patterns worth acting on.

Output a tight markdown briefing with:

## Headline
One sentence: the most important pattern in this period's data. Concrete,
quantified, names names.

## What's Driving Low Scores
3-5 bullets identifying SPECIFIC repeated misses. Reference actual calls
or named agents. Don't say "improve discovery" — say "Feyzan accepts the
first 'I'll think about it' on 8 of 12 sales calls without bridging back
to urgency."

## What's Working
2-3 bullets on bookable / strong calls. What did the agents do that
landed? Be specific — exact phrases, behaviors, or call patterns to
reinforce.

## Coach This Week
Top 3 concrete actions Brett should take in 1:1s this week. Each
action should be (a) tied to a real pattern above, (b) phrased as
"do this specific thing" not "consider doing this."

KEEP IT SHORT: under 400 words total. Skip filler. If a finding is weak
or there isn't enough data, say so rather than padding.
"""


def _gather_brief(conn, lookback_days: int = 30) -> tuple[dict, str]:
    """Pull aggregated stats + sample calls into a compact brief for Opus.

    Returns (metrics_dict, brief_text). brief_text is the prompt body
    we send to Claude; metrics_dict is saved for forensics.
    """
    with conn.cursor() as cur:
        # Overall stats
        cur.execute(
            """
            SELECT
              COUNT(*)                                  AS n,
              ROUND(AVG(s.overall_score)::numeric, 2)   AS avg,
              COUNT(*) FILTER (WHERE s.verdict IN ('bookable','strong'))
                                                        AS bookable,
              COUNT(*) FILTER (WHERE s.overall_score < 4) AS weak,
              COUNT(DISTINCT c.agent_name) FILTER (WHERE c.agent_name IS NOT NULL)
                                                        AS agents
            FROM call_scores s JOIN calls c ON c.id = s.call_id
            WHERE s.error IS NULL
              AND c.received_on >= NOW() - (%s || ' day')::interval
            """,
            (lookback_days,),
        )
        overall = dict(cur.fetchone())

        # Per-agent rollup
        cur.execute(
            """
            SELECT
              c.agent_name,
              COUNT(*) AS calls,
              ROUND(AVG(s.overall_score)::numeric, 1) AS avg,
              COUNT(*) FILTER (WHERE s.verdict IN ('bookable','strong')) AS bookable,
              COUNT(*) FILTER (WHERE s.overall_score < 4) AS weak
            FROM call_scores s JOIN calls c ON c.id = s.call_id
            WHERE s.error IS NULL
              AND c.received_on >= NOW() - (%s || ' day')::interval
              AND c.agent_name IS NOT NULL
            GROUP BY c.agent_name
            ORDER BY calls DESC
            """,
            (lookback_days,),
        )
        agents = [dict(r) for r in cur.fetchall()]

        # By call_type
        cur.execute(
            """
            SELECT
              c.direction, c.call_type,
              COUNT(*) AS n,
              ROUND(AVG(s.overall_score)::numeric, 1) AS avg
            FROM call_scores s JOIN calls c ON c.id = s.call_id
            WHERE s.error IS NULL
              AND c.received_on >= NOW() - (%s || ' day')::interval
            GROUP BY c.direction, c.call_type
            ORDER BY COUNT(*) DESC
            """,
            (lookback_days,),
        )
        by_type = [dict(r) for r in cur.fetchall()]

        # Average per dimension (which areas are weakest)
        cur.execute(
            """
            SELECT
              key   AS dim,
              ROUND(AVG((dims.value->>'score')::numeric), 1) AS avg
            FROM call_scores s
            JOIN calls c ON c.id = s.call_id,
              LATERAL jsonb_each(s.dimensions) AS dims(key, value)
            WHERE s.error IS NULL
              AND c.received_on >= NOW() - (%s || ' day')::interval
              AND s.dimensions IS NOT NULL
              AND (dims.value->>'score') ~ '^-?\d+$'
            GROUP BY key
            ORDER BY avg
            """,
            (lookback_days,),
        )
        dimensions = [dict(r) for r in cur.fetchall()]

        # Low scorers — sample of weak calls with their key_miss
        cur.execute(
            """
            SELECT s.overall_score, s.key_miss, c.agent_name,
                   c.direction, c.call_type, c.duration_seconds
            FROM call_scores s JOIN calls c ON c.id = s.call_id
            WHERE s.error IS NULL
              AND c.received_on >= NOW() - (%s || ' day')::interval
              AND s.overall_score < 4
              AND s.key_miss IS NOT NULL
            ORDER BY s.overall_score, c.received_on DESC
            LIMIT 12
            """,
            (lookback_days,),
        )
        weak_samples = [dict(r) for r in cur.fetchall()]

        # High scorers — what's working
        cur.execute(
            """
            SELECT s.overall_score, s.wins, s.coaching_summary,
                   c.agent_name, c.direction, c.call_type
            FROM call_scores s JOIN calls c ON c.id = s.call_id
            WHERE s.error IS NULL
              AND c.received_on >= NOW() - (%s || ' day')::interval
              AND s.overall_score >= 7
            ORDER BY s.overall_score DESC, c.received_on DESC
            LIMIT 6
            """,
            (lookback_days,),
        )
        strong_samples = [dict(r) for r in cur.fetchall()]

    metrics = {
        "lookback_days": lookback_days,
        "overall": overall,
        "agents": agents,
        "by_type": by_type,
        "dimensions": dimensions,
        "weak_samples_count": len(weak_samples),
        "strong_samples_count": len(strong_samples),
    }

    # Render brief
    lines = [
        f"PERIOD: last {lookback_days} days",
        f"",
        f"OVERALL ({overall['n']} scored calls across {overall['agents']} agents)",
        f"  Avg score: {overall['avg']}/10",
        f"  Bookable / strong: {overall['bookable']} ({100*overall['bookable']//max(overall['n'],1)}%)",
        f"  Weak (<4): {overall['weak']} ({100*overall['weak']//max(overall['n'],1)}%)",
        f"",
        f"PER-AGENT ROLLUP",
    ]
    for a in agents:
        lines.append(
            f"  {a['agent_name']}: {a['calls']} calls, avg {a['avg']}/10, "
            f"{a['bookable']} bookable, {a['weak']} weak"
        )
    lines += ["", "BY CALL TYPE"]
    for r in by_type:
        lines.append(
            f"  {r['direction']}/{r['call_type'] or '—'}: n={r['n']}, avg {r['avg']}/10"
        )
    lines += ["", "WEAKEST DIMENSIONS (lower = worse)"]
    for d in dimensions:
        lines.append(f"  {d['dim']:<22} {d['avg']}/10")

    lines += ["", "SAMPLE WEAK CALLS (with the AI-flagged key miss):"]
    for s in weak_samples:
        agent = s["agent_name"] or "—"
        km = (s["key_miss"] or "")[:240]
        lines.append(
            f"  [{s['overall_score']}/10 · {s['direction']}/{s['call_type'] or '—'} · "
            f"{agent} · {s['duration_seconds']}s] {km}"
        )
    lines += ["", "SAMPLE STRONG CALLS (with what they did right):"]
    for s in strong_samples:
        agent = s["agent_name"] or "—"
        wins = s["wins"] if isinstance(s["wins"], list) else []
        wins_str = " | ".join(str(w)[:120] for w in wins[:3]) or "—"
        summary = (s["coaching_summary"] or "")[:200]
        lines.append(
            f"  [{s['overall_score']}/10 · {s['direction']}/{s['call_type'] or '—'} · {agent}]"
        )
        lines.append(f"    wins: {wins_str}")
        lines.append(f"    summary: {summary}")

    return metrics, "\n".join(lines)


def build_insights(conn, lookback_days: int = 30) -> dict:
    """Generate insights via Opus 4.7 and persist to coaching_insights.

    Returns the new row as a dict with id, insights_md, etc.
    """
    metrics, brief = _gather_brief(conn, lookback_days=lookback_days)
    n_calls = metrics["overall"]["n"] or 0
    if n_calls < 5:
        raise RuntimeError(
            f"Only {n_calls} scored calls in last {lookback_days} days — "
            "not enough data for meaningful insights yet."
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing — cannot call Claude.")

    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": brief}],
    )
    insights_md = resp.content[0].text.strip()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO coaching_insights
              (period_days, n_calls, insights_md, raw_brief, model,
               tokens_in, tokens_out)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, generated_at
            """,
            (lookback_days, n_calls, insights_md, brief, MODEL,
             resp.usage.input_tokens, resp.usage.output_tokens),
        )
        row = cur.fetchone()
    conn.commit()

    return {
        "id": row["id"],
        "generated_at": row["generated_at"],
        "period_days": lookback_days,
        "n_calls": n_calls,
        "insights_md": insights_md,
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
    }


def load_latest_insights(conn) -> dict | None:
    """Return the most recent coaching_insights row, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, generated_at, period_days, n_calls, insights_md, "
            "model, tokens_in, tokens_out "
            "FROM coaching_insights ORDER BY generated_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    return dict(row) if row else None
