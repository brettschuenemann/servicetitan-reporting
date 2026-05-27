"""Call Coaching — per-call scores from the nightly scoring cron.

Reads call_scores (populated by scripts/score_calls.py via GitHub Actions).
This page is read-only — no scoring happens on page load. To re-score a
specific call, delete its row from call_scores; the next cron will pick
it up.

Admin-only. Coaching data is sensitive — keep it behind require_password
so CSRs don't see grades for themselves or each other without context.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.auth import require_password
from lib.coaching_insights import build_insights, load_latest_insights
from lib.database import db
from lib.style import apply_mobile_styles, empty_state

st.set_page_config(page_title="Call Coaching · Pure Comfort", layout="wide")
apply_mobile_styles()
require_password()


# ---------- data loading ----------

@st.cache_data(ttl=120, show_spinner="Loading scored calls…")
def load_scores(lookback_days: int = 14) -> pd.DataFrame:
    """All scored calls in the lookback window, joined with call metadata."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              s.call_id, s.scored_at, s.rubric_version, s.transcript,
              s.overall_score, s.verdict, s.key_miss, s.next_time,
              s.wins, s.coaching_summary, s.dimensions, s.error,
              c.direction, c.call_type, c.duration_seconds,
              c.received_on, c.agent_name, c.customer_name, c.from_phone
            FROM call_scores s
            JOIN calls c ON c.id = s.call_id
            WHERE c.received_on >= NOW() - (%s || ' day')::interval
            ORDER BY c.received_on DESC
            """,
            (lookback_days,),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


# ---------- helpers ----------

_VERDICT_COLORS = {
    "bookable":              ("#D1FAE5", "#065F46"),
    "strong":                ("#D1FAE5", "#065F46"),
    "coachable":             ("#FEF3C7", "#92400E"),
    "weak":                  ("#FEE2E2", "#991B1B"),
    "fundamentally broken":  ("#FEE2E2", "#991B1B"),
}


def _verdict_badge(verdict: str | None) -> str:
    if not verdict:
        return ""
    bg, fg = _VERDICT_COLORS.get(verdict, ("#E5E7EB", "#374151"))
    return (
        f"<span style='background:{bg};color:{fg};padding:2px 10px;"
        f"border-radius:10px;font-size:11px;font-weight:700;"
        f"text-transform:uppercase;letter-spacing:0.04em'>{escape(verdict)}</span>"
    )


def _score_color(score: int | None) -> str:
    """7+ green, 4-6 amber, <4 red, none = grey."""
    if score is None:
        return "#9CA3AF"
    if score >= 7:
        return "#10B981"
    if score >= 4:
        return "#F59E0B"
    return "#EF4444"


def _to_central(ts):
    if ts is None or pd.isna(ts):
        return None
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return ts.astimezone(ZoneInfo("America/Chicago"))
    except Exception:
        return ts


def _safe(v, default: str = "—") -> str:
    """Pandas-safe stringification for escape().

    Critical for any value coming out of a DataFrame: `row[col] or default`
    fails when the value is NaN (NaN is truthy → returns NaN → not a str
    → escape() crashes calling .replace()). Use this helper instead.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    s = str(v).strip()
    return s if s else default


# ---------- header ----------

st.title("🎧 Call Coaching")
st.caption(
    "AI-scored call recordings from the nightly cron. Read-only — re-score a "
    "specific call by deleting its row from `call_scores`."
)

df = load_scores(lookback_days=60)

if df.empty:
    empty_state(
        "No scored calls yet. The nightly cron runs at 3 AM Chicago time. "
        "Trigger it manually via `python scripts/score_calls.py` or wait for tomorrow's run.",
        icon="🌙",
    )
    st.stop()


# ---------- Key insights (Opus 4.7 synthesis) ----------

with db() as _conn:
    latest_insights = load_latest_insights(_conn)

ins_header_l, ins_header_r = st.columns([5, 1])
with ins_header_l:
    st.markdown("### 🧠 Key insights")
    if latest_insights:
        st.caption(
            f"Opus 4.7 synthesis of {latest_insights['n_calls']} calls over "
            f"{latest_insights['period_days']} days. Generated "
            f"{latest_insights['generated_at']:%a %b %d at %-I:%M %p UTC}."
        )
    else:
        st.caption(
            "No insights generated yet. Click ↻ Regenerate to synthesize "
            "patterns across your scored calls."
        )
