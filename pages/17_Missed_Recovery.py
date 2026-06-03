"""Missed-call recovery baseline.

Quantifies how much revenue we're recovering vs. losing from inbound
calls we can't answer. Pre-SMS-launch this is the BASELINE — every
metric here should improve once auto-reply + SMS recovery kicks in.

Sections:
  • Daily missed-call volume (chart)
  • Hour-of-day heatmap (where the misses cluster)
  • Conversion funnel: missed → trackable → converted → revenue
  • Top revenue losers (unknown after-hours callers — invisible to us)
  • The 8 hot conversions today (sanity check)
"""
from __future__ import annotations

import os, sys
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from lib.database import db
from lib.auth import require_password
from lib.style import apply_mobile_styles, page_header, chart_height

st.set_page_config(page_title="Missed Recovery • Pure Comfort", layout="wide", page_icon="📞")
apply_mobile_styles()
require_password()

page_header(
    "Missed-call recovery",
    "Baseline before SMS auto-reply. Every chart on this page should improve "
    "once outbound SMS goes live.",
)


# ────────── Window control ──────────
col_a, col_b = st.columns([1, 5])
with col_a:
    days = st.selectbox("Window", [30, 60, 90, 180], index=1)
since = datetime.now() - timedelta(days=days)


# ────────── Daily volume ──────────
@st.cache_data(ttl=600, show_spinner=False)
def daily_volume(days_back: int) -> pd.DataFrame:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (received_on AT TIME ZONE 'America/Chicago')::date AS day,
                  COUNT(*) FILTER (WHERE call_type IN ('Abandoned','Unbooked')) AS missed,
                  COUNT(*) FILTER (WHERE call_type = 'Booked') AS booked,
                  COUNT(*) FILTER (WHERE call_type IN ('Abandoned','Unbooked')
                                    AND customer_id IS NULL) AS missed_unknown
                FROM calls
                WHERE direction = 'Inbound'
                  AND received_on >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY day ORDER BY day
                """, (days_back,)
            )
            return pd.DataFrame([dict(r) for r in cur.fetchall()])

vol = daily_volume(days)
if vol.empty:
    st.info(f"No call data in the last {days} days.")
    st.stop()

st.subheader(f"📞 Daily inbound — last {days} days")
fig = px.bar(
    vol, x="day", y=["booked", "missed"],
    color_discrete_map={"booked": "#10B981", "missed": "#F34039"},
    labels={"value": "calls", "day": "Date", "variable": ""},
)
fig.update_layout(height=chart_height("default"), barmode="stack")
st.plotly_chart(fig, use_container_width=True)

total_missed = int(vol["missed"].sum())
total_booked = int(vol["booked"].sum())
total_unknown = int(vol["missed_unknown"].sum())

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total inbound", f"{total_missed + total_booked:,}")
k2.metric("Booked at call time", f"{total_booked:,}",
          help="ST tagged the call as 'Booked' — no follow-up needed")
k3.metric("Missed (Abandoned + Unbooked)", f"{total_missed:,}")
k4.metric("Unknown caller", f"{total_unknown:,}",
          help=f"No customer record matched. "
               f"{100*total_unknown/max(total_missed,1):.0f}% of misses are unknown.")


# ────────── Hour-of-day heatmap ──────────
@st.cache_data(ttl=600, show_spinner=False)
def hour_heatmap(days_back: int) -> pd.DataFrame:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  CASE EXTRACT(DOW FROM received_on AT TIME ZONE 'America/Chicago')::int
                    WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue'
                    WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri'
                    WHEN 6 THEN 'Sat' END AS dow,
                  EXTRACT(DOW FROM received_on AT TIME ZONE 'America/Chicago')::int AS dow_n,
                  EXTRACT(HOUR FROM received_on AT TIME ZONE 'America/Chicago')::int AS hour,
                  COUNT(*) AS misses
                FROM calls
                WHERE direction = 'Inbound'
                  AND call_type IN ('Abandoned','Unbooked')
                  AND received_on >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY dow, dow_n, hour
                """, (days_back,)
            )
            return pd.DataFrame([dict(r) for r in cur.fetchall()])

hmap = hour_heatmap(days)
if not hmap.empty:
    st.subheader("⏰ When do misses happen?")
    # Pivot to dow × hour grid, ordered Mon-Sun
    order_idx = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6}
    hmap["sort_dow"] = hmap["dow_n"].map(order_idx)
    pivot = hmap.pivot_table(
        index="sort_dow", columns="hour", values="misses", fill_value=0
    )
    pivot.index = pivot.index.map(
        {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    )
    # Ensure all 24 hours appear
    for h in range(24):
        if h not in pivot.columns:
            pivot[h] = 0
    pivot = pivot.reindex(sorted(pivot.columns), axis=1)
    fig = px.imshow(
        pivot,
        labels=dict(x="Hour (Chicago)", y="Day", color="Misses"),
        color_continuous_scale="Reds",
        aspect="auto",
    )
    fig.update_layout(height=chart_height("compact"))
    # Mark business-hours window (8a-4:30p Mon-Fri)
    fig.add_vrect(x0=7.5, x1=16.5, line_dash="dash", line_color="#0066EE",
                   annotation_text="Business hours", annotation_position="top left")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Dark cells = clusters of missed calls. Anything outside the blue box "
               "is after-hours — prime target for the SMS auto-reply.")


