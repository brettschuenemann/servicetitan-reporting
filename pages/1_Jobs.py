"""Jobs report — filterable table, daily trend, CSV export."""
from __future__ import annotations

from datetime import date, timedelta

import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.loaders import load_jobs
from lib.reporting import jobs_to_dataframe
from lib.style import apply_mobile_styles, chart_height

st.set_page_config(page_title="Jobs · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Jobs report")

with st.sidebar:
    st.header("Filters")
    today = date.today()
    start = st.date_input("From", today - timedelta(days=30))
    end = st.date_input("To", today)

if end < start:
    st.error("End date is before start date.")
    st.stop()

try:
    with st.spinner("Loading jobs…"):
        jobs = load_jobs(start, end)
except Exception as exc:
    st.error(f"Failed to fetch jobs: {exc}")
    st.stop()

df = jobs_to_dataframe(jobs)
if df.empty:
    st.info("No jobs found for this date range.")
    st.stop()

statuses = sorted(df["jobStatus"].dropna().unique().tolist()) if "jobStatus" in df.columns else []
selected = st.multiselect("Status", statuses, default=statuses) if statuses else []
filtered = df[df["jobStatus"].isin(selected)] if selected else df

c1, c2, c3 = st.columns(3)
c1.metric("Jobs in range", f"{len(df):,}")
c2.metric("Matching filter", f"{len(filtered):,}")
if "completedOn" in filtered.columns:
    c3.metric("Completed", f"{int(filtered['completedOn'].notna().sum()):,}")

st.subheader("Jobs created per day")
if "createdOn" in filtered.columns:
    daily = (
        filtered.dropna(subset=["createdOn"])
        .set_index("createdOn")
        .resample("D")
        .size()
        .reset_index(name="jobs")
    )
    fig = px.bar(daily, x="createdOn", y="jobs", labels={"createdOn": "Date", "jobs": "Jobs"})
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=chart_height("default"))
    st.plotly_chart(fig, use_container_width=True)

st.subheader(f"{len(filtered):,} jobs")
display_cols = [
    c
    for c in (
        "id",
        "jobNumber",
        "jobStatus",
        "createdOn",
        "completedOn",
        "customerId",
        "locationId",
        "businessUnitId",
        "summary",
    )
    if c in filtered.columns
]
st.dataframe(
    filtered[display_cols] if display_cols else filtered,
    use_container_width=True,
    hide_index=True,
)

csv = filtered.to_csv(index=False).encode("utf-8")
st.download_button("Download CSV", csv, file_name="jobs.csv", mime="text/csv")
