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

# ---- PPC jobs list (newest first) ----
st.divider()
st.subheader("Pay Per Click jobs (newest first)")


@st.cache_data(ttl=120, show_spinner=False)
def load_ppc_jobs() -> pd.DataFrame:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH cust_name AS (
                  SELECT customer_id, MIN(customer_name) AS name
                  FROM invoices WHERE customer_name IS NOT NULL GROUP BY customer_id
                )
                SELECT
                  j.id            AS job_id,
                  j.job_number,
                  j.created_on,
                  j.completed_on,
                  j.job_status,
                  j.summary,
                  j.total         AS job_total,
                  i.total         AS invoice_total,
                  i.invoice_date,
                  COALESCE(cn.name, 'Customer ' || j.customer_id::text) AS customer
                FROM jobs j
                JOIN campaigns c ON c.id = j.campaign_id
                LEFT JOIN cust_name cn ON cn.customer_id = j.customer_id
                LEFT JOIN invoices i ON i.id = j.invoice_id
                WHERE c.name = 'Pay Per Click (PPC)'
                ORDER BY j.created_on DESC
                """
            )
            return pd.DataFrame([dict(r) for r in cur.fetchall()])


ppc = load_ppc_jobs()
if ppc.empty:
    st.info("No PPC jobs in the cache yet.")
else:
    ppc["job_total"] = ppc["job_total"].fillna(0.0).astype(float)
    ppc["invoice_total"] = ppc["invoice_total"].fillna(0.0).astype(float)
    p1, p2, p3 = st.columns(3)
    p1.metric("PPC jobs", f"{len(ppc):,}")
    p2.metric("Total job value", f"${ppc['job_total'].sum():,.0f}")
    p3.metric("Total invoiced", f"${ppc['invoice_total'].sum():,.0f}")

    display = ppc.assign(
        created=lambda d: pd.to_datetime(d["created_on"]).dt.strftime("%Y-%m-%d"),
        invoiced=lambda d: pd.to_datetime(d["invoice_date"]).dt.strftime("%Y-%m-%d"),
        job_val=lambda d: d["job_total"].map(lambda v: f"${v:,.2f}"),
        inv_val=lambda d: d["invoice_total"].map(lambda v: f"${v:,.2f}"),
    ).rename(
        columns={
            "customer": "Customer",
            "created": "Created",
            "invoiced": "Invoiced",
            "job_status": "Status",
            "summary": "Summary",
            "job_val": "Job value",
            "inv_val": "Invoiced value",
            "job_number": "Job #",
            "job_id": "Job ID",
        }
    )[
        [
            "Created",
            "Customer",
            "Job #",
            "Status",
            "Job value",
            "Invoiced value",
            "Invoiced",
            "Summary",
            "Job ID",
        ]
    ]
    st.dataframe(display, use_container_width=True, hide_index=True, height=420)

    csv = ppc[
        ["job_id", "job_number", "customer", "created_on", "job_status",
         "job_total", "invoice_total", "invoice_date", "summary"]
    ].to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, file_name="ppc_jobs.csv", mime="text/csv")

# ---- Expanded PPC attribution (customer-level) ----
st.divider()
st.subheader("Expanded PPC attribution (customer lifetime)")
st.caption(
    "Counts every invoice dated **on or after the customer's first PPC job**, "
    "PLUS every invoice directly linked to a PPC-tagged job (even if backdated). "
    "Idea: the PPC ad that first acquired the customer gets credit for their "
    "ongoing business — and any invoice tied to a PPC job itself is always counted."
)


@st.cache_data(ttl=120, show_spinner=False)
def load_ppc_customer_attribution() -> tuple[pd.DataFrame, dict]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ppc_first AS (
                  SELECT j.customer_id, MIN(j.created_on) AS first_ppc_at
                    FROM jobs j
                    JOIN campaigns c ON c.id = j.campaign_id
                   WHERE c.name = 'Pay Per Click (PPC)' AND j.customer_id IS NOT NULL
                   GROUP BY j.customer_id
                ),
                ppc_direct_invoice_ids AS (
                  SELECT DISTINCT i.id
                    FROM jobs j
                    JOIN campaigns c ON c.id = j.campaign_id
                    JOIN invoices i ON i.id = j.invoice_id
                   WHERE c.name = 'Pay Per Click (PPC)'
                ),
                cust_name AS (
                  SELECT customer_id, MIN(customer_name) AS name
                    FROM invoices WHERE customer_name IS NOT NULL GROUP BY customer_id
                )
                SELECT
                  i.customer_id,
                  COALESCE(cn.name, 'Customer ' || i.customer_id::text) AS customer,
                  COUNT(*)                              AS invoices,
                  SUM(i.total)                          AS revenue,
                  MIN(i.invoice_date)                   AS first_invoice,
                  MAX(i.invoice_date)                   AS latest_invoice,
                  MIN(p.first_ppc_at)::date             AS first_ppc_date
                FROM invoices i
                JOIN ppc_first p ON p.customer_id = i.customer_id
                LEFT JOIN cust_name cn ON cn.customer_id = i.customer_id
                WHERE i.invoice_date >= p.first_ppc_at::date
                   OR i.id IN (SELECT id FROM ppc_direct_invoice_ids)
                GROUP BY i.customer_id, cn.name
                ORDER BY revenue DESC NULLS LAST
                """
            )
            by_cust = pd.DataFrame([dict(r) for r in cur.fetchall()])

            # Direct PPC revenue for comparison: only invoices linked to PPC-tagged jobs
            cur.execute(
                """
                SELECT COALESCE(SUM(i.total), 0) AS direct_revenue,
                       COUNT(DISTINCT i.id)     AS direct_invoices
                FROM jobs j
                JOIN campaigns c ON c.id = j.campaign_id
                JOIN invoices i ON i.id = j.invoice_id
                WHERE c.name = 'Pay Per Click (PPC)'
                """
            )
            row = cur.fetchone()
            direct_rev = float(row["direct_revenue"] or 0)
            direct_invoices = int(row["direct_invoices"] or 0)
    return by_cust, {"direct_revenue": direct_rev, "direct_invoices": direct_invoices}


