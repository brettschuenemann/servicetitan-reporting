"""Technicians — per-tech jobs, revenue, and utilization.

Revenue is attributed to a tech when they have an appointment-assignment on
a job that produced an invoice. If a job has multiple techs across multiple
appointments, the revenue is *attributed to each tech* — not split — since
ST doesn't track per-tech contribution. Use this as a leaderboard, not
strict accounting.

Migration note: about 30% of jobs are tagged to "Imported Default Technician"
and excluded here (they have no real attribution).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles, chart_height

st.set_page_config(page_title="Technicians · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Technicians — performance leaderboard")
st.caption(
    "Attribution is via ServiceTitan **appointment-assignments**: which tech "
    "was assigned to which job. Revenue is attributed to every tech on the job "
    "(not split), so multi-tech jobs count for each tech. Treat as a "
    "leaderboard, not strict accounting. Migration-imported jobs are excluded."
)

with st.sidebar:
    st.header("Filters")
    today = date.today()
    start = st.date_input("From", today.replace(day=1))   # month-to-date by default
    end = st.date_input("To", today)

if end < start:
    st.error("End date is before start date.")
    st.stop()


@st.cache_data(ttl=120, show_spinner="Loading tech data…")
def load_tech_data(s: date, e: date) -> pd.DataFrame:
    """One row per (tech, job) where the assignment is in range and tech is real."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              aa.technician_id,
              aa.technician_name,
              aa.job_id,
              aa.assigned_on,
              aa.status                  AS assignment_status,
              j.job_status,
              j.job_type_id,
              j.created_on               AS job_created_on,
              j.completed_on             AS job_completed_on,
              j.business_unit_id,
              i.id                       AS invoice_id,
              i.total                    AS invoice_total,
              i.invoice_date,
              i.customer_name
            FROM appointment_assignments aa
            LEFT JOIN jobs j ON j.id = aa.job_id
            LEFT JOIN invoices i ON i.id = j.invoice_id
            WHERE aa.technician_name IS NOT NULL
              AND aa.technician_name <> 'Imported Default Technician'
              AND aa.assigned_on::date BETWEEN %s AND %s
            """,
            (s, e),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


df = load_tech_data(start, end)
if df.empty:
    st.info("No tech-attributed assignments in this range. Try widening the date filter.")
    st.stop()

df["invoice_total"] = df["invoice_total"].fillna(0).astype(float)

# Deduplicate to (tech, job) — multiple appointments per job per tech shouldn't double-count
unique = df.drop_duplicates(subset=["technician_id", "job_id"])

# ---- KPIs ----
total_techs = unique["technician_id"].nunique()
total_jobs = unique["job_id"].nunique()
total_revenue = float(unique["invoice_total"].sum())
days_in_range = (end - start).days + 1

c1, c2, c3, c4 = st.columns(4)
c1.metric("Active techs", f"{total_techs}")
c2.metric("Job-tech assignments", f"{len(unique):,}")
c3.metric("Attributed revenue", f"${total_revenue:,.0f}")
c4.metric("Window length", f"{days_in_range} days")

st.divider()

# ---- Leaderboard ----
st.subheader("Tech leaderboard")
agg = (
    unique.groupby(["technician_id", "technician_name"], as_index=False)
    .agg(
        jobs=("job_id", "nunique"),
        paid_jobs=("invoice_total", lambda s: int((s > 0).sum())),
        revenue=("invoice_total", "sum"),
    )
    .assign(
        avg_paid_ticket=lambda d: (d["revenue"] / d["paid_jobs"].replace(0, float("nan"))).round(2),
        conversion=lambda d: (d["paid_jobs"] / d["jobs"] * 100).round(1),
        revenue_per_day=lambda d: (d["revenue"] / days_in_range).round(2),
    )
    .sort_values("revenue", ascending=False)
)
agg["avg_paid_ticket"] = agg["avg_paid_ticket"].fillna(0)

display = agg.assign(
    revenue_fmt=lambda d: d["revenue"].map(lambda v: f"${v:,.2f}"),
    avg_fmt=lambda d: d["avg_paid_ticket"].map(lambda v: f"${v:,.2f}"),
    per_day_fmt=lambda d: d["revenue_per_day"].map(lambda v: f"${v:,.0f}/day"),
    conv_fmt=lambda d: d["conversion"].map(lambda v: f"{v:.0f}%"),
).rename(
    columns={
        "technician_name": "Tech",
        "jobs": "Jobs",
        "paid_jobs": "Paid jobs",
        "conv_fmt": "Conv %",
        "revenue_fmt": "Revenue",
        "avg_fmt": "Avg / paid job",
        "per_day_fmt": "Revenue / day",
    }
)[["Tech", "Jobs", "Paid jobs", "Conv %", "Revenue", "Avg / paid job", "Revenue / day"]]
st.dataframe(display, use_container_width=True, hide_index=True)

csv = agg[["technician_id", "technician_name", "jobs", "paid_jobs", "conversion",
            "revenue", "avg_paid_ticket", "revenue_per_day"]].to_csv(index=False).encode("utf-8")
st.download_button("Download CSV", csv, file_name="tech_leaderboard.csv", mime="text/csv")

st.divider()

# ---- Charts ----
left, right = st.columns(2)
with left:
    st.subheader("Revenue by tech")
    fig = px.bar(
        agg, x="revenue", y="technician_name", orientation="h", text="revenue",
        labels={"revenue": "Revenue ($)", "technician_name": ""},
    )
    fig.update_traces(texttemplate="$%{text:,.0f}", textposition="outside")
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0), height=chart_height("default"),
        yaxis={"categoryorder": "total ascending"}, showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Avg ticket per paid job")
    fig = px.bar(
        agg, x="avg_paid_ticket", y="technician_name", orientation="h", text="avg_paid_ticket",
        labels={"avg_paid_ticket": "Avg / paid job ($)", "technician_name": ""},
        color_discrete_sequence=["#2ca02c"],
    )
    fig.update_traces(texttemplate="$%{text:,.0f}", textposition="outside")
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0), height=chart_height("default"),
        yaxis={"categoryorder": "total ascending"}, showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)

# ---- Recent jobs per tech (drill-down) ----
st.divider()
st.subheader("Recent jobs by tech")
tech_filter = st.selectbox(
    "Filter", ["All techs"] + sorted(agg["technician_name"].tolist())
)
recent = unique.copy()
if tech_filter != "All techs":
    recent = recent[recent["technician_name"] == tech_filter]
recent = recent.sort_values("job_created_on", ascending=False).head(50)
recent_display = recent.assign(
    job_date=lambda d: pd.to_datetime(d["job_created_on"]).dt.strftime("%Y-%m-%d"),
    rev=lambda d: d["invoice_total"].map(lambda v: f"${v:,.2f}"),
).rename(
    columns={
        "technician_name": "Tech",
        "job_date": "Job created",
        "customer_name": "Customer",
        "job_status": "Status",
        "rev": "Invoice",
        "job_id": "Job ID",
    }
)[["Job created", "Tech", "Customer", "Status", "Invoice", "Job ID"]]
st.dataframe(recent_display, use_container_width=True, hide_index=True, height=400)