with ins_header_r:
    if st.button("↻ Regenerate", use_container_width=True,
                 help="Build a fresh Opus 4.7 synthesis over the last 30 days "
                      "of scored calls (~10-15s, ~$0.05)."):
        try:
            with st.spinner("Synthesizing with Opus 4.7…"):
                with db() as _conn:
                    latest_insights = build_insights(_conn, lookback_days=30)
            st.success(
                f"Generated · {latest_insights['n_calls']} calls analyzed · "
                f"{latest_insights['tokens_in']} tok in / "
                f"{latest_insights['tokens_out']} tok out"
            )
        except Exception as exc:
            st.error(f"Couldn't generate insights: {exc}")

if latest_insights:
    # Escape dollar signs so Streamlit's markdown doesn't try to render
    # them as LaTeX math (e.g. "$10,000 deal" looks weird otherwise).
    _md = (latest_insights["insights_md"] or "").replace("$", r"\$")
    with st.container(border=True):
        st.markdown(_md)

st.divider()

# Errors flagged separately
errored = df[df["error"].notna()]
scored = df[df["error"].isna()]

# ---------- KPI strip ----------

c1, c2, c3, c4 = st.columns(4)
c1.metric("Scored (14d)", f"{len(scored):,}",
          help="Successfully analyzed calls in the last 14 days")

avg_score = scored["overall_score"].mean() if not scored.empty else 0
c2.metric("Avg score", f"{avg_score:.1f}/10",
          help="Mean overall score across all analyzed calls")

bookable_set = {"bookable", "strong"}
bookable_pct = (
    100 * scored["verdict"].isin(bookable_set).sum() / len(scored)
    if not scored.empty else 0
)
c3.metric("Bookable / strong", f"{bookable_pct:.0f}%",
          help="Calls scored as bookable (inbound) or strong (outbound)")

needs_coaching = int((scored["overall_score"] < 5).sum()) if not scored.empty else 0
c4.metric("⚠️ Needs coaching", f"{needs_coaching}",
          help="Calls scoring below 5/10 — best 1:1 candidates")

if not errored.empty:
    with st.expander(f"⚠️ {len(errored)} calls failed to score", expanded=False):
        for _, row in errored.iterrows():
            st.caption(
                f"call {row['call_id']} ({row['agent_name'] or '—'}, "
                f"{row['duration_seconds']}s): {row['error']}"
            )

st.divider()

# ---------- filters ----------

fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 3])

agents = ["All agents"] + sorted(
    a for a in scored["agent_name"].dropna().unique().tolist() if a
)
with fc1:
    agent_filter = st.selectbox("Agent", agents, label_visibility="collapsed")

directions = ["All directions", "Inbound", "Outbound"]
with fc2:
    dir_filter = st.selectbox("Direction", directions, label_visibility="collapsed")

verdicts = ["All verdicts"] + sorted(
    v for v in scored["verdict"].dropna().unique().tolist() if v
)
with fc3:
    verdict_filter = st.selectbox("Verdict", verdicts, label_visibility="collapsed")

sort_options = {
    "Most recent first":                    ("received_on", False),
    "Score: lowest first (needs coaching)": ("overall_score", True),
    "Score: highest first (wins)":          ("overall_score", False),
}
with fc4:
    sort_key = st.selectbox("Sort", list(sort_options.keys()), label_visibility="collapsed")

# Apply
view = scored.copy()
if agent_filter != "All agents":
    view = view[view["agent_name"] == agent_filter]
if dir_filter != "All directions":
    view = view[view["direction"] == dir_filter]
if verdict_filter != "All verdicts":
    view = view[view["verdict"] == verdict_filter]

col, ascending = sort_options[sort_key]
view = view.sort_values(col, ascending=ascending, na_position="last")

st.caption(f"Showing **{len(view):,}** of {len(scored):,} scored calls.")

st.divider()

# ---------- per-call cards ----------

if view.empty:
    empty_state("No calls match the current filter.", icon="🔍")
