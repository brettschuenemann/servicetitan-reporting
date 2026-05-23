"""Jobs report — filterable table, daily trend, CSV export."""
from __future__ import annotations

from datetime import date, timedelta

import plotly.express as px
import streamlit as st

import pandas as pd

from lib.auth import require_password
from lib.database import db
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
    start = st.date_input("From", today.replace(day=1))   # month-to-date by default
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


@st.cache_data(ttl=600, show_spinner=False)
def _customer_name_map() -> dict[int, str]:
    """customer_id → customer_name from cached invoices."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT customer_id, MIN(customer_name) AS name FROM invoices "
            "WHERE customer_name IS NOT NULL GROUP BY customer_id"
        )
        return {r["customer_id"]: r["name"] for r in cur.fetchall()}


cust_lookup = _customer_name_map()
display = filtered.copy()
if "customerId" in display.columns:
    display["customer"] = display["customerId"].map(
        lambda cid: cust_lookup.get(cid, f"cust {cid}" if cid else "")
    )
if "total" in display.columns:
    display["value"] = display["total"].fillna(0).map(lambda v: f"${v:,.2f}")

display_cols = [
    c
    for c in ("jobNumber", "jobStatus", "createdOn", "completedOn",
              "customer", "value", "summary")
    if c in display.columns
]
table = display[display_cols].rename(
    columns={
        "jobNumber": "Job #",
        "jobStatus": "Status",
        "createdOn": "Created",
        "completedOn": "Completed",
        "customer": "Customer",
        "value": "Value",
        "summary": "Summary",
    }
)
st.dataframe(table, use_container_width=True, hide_index=True)

csv = filtered.to_csv(index=False).encode("utf-8")
st.download_button("Download CSV", csv, file_name="jobs.csv", mime="text/csv")
