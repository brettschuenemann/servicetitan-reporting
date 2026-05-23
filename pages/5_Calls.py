"""Calls — CSR performance, call funnel, hour-of-day, and abandoned-call followups."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles, chart_height

st.set_page_config(page_title="Calls · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Calls — CSR performance and call funnel")
st.caption(
    "All call data from ServiceTitan's telecom log. Inbound calls only unless "
    "noted. CSR booking rate measures **bookable** calls only (Booked + Unbooked "
    "+ Abandoned) — excluded Excused/NotLead so wrong-numbers and vendor calls "
    "don't drag the rate down."
)

with st.sidebar:
    st.header("Filters")
    today = date.today()
    start = st.date_input("From", today - timedelta(days=30))
    end = st.date_input("To", today)

if end < start:
    st.error("End date is before start date.")
    st.stop()


@st.cache_data(ttl=120, show_spinner="Loading calls…")
def load_calls(s: date, e: date) -> pd.DataFrame:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, received_on, created_on, direction, call_type,
                   duration_seconds, from_phone, to_phone,
                   agent_id, agent_name, customer_id, customer_name,
                   campaign_name, job_id, job_number,
                   business_unit_name, recording_url, voicemail_url, reason
            FROM calls
            WHERE created_on::date BETWEEN %s AND %s
            ORDER BY created_on DESC
            """,
            (s, e),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


df = load_calls(start, end)
if df.empty:
    st.info("No calls in this range.")
    st.stop()

inbound = df[df["direction"] == "Inbound"].copy()
outbound = df[df["direction"] == "Outbound"].copy()

# ---- Top KPI row ----
booked = int((inbound["call_type"] == "Booked").sum())
unbooked = int((inbound["call_type"] == "Unbooked").sum())
abandoned = int((inbound["call_type"] == "Abandoned").sum())
excused = int((inbound["call_type"] == "Excused").sum())
not_lead = int((inbound["call_type"] == "NotLead").sum())
bookable = booked + unbooked + abandoned
book_rate = (booked / bookable * 100) if bookable else 0
jobs_linked = int(inbound["job_id"].notna().sum())

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Inbound calls", f"{len(inbound):,}")
k2.metric(
    "Booking rate",
    f"{book_rate:.0f}%",
    help=f"{booked:,} booked / {bookable:,} bookable (excludes Excused/NotLead)",
)
k3.metric("Abandoned", f"{abandoned:,}", help="Caller hung up before booking — missed revenue")
k4.metric("Linked to a job", f"{jobs_linked:,}")
k5.metric("Outbound", f"{len(outbound):,}")

st.divider()

# ---- Funnel ----
st.subheader("Call funnel (inbound)")
funnel_df = pd.DataFrame(
    [
        ("Booked", booked, "#2ca02c"),
        ("Unbooked", unbooked, "#d62728"),
        ("Abandoned", abandoned, "#d62728"),
        ("Excused", excused, "#9e9e9e"),
        ("NotLead", not_lead, "#9e9e9e"),
    ],
    columns=["Type", "Count", "color"],
)
fig = px.bar(
    funnel_df,
    x="Type",
    y="Count",
    color="Type",
    text="Count",
    color_discrete_map={r.Type: r.color for r in funnel_df.itertuples()},
)
fig.update_traces(textposition="outside")
fig.update_layout(
    margin=dict(l=0, r=0, t=10, b=0),
    height=chart_height("default"),
    showlegend=False,
)
st.plotly_chart(fig, use_container_width=True)

