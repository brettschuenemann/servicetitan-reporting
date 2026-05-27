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
from lib.call_coaching import compute_conversion_stats
from lib.coaching_insights import build_insights, load_latest_insights
from lib.database import db
from lib.style import apply_mobile_styles, empty_state

st.set_page_config(page_title="Call Coaching · Pure Comfort", layout="wide")
apply_mobile_styles()
require_password()


# ---------- data loading ----------

@st.cache_data(ttl=120, show_spinner="Loading scored calls…")
def load_scores(lookback_days: int = 14, audience: str = "csr") -> pd.DataFrame:
    """All scored calls in the lookback window, filtered by audience."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              s.call_id, s.scored_at, s.audience, s.rubric_version, s.transcript,
              s.overall_score, s.verdict, s.key_miss, s.next_time,
              s.wins, s.coaching_summary, s.dimensions, s.error,
              c.direction, c.call_type, c.duration_seconds,
              c.received_on, c.agent_name, c.customer_name, c.from_phone
            FROM call_scores s
            JOIN calls c ON c.id = s.call_id
            WHERE s.audience = %s
              AND c.received_on >= NOW() - (%s || ' day')::interval
            ORDER BY c.received_on DESC
            """,
            (audience, lookback_days),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


# ---------- helpers ----------

_VERDICT_COLORS = {
    # CSR rubric verdicts
    "bookable":              ("#D1FAE5", "#065F46"),
    "strong":                ("#D1FAE5", "#065F46"),
    "coachable":             ("#FEF3C7", "#92400E"),
    "weak":                  ("#FEE2E2", "#991B1B"),
    "fundamentally broken":  ("#FEE2E2", "#991B1B"),
    # After-hours rubric verdicts
    "well_handled":          ("#D1FAE5", "#065F46"),
    "acceptable":            ("#FEF3C7", "#92400E"),
    "poorly_handled":        ("#FEE2E2", "#991B1B"),
}
# Verdicts considered "positive" for KPI rollups, by audience
_POSITIVE_VERDICTS = {
    "csr":         {"bookable", "strong"},
    "after_hours": {"well_handled"},
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

def _render_audience(audience: str, label: str, positive_label: str) -> None:
    """Render the full coaching view (insights + KPIs + filters + cards +
    rollup) for one audience bucket. Called once per tab.

    audience: 'csr' or 'after_hours'
    label: human-readable section name ("Daytime CSR", "After-hours")
    positive_label: KPI label for the "did well" metric (varies by rubric)
    """
    df = load_scores(lookback_days=60, audience=audience)

    if df.empty:
        empty_state(
            f"No scored {label.lower()} calls yet. The nightly cron will pick "
            f"them up; trigger manually via `python scripts/score_calls.py`.",
            icon="🌙",
        )
        return

    # ---------- Key insights (Opus 4.7 synthesis, per audience) ----------
    with db() as _conn:
        latest_insights = load_latest_insights(_conn, audience=audience)

    ins_l, ins_r = st.columns([5, 1])
    with ins_l:
        st.markdown(f"### 🧠 Key insights — {label}")
        if latest_insights:
            st.caption(
                f"Opus 4.7 synthesis of {latest_insights['n_calls']} calls over "
                f"{latest_insights['period_days']} days. Generated "
                f"{latest_insights['generated_at']:%a %b %d at %-I:%M %p UTC}."
            )
        else:
            st.caption(
                "No insights generated yet for this audience. Click ↻ Regenerate "
                "to synthesize patterns."
            )
    with ins_r:
        if st.button(
            "↻ Regenerate",
            key=f"regen_insights_{audience}",
            use_container_width=True,
            help=f"Build a fresh Opus 4.7 synthesis of {label.lower()} calls "
                 f"over the last 30 days (~10-15s, ~$0.05).",
        ):
            try:
                with st.spinner("Synthesizing with Opus 4.7…"):
                    with db() as _conn:
                        latest_insights = build_insights(
                            _conn, lookback_days=30, audience=audience,
                        )
                st.success(
                    f"Generated · {latest_insights['n_calls']} calls analyzed · "
                    f"{latest_insights['tokens_in']} tok in / "
                    f"{latest_insights['tokens_out']} tok out"
                )
            except Exception as exc:
                st.error(f"Couldn't generate insights: {exc}")

    if latest_insights:
        _md = (latest_insights["insights_md"] or "").replace("$", r"\$")
        with st.container(border=True):
            st.markdown(_md)

    st.divider()

    # Errors flagged separately
    errored = df[df["error"].notna()]
    scored = df[df["error"].isna()]

    # ---------- KPI strip ----------
    positive_set = _POSITIVE_VERDICTS[audience]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Scored (60d)", f"{len(scored):,}",
              help="Successfully analyzed calls in the last 60 days")

    avg_score = scored["overall_score"].mean() if not scored.empty else 0
    c2.metric("Avg score", f"{avg_score:.1f}/10",
              help="Mean overall score across all analyzed calls")

    positive_pct = (
        100 * scored["verdict"].isin(positive_set).sum() / len(scored)
        if not scored.empty else 0
    )
    c3.metric(positive_label, f"{positive_pct:.0f}%",
              help=f"Calls with a positive verdict ({', '.join(sorted(positive_set))})")

    needs_coaching = int((scored["overall_score"] < 5).sum()) if not scored.empty else 0
    c4.metric("⚠️ Needs coaching", f"{needs_coaching}",
              help="Calls scoring below 5/10 — best review candidates")

    if not errored.empty:
        with st.expander(f"⚠️ {len(errored)} calls failed to score", expanded=False):
            for _, row in errored.iterrows():
                st.caption(
                    f"call {row['call_id']} ({row['agent_name'] or '—'}, "
                    f"{row['duration_seconds']}s): {row['error']}"
                )

    st.divider()

    # ---------- conversion impact ----------
    # For each inbound call with a matched customer_id, did a paid invoice
    # appear within 30 days? Anonymous callers (no customer_id) are
    # excluded from the denominator — we surface that gap separately.
    st.markdown(f"### 📈 Conversion impact — {label}")
    st.caption(
        "Of customer-matched inbound calls in the last 60 days, what "
        "fraction led to a paid invoice within 30 days? Anonymous callers "
        "(no customer_id matched) are excluded from the denominator."
    )

    with db() as _conn:
        own_conv = compute_conversion_stats(_conn, audience=audience,
                                            lookback_days=60, attribution_days=30)
        other_aud = "csr" if audience == "after_hours" else "after_hours"
        other_conv = compute_conversion_stats(_conn, audience=other_aud,
                                              lookback_days=60, attribution_days=30)

    cv1, cv2, cv3, cv4 = st.columns(4)
    cv1.metric(
        "Conversion rate",
        f"{100 * own_conv['conversion_rate']:.0f}%",
        help=f"{own_conv['converted_calls']} of {own_conv['matched_calls']} "
             f"customer-matched calls led to a paid invoice within 30d",
    )
    cv2.metric(
        "Attributed revenue",
        f"${own_conv['attributed_revenue']:,.0f}",
        help="Sum of paid-invoice totals within 30d of a customer-matched call",
    )
    cv3.metric(
        "Revenue per matched call",
        f"${own_conv['revenue_per_matched_call']:,.0f}",
        help="Average attributed revenue per matched inbound call",
    )
    cv4.metric(
        "Customer-match rate",
        f"{100 * own_conv['match_rate']:.0f}%",
        help=f"{own_conv['matched_calls']} of {own_conv['total_inbound']} inbound calls "
             "matched to a known ST customer. The rest are anonymous (we can't "
             "attribute their downstream revenue without phone-match lookup)",
    )

    # Side-by-side comparison
    if other_conv["matched_calls"] > 0:
        other_label = "Daytime CSR" if other_aud == "csr" else "After-hours"
        delta_conv = own_conv["conversion_rate"] - other_conv["conversion_rate"]
        delta_rev = own_conv["revenue_per_matched_call"] - other_conv["revenue_per_matched_call"]
        st.caption(
            f"**vs {other_label}:** "
            f"conversion {100 * other_conv['conversion_rate']:.0f}% "
            f"(this is {'+' if delta_conv >= 0 else ''}{100 * delta_conv:.0f}pp) · "
            f"revenue/call ${other_conv['revenue_per_matched_call']:,.0f} "
            f"(this is {'+' if delta_rev >= 0 else ''}${delta_rev:,.0f})"
        )

    st.divider()

    # ---------- filters ----------

    fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 3])

    agents = ["All agents"] + sorted(
        a for a in scored["agent_name"].dropna().unique().tolist() if a
    )
    with fc1:
        agent_filter = st.selectbox(
            "Agent", agents, key=f"agent_{audience}", label_visibility="collapsed"
        )

    directions = ["All directions", "Inbound", "Outbound"]
    with fc2:
        dir_filter = st.selectbox(
            "Direction", directions, key=f"dir_{audience}", label_visibility="collapsed"
        )

    verdicts = ["All verdicts"] + sorted(
        v for v in scored["verdict"].dropna().unique().tolist() if v
    )
    with fc3:
        verdict_filter = st.selectbox(
            "Verdict", verdicts, key=f"verdict_{audience}", label_visibility="collapsed"
        )

    sort_options = {
        "Most recent first":                    ("received_on", False),
        "Score: lowest first (needs coaching)": ("overall_score", True),
        "Score: highest first (wins)":          ("overall_score", False),
    }
    with fc4:
        sort_key = st.selectbox(
            "Sort", list(sort_options.keys()),
            key=f"sort_{audience}", label_visibility="collapsed",
        )

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
    st.markdown(f"### 📊 Agent rollup — {label}")

    if scored.empty:
        empty_state("Nothing to roll up yet.")
    else:
        rollup = (
            scored.dropna(subset=["agent_name"])
            .groupby("agent_name")
            .agg(
                calls=("call_id", "count"),
                avg_score=("overall_score", "mean"),
                positive_pct=("verdict", lambda v: 100 * v.isin(positive_set).sum() / len(v)),
            )
            .reset_index()
            .sort_values("avg_score", ascending=False)
        )
        rollup["avg_score"] = rollup["avg_score"].round(1)
        rollup["positive_pct"] = rollup["positive_pct"].round(0).astype(int).astype(str) + "%"
        rollup = rollup.rename(columns={
            "agent_name": "Agent",
            "calls": "Calls",
            "avg_score": "Avg score",
            "positive_pct": positive_label,
        })
        st.dataframe(rollup, hide_index=True, use_container_width=True)


# ---------- tabs: route between audiences ----------

tab_csr, tab_after = st.tabs(["👤 Daytime CSR", "🌙 After-hours"])
with tab_csr:
    _render_audience("csr", "Daytime CSR", "Bookable / strong")
with tab_after:
    _render_audience("after_hours", "After-hours", "Well handled")