# ────────── Conversion funnel ──────────
@st.cache_data(ttl=600, show_spinner=False)
def funnel_stats(days_back: int) -> dict:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH missed AS (
                  SELECT c.id, c.customer_id, c.received_on, c.from_phone,
                    NOT (
                      EXTRACT(DOW FROM c.received_on AT TIME ZONE 'America/Chicago')::int BETWEEN 1 AND 5
                      AND (c.received_on AT TIME ZONE 'America/Chicago')::time
                          BETWEEN TIME '08:00' AND TIME '16:30'
                    ) AS is_after_hours
                  FROM calls c
                  WHERE c.direction = 'Inbound'
                    AND c.call_type IN ('Abandoned','Unbooked')
                    AND c.received_on >= NOW() - (%s * INTERVAL '1 day')
                ),
                with_rev AS (
                  SELECT m.*,
                    (SELECT SUM(i.total) FROM invoices i
                     WHERE i.customer_id = m.customer_id AND i.total > 0
                       AND i.invoice_date >  (m.received_on AT TIME ZONE 'UTC')::date
                       AND i.invoice_date <= (m.received_on AT TIME ZONE 'UTC')::date + 30) AS rev_30d
                  FROM missed m
                )
                SELECT
                  COUNT(*) AS total_missed,
                  COUNT(*) FILTER (WHERE is_after_hours) AS after_hours_missed,
                  COUNT(*) FILTER (WHERE customer_id IS NULL) AS unknown_caller,
                  COUNT(*) FILTER (WHERE is_after_hours AND customer_id IS NULL) AS after_hours_unknown,
                  COUNT(*) FILTER (WHERE rev_30d > 0) AS converted_30d,
                  COALESCE(SUM(rev_30d), 0)::int AS recovered_revenue,
                  COUNT(*) FILTER (WHERE is_after_hours AND rev_30d > 0) AS ah_converted,
                  COALESCE(SUM(rev_30d) FILTER (WHERE is_after_hours), 0)::int AS ah_revenue
                FROM with_rev
                """, (days_back,)
            )
            return dict(cur.fetchone())

stats = funnel_stats(days)

st.subheader("🪜 Conversion funnel (current baseline — no SMS yet)")
fc1, fc2, fc3, fc4 = st.columns(4)
fc1.metric("Missed calls", f"{stats['total_missed']:,}")
fc2.metric("Of which unknown caller",
           f"{stats['unknown_caller']:,}",
           help=f"{100*stats['unknown_caller']/max(stats['total_missed'],1):.0f}% — "
                "these are the ones SMS would convert to trackable")
fc3.metric("Converted to paid invoice (≤30 days)",
           f"{stats['converted_30d']:,}",
           help="Of trackable missed calls, how many ended up paying us within 30 days")
fc4.metric("Recovered revenue",
           f"${stats['recovered_revenue']:,}",
           help="Total $ from those conversions")

st.markdown("**After-hours only** (the SMS auto-reply's primary target):")
ac1, ac2, ac3, ac4 = st.columns(4)
ac1.metric("After-hours missed", f"{stats['after_hours_missed']:,}")
ac2.metric("After-hours unknown",
           f"{stats['after_hours_unknown']:,}",
           help="Untrackable — went into the void. SMS auto-reply would identify these.")
ac3.metric("After-hours converted", f"{stats['ah_converted']:,}")
ac4.metric("After-hours revenue", f"${stats['ah_revenue']:,}")


# ────────── Today's converters (sanity check) ──────────
st.subheader("✅ Today's missed-call → paid conversions (sanity check)")
@st.cache_data(ttl=300, show_spinner=False)
def todays_recoveries() -> pd.DataFrame:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  c.received_on AT TIME ZONE 'America/Chicago' AS local_call_time,
                  c.customer_name, c.from_phone, c.call_type,
                  i.total AS paid, i.invoice_date
                FROM calls c
                JOIN invoices i ON i.customer_id = c.customer_id
                  AND i.total > 0
                  AND i.invoice_date::date >= (c.received_on AT TIME ZONE 'UTC')::date
                WHERE c.direction = 'Inbound'
                  AND c.call_type IN ('Abandoned','Unbooked')
                  AND c.received_on >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY c.received_on DESC
                LIMIT 20
                """
            )
            return pd.DataFrame([dict(r) for r in cur.fetchall()])

today_rec = todays_recoveries()
if today_rec.empty:
    st.caption("No recovered conversions in the last 7 days.")
else:
    today_rec["local_call_time"] = today_rec["local_call_time"].dt.strftime("%Y-%m-%d %H:%M")
    today_rec["paid"] = today_rec["paid"].apply(lambda v: f"${float(v):,.0f}" if v else "")
    today_rec = today_rec.rename(columns={
        "local_call_time": "Call time (CT)", "customer_name": "Customer",
        "from_phone": "From", "call_type": "Type",
        "paid": "Paid", "invoice_date": "Invoice date",
    })
    st.dataframe(today_rec, use_container_width=True, hide_index=True)

st.caption(
    "Once SMS auto-reply launches, this page becomes the before/after view. "
    "Expect: 'unknown caller' bar shrinks dramatically, 'converted' bar grows, "
    "and the heatmap goes lighter in the after-hours regions."
)