# ---- CSR leaderboard ----
st.subheader("CSR performance (inbound)")
st.caption("Booking rate = Booked / (Booked + Unbooked + Abandoned). Excused and NotLead excluded.")
csr_inbound = inbound[inbound["agent_name"].notna()].copy()
if not csr_inbound.empty:
    csr = (
        csr_inbound.groupby("agent_name", as_index=False)
        .agg(
            total=("id", "count"),
            booked=("call_type", lambda s: int((s == "Booked").sum())),
            unbooked=("call_type", lambda s: int((s == "Unbooked").sum())),
            abandoned=("call_type", lambda s: int((s == "Abandoned").sum())),
            excused=("call_type", lambda s: int((s == "Excused").sum())),
            not_lead=("call_type", lambda s: int((s == "NotLead").sum())),
        )
        .assign(
            bookable=lambda d: d["booked"] + d["unbooked"] + d["abandoned"],
            book_rate=lambda d: (d["booked"] / d["bookable"].replace(0, pd.NA) * 100).round(1),
        )
        .sort_values("total", ascending=False)
    )
    csr["book_rate"] = csr["book_rate"].fillna(0)
    display = csr.rename(
        columns={
            "agent_name": "Agent",
            "total": "Total calls",
            "booked": "Booked",
            "unbooked": "Unbooked",
            "abandoned": "Abandoned",
            "excused": "Excused",
            "not_lead": "NotLead",
            "bookable": "Bookable",
            "book_rate": "Book rate %",
        }
    )[["Agent", "Total calls", "Bookable", "Booked", "Unbooked", "Abandoned",
       "Excused", "NotLead", "Book rate %"]]
    st.dataframe(display, use_container_width=True, hide_index=True)
else:
    st.info("No agent-assigned inbound calls in this range.")

# ---- Hour-of-day pattern ----
st.divider()
st.subheader("Inbound call volume by hour (Central time)")
hour_df = inbound.dropna(subset=["created_on"]).copy()
hour_df["created_on"] = pd.to_datetime(hour_df["created_on"], utc=True)
hour_df["local_hour"] = hour_df["created_on"].dt.tz_convert("America/Chicago").dt.hour
hours = hour_df.groupby("local_hour", as_index=False).size().rename(columns={"size": "calls"})
all_hours = pd.DataFrame({"local_hour": range(24)})
hours = all_hours.merge(hours, on="local_hour", how="left").fillna(0)
fig = px.bar(
    hours,
    x="local_hour",
    y="calls",
    labels={"local_hour": "Hour (Central)", "calls": "Inbound calls"},
)
fig.update_layout(
    margin=dict(l=0, r=0, t=10, b=0),
    height=chart_height("default"),
    xaxis=dict(tickmode="linear"),
)
st.plotly_chart(fig, use_container_width=True)

# ---- Missed-call followups (abandoned/unbooked inbound) ----
st.divider()
st.subheader("Missed-call followups")
st.caption(
    "Inbound calls that didn't convert. Each is a potential customer worth calling back. "
    "Sorted newest first."
)
missed = inbound[inbound["call_type"].isin(["Abandoned", "Unbooked"])].copy()
if missed.empty:
    st.success("No abandoned or unbooked inbound calls in this range.")
else:
    missed_display = missed.assign(
        received=lambda d: pd.to_datetime(d["created_on"]).dt.strftime("%Y-%m-%d %H:%M"),
        duration=lambda d: d["duration_seconds"].fillna(0).map(
            lambda s: f"{int(s)//60}:{int(s)%60:02d}" if s else "—"
        ),
        phone=lambda d: d["from_phone"].fillna("—"),
        cust=lambda d: d["customer_name"].fillna("(no match)"),
    ).rename(
        columns={
            "received": "Received",
            "phone": "Phone",
            "cust": "Customer",
            "call_type": "Type",
            "duration": "Length",
            "agent_name": "Agent",
            "reason": "Reason",
            "recording_url": "Recording",
        }
    )[["Received", "Phone", "Customer", "Type", "Length", "Agent", "Reason", "Recording"]]
    st.dataframe(
        missed_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Recording": st.column_config.LinkColumn("Recording", display_text="🔊 listen"),
        },
        height=400,
    )

    csv = missed[
        ["id", "created_on", "from_phone", "customer_name", "call_type",
         "duration_seconds", "agent_name", "reason", "recording_url"]
    ].to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, file_name="missed_calls.csv", mime="text/csv")