else:
    for _, row in view.iterrows():
        score = row["overall_score"]
        score_color = _score_color(score)
        verdict_html = _verdict_badge(row["verdict"])
        received = _to_central(row["received_on"])
        when = received.strftime("%a %b %d · %-I:%M %p") if received else "—"

        with st.container(border=True):
            # Header row: score (big) + agent + customer + meta
            head_l, head_r = st.columns([1, 6])
            with head_l:
                st.markdown(
                    f"<div style='text-align:center;background:white;"
                    f"border:2px solid {score_color};border-radius:10px;"
                    f"padding:12px 8px;'>"
                    f"<div style='font-size:28px;font-weight:800;color:{score_color};"
                    f"line-height:1'>{score if score is not None else '—'}</div>"
                    f"<div style='font-size:10px;color:#6B7280;font-weight:600;"
                    f"text-transform:uppercase;letter-spacing:0.06em;margin-top:2px'>"
                    f"/ 10</div></div>",
                    unsafe_allow_html=True,
                )
            with head_r:
                st.markdown(
                    f"<div style='font-size:15px;font-weight:600;color:#111;margin-bottom:4px'>"
                    f"{escape(_safe(row['agent_name']))} → "
                    f"{escape(_safe(row['customer_name'], 'Unknown'))}"
                    f"{(' ' + verdict_html) if verdict_html else ''}</div>"
                    f"<div style='font-size:12px;color:#6B7280'>"
                    f"{escape(_safe(row['direction']))} · "
                    f"{escape(_safe(row['call_type']))} · "
                    f"{int(row['duration_seconds']) if pd.notna(row['duration_seconds']) else '—'}s · "
                    f"{escape(when or '—')}</div>",
                    unsafe_allow_html=True,
                )

            # Key insight: KEY MISS + NEXT TIME
            key_miss = _safe(row["key_miss"], "")
            if key_miss:
                st.markdown(
                    f"<div style='margin-top:10px;padding:10px 12px;"
                    f"background:#FEF3C7;border-left:3px solid #F59E0B;"
                    f"border-radius:4px;font-size:13px;color:#78350F'>"
                    f"<b>📍 Key miss</b> — {escape(key_miss)}</div>",
                    unsafe_allow_html=True,
                )
            next_time = _safe(row["next_time"], "")
            if next_time:
                st.markdown(
                    f"<div style='margin-top:6px;padding:10px 12px;"
                    f"background:#DBEAFE;border-left:3px solid #0066EE;"
                    f"border-radius:4px;font-size:13px;color:#1E3A8A'>"
                    f"<b>💡 Next time</b> — {escape(next_time)}</div>",
                    unsafe_allow_html=True,
                )

            # Detail expander
            with st.expander("Show dimensions, wins, transcript"):
                dims = row["dimensions"] if isinstance(row["dimensions"], dict) else {}
                if dims:
                    st.markdown("**Dimensions**")
                    for name, info in dims.items():
                        s = info.get("score") if isinstance(info, dict) else None
                        ev = info.get("evidence", "") if isinstance(info, dict) else ""
                        bar = "█" * (s or 0) + "░" * (10 - (s or 0))
                        st.markdown(
                            f"<div style='font-family:monospace;font-size:12px;color:#374151'>"
                            f"<b>{name.replace('_',' '):<22}</b> "
                            f"<span style='color:{_score_color(s)}'>{bar}</span> "
                            f"{s or '—'}/10</div>"
                            f"<div style='font-size:11px;color:#6B7280;"
                            f"margin-left:14px;margin-bottom:6px'>"
                            f"<i>{escape(_safe(ev, ''))}</i></div>",
                            unsafe_allow_html=True,
                        )

                wins = row["wins"] if isinstance(row["wins"], list) else []
                if wins:
                    st.markdown("**Wins**")
                    for w in wins:
                        st.markdown(f"- {escape(_safe(w, ''))}")

                coaching_summary = _safe(row["coaching_summary"], "")
                if coaching_summary:
                    st.markdown("**Coaching summary**")
                    st.markdown(
                        f"<div style='padding:8px 12px;background:#F8FAFC;"
                        f"border-radius:6px;font-size:13px;color:#0F172A'>"
                        f"{escape(coaching_summary)}</div>",
                        unsafe_allow_html=True,
                    )

                transcript = _safe(row["transcript"], "")
                if transcript:
                    st.markdown("**Transcript**")
                    st.code(transcript, language="text")

# ---------- agent rollup ----------

st.divider()
st.markdown("### 📊 Agent rollup (last 14 days)")

if scored.empty:
    empty_state("Nothing to roll up yet.")
else:
    rollup = (
        scored.dropna(subset=["agent_name"])
        .groupby("agent_name")
        .agg(
            calls=("call_id", "count"),
            avg_score=("overall_score", "mean"),
            bookable_pct=("verdict", lambda v: 100 * v.isin(bookable_set).sum() / len(v)),
        )
        .reset_index()
        .sort_values("avg_score", ascending=False)
    )
    rollup["avg_score"] = rollup["avg_score"].round(1)
    rollup["bookable_pct"] = rollup["bookable_pct"].round(0).astype(int).astype(str) + "%"
    rollup = rollup.rename(columns={
        "agent_name": "Agent",
        "calls": "Calls",
        "avg_score": "Avg score",
        "bookable_pct": "Bookable / strong",
    })
    st.dataframe(rollup, hide_index=True, use_container_width=True)