by_cust, ppc_meta = load_ppc_customer_attribution()

if by_cust.empty:
    st.info("No PPC-attributed customers yet.")
else:
    by_cust["revenue"] = by_cust["revenue"].fillna(0.0).astype(float)
    by_cust["invoices"] = by_cust["invoices"].astype(int)

    total_customers = len(by_cust)
    total_invoices = int(by_cust["invoices"].sum())
    expanded_revenue = float(by_cust["revenue"].sum())
    direct_rev = ppc_meta["direct_revenue"]
    uplift = expanded_revenue - direct_rev
    uplift_pct = (uplift / direct_rev * 100) if direct_rev else 0

    e1, e2, e3, e4 = st.columns(4)
    e1.metric("PPC customers", f"{total_customers:,}")
    e2.metric("Their total invoices", f"{total_invoices:,}")
    e3.metric(
        "Total PPC revenue",
        f"${expanded_revenue:,.0f}",
        help=f"Direct PPC ${direct_rev:,.0f} + post-PPC repeats ${uplift:,.0f}",
    )
    e4.metric(
        "Post-PPC repeats",
        f"+${uplift:,.0f}",
        delta=f"{uplift_pct:+.0f}% on top of direct" if direct_rev else None,
        help="Additional revenue from PPC customers' follow-up invoices, on top of the direct PPC invoices.",
    )

    display = by_cust.assign(
        revenue_fmt=lambda d: d["revenue"].map(lambda v: f"${v:,.2f}"),
        first_ppc=lambda d: pd.to_datetime(d["first_ppc_date"]).dt.strftime("%Y-%m-%d"),
        first=lambda d: pd.to_datetime(d["first_invoice"]).dt.strftime("%Y-%m-%d"),
        latest=lambda d: pd.to_datetime(d["latest_invoice"]).dt.strftime("%Y-%m-%d"),
    ).rename(
        columns={
            "customer": "Customer",
            "invoices": "Invoices",
            "revenue_fmt": "Revenue",
            "first_ppc": "First PPC",
            "first": "First invoice (post-PPC)",
            "latest": "Latest invoice",
            "customer_id": "Customer ID",
        }
    )[["Customer", "First PPC", "Invoices", "Revenue", "First invoice (post-PPC)",
       "Latest invoice", "Customer ID"]]
    st.dataframe(display, use_container_width=True, hide_index=True, height=380)

    csv = by_cust[
        ["customer_id", "customer", "first_ppc_date", "invoices", "revenue",
         "first_invoice", "latest_invoice"]
    ].to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download expanded PPC CSV", csv,
        file_name="ppc_customer_attribution.csv", mime="text/csv",
    )
