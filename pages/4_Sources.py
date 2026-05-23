"""Sources — lead-source attribution for ServiceTitan jobs.

Only counts jobs whose `campaignId` points to a real marketing campaign in this
tenant. Jobs mapped to the "Imported Default Campaign" (migration placeholder)
are excluded since they carry no real attribution.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles, chart_height

st.set_page_config(page_title="Sources · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Lead sources")
st.caption(
    "Revenue attributed to marketing campaigns. **Only counts jobs created in "
    "ServiceTitan natively** — jobs imported from the legacy system are tagged "
    "'Imported Default Campaign' and excluded here because they carry no real "
    "attribution."
)


@st.cache_data(ttl=120, show_spinner="Loading source attribution…")
def load_source_data() -> tuple[pd.DataFrame, dict]:
    """Pull jobs ↔ campaigns ↔ invoices, return aggregated by source + meta."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  j.id            AS job_id,
                  j.completed_on,
                  j.created_on    AS job_created_on,
                  j.invoice_id,
                  j.campaign_id,
                  c.name          AS campaign_name,
                  c.active        AS campaign_active,
                  i.total         AS invoice_total,
                  i.invoice_date,
                  i.customer_name
                FROM jobs j
                JOIN campaigns c ON c.id = j.campaign_id
                LEFT JOIN invoices i ON i.id = j.invoice_id
                WHERE c.name <> 'Imported Default Campaign'
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT MIN(created_on) AS first_real, MAX(created_on) AS last_real "
                        "FROM jobs WHERE campaign_id IN (SELECT id FROM campaigns "
                        "WHERE name <> 'Imported Default Campaign')")
            meta = dict(cur.fetchone())
    df = pd.DataFrame(rows)
    return df, meta


df, meta = load_source_data()

if df.empty:
    st.warning(
        "No jobs with real campaign attribution yet. Jobs created in ServiceTitan "
        "natively (i.e., not migrated) will appear here once they're tagged to a "
        "real campaign."
    )
    st.stop()

st.caption(
    f"Window: **{pd.to_datetime(meta['first_real']).date()} → "
    f"{pd.to_datetime(meta['last_real']).date()}**  "
    f"({len(df):,} attributed jobs)."
)

# ---- Date filter ----
min_d = pd.to_datetime(meta["first_real"]).date()
max_d = date.today()
with st.sidebar:
    st.header("Filters")
    start = st.date_input("From", min_d, min_value=min_d, max_value=max_d)
    end = st.date_input("To", max_d, min_value=start, max_value=max_d)

# Filter by job created_on (when attribution was logged in ST)
df["job_created_on"] = pd.to_datetime(df["job_created_on"])
df["invoice_total"] = df["invoice_total"].fillna(0.0).astype(float)
mask = (df["job_created_on"].dt.date >= start) & (df["job_created_on"].dt.date <= end)
df = df[mask]

if df.empty:
    st.info("No attributed jobs in this date range.")
    st.stop()

# ---- Aggregate by source ----
agg = (
    df.groupby("campaign_name", as_index=False)
    .agg(
        jobs=("job_id", "count"),
        revenue=("invoice_total", "sum"),
        jobs_invoiced=("invoice_total", lambda s: int((s > 0).sum())),
    )
    .assign(avg_ticket=lambda d: (d["revenue"] / d["jobs"]).round(2))
    .sort_values("revenue", ascending=False)
)

# ---- KPIs ----
total_jobs = int(agg["jobs"].sum())
total_rev = float(agg["revenue"].sum())
n_sources = len(agg)
top_source = agg.iloc[0]["campaign_name"] if len(agg) else "—"

c1, c2, c3, c4 = st.columns(4)
c1.metric("Attributed jobs", f"{total_jobs:,}")
c2.metric("Attributed revenue", f"${total_rev:,.0f}")
c3.metric("Active sources", f"{n_sources}")
c4.metric("Top source", top_source)

st.divider()

# ---- Charts: revenue + avg ticket per source ----
left, right = st.columns(2)
with left:
    st.subheader("Revenue by source")
    fig = px.bar(
        agg,
        x="revenue",
        y="campaign_name",
        orientation="h",
        text="revenue",
        labels={"revenue": "Revenue ($)", "campaign_name": ""},
    )
    fig.update_traces(texttemplate="$%{text:,.0f}", textposition="outside")
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=chart_height("default"),
        yaxis={"categoryorder": "total ascending"},
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Average job value by source")
    fig = px.bar(
        agg,
        x="avg_ticket",
        y="campaign_name",
        orientation="h",
        text="avg_ticket",
        labels={"avg_ticket": "Avg invoice ($)", "campaign_name": ""},
        color_discrete_sequence=["#2ca02c"],
    )
    fig.update_traces(texttemplate="$%{text:,.0f}", textposition="outside")
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=chart_height("default"),
        yaxis={"categoryorder": "total ascending"},
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)

st.divider()
st.subheader("Source breakdown")
display = (
    agg.assign(
        revenue_fmt=lambda d: d["revenue"].map(lambda v: f"${v:,.2f}"),
        avg_ticket_fmt=lambda d: d["avg_ticket"].map(lambda v: f"${v:,.2f}"),
        share=lambda d: (d["revenue"] / d["revenue"].sum() * 100).map(lambda v: f"{v:.1f}%"),
    )
    .rename(
        columns={
            "campaign_name": "Source",
            "jobs": "Jobs",
            "jobs_invoiced": "Jobs invoiced",
            "revenue_fmt": "Revenue",
            "avg_ticket_fmt": "Avg/job",
            "share": "% of revenue",
        }
    )[["Source", "Jobs", "Jobs invoiced", "Revenue", "Avg/job", "% of revenue"]]
)
st.dataframe(display, use_container_width=True, hide_index=True)

csv = (
    agg[["campaign_name", "jobs", "jobs_invoiced", "revenue", "avg_ticket"]]
    .to_csv(index=False)
    .encode("utf-8")
)
st.download_button("Download CSV", csv, file_name="sources.csv", mime="text/csv")

# ---- Monthly trend by source ----
st.divider()
st.subheader("Monthly trend by source")
df["month"] = df["job_created_on"].dt.to_period("M").dt.to_timestamp()
trend = (
    df.groupby(["month", "campaign_name"], as_index=False)["invoice_total"]
    .sum()
    .rename(columns={"invoice_total": "revenue"})
)
fig = px.bar(
    trend,
    x="month",
    y="revenue",
    color="campaign_name",
    labels={"month": "Month", "revenue": "Revenue ($)", "campaign_name": "Source"},
)
fig.update_xaxes(tickformat="%b %Y", dtick="M1")
fig.update_layout(
    margin=dict(l=0, r=0, t=10, b=0),
    height=chart_height("tall"),
    barmode="stack",
    legend=dict(orientation="h", y=-0.2),
)
st.plotly_chart(fig, use_container_width=True)
