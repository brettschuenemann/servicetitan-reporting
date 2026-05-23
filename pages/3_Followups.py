"""Followups — unsold estimates that need attention.

An "unsold" estimate is one whose status is `Open` (still pending customer decision).
We surface them sorted by age so the oldest leads can be chased first.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from lib.auth import require_password
from lib.database import db

st.set_page_config(page_title="Followups · ServiceTitan Reporting", layout="wide")
require_password()
st.title("Followups — unsold estimates")
st.caption(
    "Estimates with status **Open** (customer hasn't decided). Sorted by age. "
    "Click a customer or estimate ID to look it up in ServiceTitan."
)


@st.cache_data(ttl=120, show_spinner="Loading estimates…")
def load_open_estimates() -> pd.DataFrame:
    """Pull all Open estimates joined with customer name from invoices."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH cust_name AS (
                  SELECT customer_id, MIN(customer_name) AS name
                  FROM invoices WHERE customer_name IS NOT NULL
                  GROUP BY customer_id
                )
                SELECT
                  e.id,
                  e.name                                              AS estimate_name,
                  e.summary,
                  e.subtotal,
                  e.tax,
                  e.created_on,
                  e.modified_on,
                  e.business_unit_name,
                  e.job_number,
                  e.customer_id,
                  COALESCE(cn.name, 'cust ' || e.customer_id::text)   AS customer,
                  EXTRACT(DAY FROM (NOW() - e.created_on))::int       AS age_days
                FROM estimates e
                LEFT JOIN cust_name cn ON cn.customer_id = e.customer_id
                WHERE e.status_name = 'Open' AND e.active = TRUE
                ORDER BY e.created_on ASC
                """
            )
            rows = cur.fetchall()
    return pd.DataFrame([dict(r) for r in rows])


df = load_open_estimates()

if df.empty:
    st.success("No open estimates. Inbox zero!")
    st.stop()

# ---- KPI row ----
total_value = float(df["subtotal"].sum())
count = len(df)
avg_age = float(df["age_days"].mean())
stale_60 = int((df["age_days"] >= 60).sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("Open estimates", f"{count:,}")
c2.metric("Pipeline value", f"${total_value:,.0f}")
c3.metric("Average age", f"{avg_age:.0f} days")
c4.metric("Stale (60+ days)", f"{stale_60:,}", help="Estimates created 60+ days ago.")

st.divider()

# ---- Filters ----
fc1, fc2, fc3 = st.columns(3)
with fc1:
    age_min, age_max = st.slider(
        "Age (days)",
        min_value=0,
        max_value=int(df["age_days"].max()),
        value=(0, int(df["age_days"].max())),
    )
with fc2:
    val_min = st.number_input("Min value ($)", min_value=0, value=0, step=100)
with fc3:
    bu_options = sorted(df["business_unit_name"].dropna().unique().tolist())
    bus = st.multiselect("Business unit", bu_options, default=bu_options)

filtered = df[
    (df["age_days"] >= age_min)
    & (df["age_days"] <= age_max)
    & (df["subtotal"] >= val_min)
    & (df["business_unit_name"].isin(bus) | df["business_unit_name"].isna())
]

st.caption(
    f"{len(filtered):,} of {count:,} shown · "
    f"${float(filtered['subtotal'].sum()):,.0f} pipeline · "
    f"avg age {float(filtered['age_days'].mean()) if len(filtered) else 0:.0f} days"
)

# ---- Table ----
display = filtered.assign(
    created=lambda d: d["created_on"].dt.strftime("%Y-%m-%d"),
    value=lambda d: d["subtotal"].map(lambda v: f"${v:,.2f}"),
).rename(
    columns={
        "customer": "Customer",
        "estimate_name": "Estimate",
        "summary": "Summary",
        "value": "Value",
        "age_days": "Age (days)",
        "created": "Created",
        "business_unit_name": "Business unit",
        "job_number": "Job #",
        "id": "Estimate ID",
    }
)[
    [
        "Customer",
        "Value",
        "Age (days)",
        "Created",
        "Business unit",
        "Estimate",
        "Summary",
        "Job #",
        "Estimate ID",
    ]
]
st.dataframe(display, use_container_width=True, hide_index=True, height=480)

# ---- CSV download (use raw numeric columns for analysis) ----
csv_cols = [
    "id",
    "customer",
    "customer_id",
    "subtotal",
    "tax",
    "age_days",
    "created_on",
    "modified_on",
    "business_unit_name",
    "job_number",
    "estimate_name",
    "summary",
]
csv = filtered[csv_cols].to_csv(index=False).encode("utf-8")
st.download_button("Download CSV", csv, file_name="open_estimates.csv", mime="text/csv")

st.divider()

# ---- Age buckets bar ----
buckets = pd.cut(
    df["age_days"],
    bins=[-1, 7, 30, 60, 90, 9999],
    labels=["0-7 days", "8-30 days", "31-60 days", "61-90 days", "90+ days"],
)
bucket_counts = (
    df.groupby(buckets, observed=True)
    .agg(count=("id", "size"), value=("subtotal", "sum"))
    .reset_index()
    .rename(columns={"age_days": "Age bucket"})
)
bucket_counts["value_label"] = bucket_counts["value"].map(lambda v: f"${v:,.0f}")

import plotly.express as px

bk_left, bk_right = st.columns(2)
with bk_left:
    st.caption("Open estimates by age")
    fig1 = px.bar(
        bucket_counts,
        x="Age bucket",
        y="count",
        text="count",
        labels={"Age bucket": "", "count": "Estimates"},
    )
    fig1.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280)
    st.plotly_chart(fig1, use_container_width=True)
with bk_right:
    st.caption("Pipeline value by age")
    fig2 = px.bar(
        bucket_counts,
        x="Age bucket",
        y="value",
        text="value_label",
        labels={"Age bucket": "", "value": "Pipeline ($)"},
        color_discrete_sequence=["#2ca02c"],
    )
    fig2.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280)
    st.plotly_chart(fig2, use_container_width=True)
