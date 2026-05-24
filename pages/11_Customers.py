"""Customers — lifetime value, cohort retention, repeat-purchase behavior.

All-time view; the page shows:
  - lifetime KPIs (customers, repeat rate, avg LTV)
  - rolling 12-month new-vs-returning revenue split
  - monthly cohort retention table (cumulative revenue per customer over time)
  - top 25 customers by lifetime revenue
  - membership attach effect (members vs non-members avg LTV)
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.style import (
    apply_mobile_styles,
    chart_height,
    empty_state,
)

st.set_page_config(page_title="Customers · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Customers — lifetime value & cohorts")
st.caption(
    "Lifetime view of the customer base. New vs returning is computed from each "
    "customer's first invoice date. Cohorts group customers by their first-invoice "
    "month and track cumulative revenue forward. Includes only invoices with "
    "total > 0 (zero-dollar invoices are warranty/courtesy and don't reflect spend)."
)


@st.cache_data(ttl=600, show_spinner="Loading customer history…")
def load_customer_data() -> dict:
    today = date.today()
    with db() as conn, conn.cursor() as cur:
        # Per-customer aggregates (lifetime)
        cur.execute(
            """
            SELECT
              customer_id,
              MIN(customer_name) AS customer_name,
              COUNT(*) AS invoices,
              SUM(total) AS revenue,
              MIN(invoice_date) AS first_invoice,
              MAX(invoice_date) AS last_invoice
            FROM invoices
            WHERE customer_id IS NOT NULL AND total > 0
              AND invoice_date IS NOT NULL
            GROUP BY customer_id
            """
        )
        customers = pd.DataFrame([dict(r) for r in cur.fetchall()])

        # New vs returning revenue per month (last 24 months)
        cur.execute(
            """
            WITH firsts AS (
              SELECT customer_id, MIN(invoice_date) AS first_date
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            )
            SELECT
              DATE_TRUNC('month', i.invoice_date)::date AS month,
              CASE
                WHEN DATE_TRUNC('month', i.invoice_date) =
                     DATE_TRUNC('month', f.first_date) THEN 'New'
                ELSE 'Returning'
              END AS cohort,
              SUM(i.total) AS revenue,
              COUNT(DISTINCT i.customer_id) AS customers
            FROM invoices i
            JOIN firsts f ON f.customer_id = i.customer_id
            WHERE i.total > 0
              AND i.invoice_date >= %s
            GROUP BY 1, 2
            ORDER BY 1
            """,
            (date(today.year - 2, today.month, 1),),
        )
        new_vs_ret = pd.DataFrame([dict(r) for r in cur.fetchall()])

        # Cohort cells — revenue contributed by each cohort in each
        # "months-since-first-invoice" bucket. Pandas pivots from here.
        cur.execute(
            """
            WITH firsts AS (
              SELECT customer_id, MIN(invoice_date) AS first_date
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            )
            SELECT
              DATE_TRUNC('month', f.first_date)::date AS cohort_month,
              (EXTRACT(YEAR  FROM AGE(i.invoice_date, f.first_date)) * 12
             + EXTRACT(MONTH FROM AGE(i.invoice_date, f.first_date)))::int AS months_since,
              SUM(i.total) AS revenue,
              COUNT(DISTINCT i.customer_id) AS customers
            FROM invoices i
            JOIN firsts f ON f.customer_id = i.customer_id
            WHERE i.total > 0
            GROUP BY 1, 2
            ORDER BY 1, 2
            """
        )
        cohort_raw = pd.DataFrame([dict(r) for r in cur.fetchall()])

        # Membership attach: customers with vs without an active membership
        cur.execute(
            """
            WITH customer_rev AS (
              SELECT customer_id, SUM(total) AS lifetime_rev, COUNT(*) AS invoices
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            ),
            customer_mem AS (
              SELECT customer_id,
                     BOOL_OR(status = 'Active') AS has_active_mem
              FROM memberships
              WHERE customer_id IS NOT NULL
              GROUP BY customer_id
            )
            SELECT
              COALESCE(m.has_active_mem, FALSE) AS has_membership,
              COUNT(*) AS customers,
              SUM(c.lifetime_rev) AS revenue,
              AVG(c.lifetime_rev) AS avg_ltv,
              AVG(c.invoices) AS avg_invoices
            FROM customer_rev c
            LEFT JOIN customer_mem m ON m.customer_id = c.customer_id
            GROUP BY 1
            """
        )
        member_attach = pd.DataFrame([dict(r) for r in cur.fetchall()])

    return {
        "customers": customers,
        "new_vs_ret": new_vs_ret,
        "cohort_raw": cohort_raw,
        "member_attach": member_attach,
    }


_data = load_customer_data()
cust = _data["customers"]
new_vs_ret = _data["new_vs_ret"]
cohort_raw = _data["cohort_raw"]
member_attach = _data["member_attach"]

if cust.empty:
    empty_state("No customer history yet.")
    st.stop()

# ---- Lifetime KPIs ----
today = date.today()
last_12mo_start = pd.Timestamp(date(today.year - 1, today.month, 1))

cust["first_invoice"] = pd.to_datetime(cust["first_invoice"])
cust["last_invoice"] = pd.to_datetime(cust["last_invoice"])
cust["revenue"] = cust["revenue"].astype(float)

total_customers = len(cust)
new_12mo = int((cust["first_invoice"] >= last_12mo_start).sum())
repeat_customers = int((cust["invoices"] >= 2).sum())
repeat_rate = (repeat_customers / total_customers * 100) if total_customers else 0
avg_ltv = float(cust["revenue"].mean())
top_ltv = float(cust["revenue"].max())
top_ltv_name = cust.loc[cust["revenue"].idxmax(), "customer_name"] or "(unnamed)"

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total customers", f"{total_customers:,}", help="Customers with ≥1 paid invoice on record")
k2.metric("New in last 12 mo", f"{new_12mo:,}")
k3.metric("Repeat-purchase rate", f"{repeat_rate:.0f}%", help=f"{repeat_customers:,} customers with ≥2 invoices")
k4.metric("Avg lifetime spend", f"${avg_ltv:,.0f}", help=f"Top customer: {top_ltv_name} (${top_ltv:,.0f})")

st.divider()

# ---- New vs returning revenue per month (rolling 24mo) ----
st.subheader("New vs returning revenue — last 24 months")
st.caption(
    "**New** = revenue from customers whose first-ever invoice is in that month. "
    "**Returning** = revenue from customers who'd visited before. Healthy "
    "businesses see returning grow over time."
)
if not new_vs_ret.empty:
    new_vs_ret["month"] = pd.to_datetime(new_vs_ret["month"])
    new_vs_ret["revenue"] = new_vs_ret["revenue"].astype(float)
    fig = px.bar(
        new_vs_ret,
        x="month",
        y="revenue",
        color="cohort",
        barmode="stack",
        color_discrete_map={"New": "#0066EE", "Returning": "#00214D"},
        labels={"month": "Month", "revenue": "Revenue ($)", "cohort": ""},
    )
    fig.update_xaxes(tickformat="%b %Y", dtick="M2")
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=chart_height("tall"),
        legend=dict(orientation="h", y=-0.2),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Quick stat: what % of recent revenue is from returning customers?
    last_3mo = new_vs_ret[
        new_vs_ret["month"] >= pd.Timestamp(date(today.year, max(1, today.month - 2), 1))
    ]
    if not last_3mo.empty:
        total = float(last_3mo["revenue"].sum())
        ret_share = float(last_3mo.loc[last_3mo["cohort"] == "Returning", "revenue"].sum())
        ret_pct = (ret_share / total * 100) if total else 0
        st.caption(f"Last 3 months: **{ret_pct:.0f}%** of revenue from returning customers.")
else:
    empty_state("No invoice history yet.")

st.divider()

# ---- Cohort retention table ----
st.subheader("Cohort cumulative revenue per customer")
st.caption(
    "Rows are months of acquisition (newest at top). Cells show **cumulative "
    "revenue per customer** at 0/3/6/12/24 months since first invoice. Higher "
    "later columns = stickier customers. Blank = not enough time elapsed yet."
)
if not cohort_raw.empty:
    cohort_raw["cohort_month"] = pd.to_datetime(cohort_raw["cohort_month"])
    cohort_raw["revenue"] = cohort_raw["revenue"].astype(float)
    cohort_raw["months_since"] = cohort_raw["months_since"].astype(int)

    # Cohort size (customers acquired) = customers in months_since=0
    sizes = (
        cohort_raw[cohort_raw["months_since"] == 0]
        .groupby("cohort_month")["customers"]
        .sum()
    )

    # Revenue per cohort at each months_since (we want CUMULATIVE)
    pivot_rev = cohort_raw.pivot_table(
        index="cohort_month",
        columns="months_since",
        values="revenue",
        aggfunc="sum",
        fill_value=0,
    ).sort_index()
    cumulative = pivot_rev.cumsum(axis=1)

    # Convert to revenue-per-customer using the cohort's size
    per_cust = cumulative.div(sizes, axis=0)

    # Show the requested bucket columns — others get hidden to keep it readable
    buckets = [0, 3, 6, 12, 24]
    available = [b for b in buckets if b in per_cust.columns]

    today_ts = pd.Timestamp(today.replace(day=1))

    def _months_between(a: pd.Timestamp, b: pd.Timestamp) -> int:
        return (b.year - a.year) * 12 + (b.month - a.month)

    rows = []
    for cohort_month in per_cust.index[::-1][:18]:  # last 18 cohorts, newest first
        months_elapsed = _months_between(cohort_month, today_ts)
        row = {"Cohort": cohort_month.strftime("%Y-%m"), "Customers": int(sizes.loc[cohort_month])}
        for b in available:
            if b > months_elapsed:
                row[f"M{b}"] = "—"
            else:
                v = per_cust.loc[cohort_month, b]
                row[f"M{b}"] = f"${v:,.0f}"
        rows.append(row)

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    empty_state("Not enough history for a cohort view yet.")

st.divider()

# ---- Membership attach effect ----
st.subheader("Membership impact on LTV")
if not member_attach.empty and len(member_attach) == 2:
    member_attach = member_attach.sort_values("has_membership", ascending=False)
    members = member_attach[member_attach["has_membership"] == True].iloc[0]
    non = member_attach[member_attach["has_membership"] == False].iloc[0]
    ltv_lift = (members["avg_ltv"] / non["avg_ltv"]) if non["avg_ltv"] else 1
    attach_rate = members["customers"] / (members["customers"] + non["customers"]) * 100

    a, b, c = st.columns(3)
    a.metric(
        "Membership attach rate",
        f"{attach_rate:.0f}%",
        help=f"{int(members['customers']):,} of {int(members['customers']+non['customers']):,} customers have an active membership",
    )
    b.metric(
        "Avg LTV — members",
        f"${float(members['avg_ltv']):,.0f}",
        delta=f"{(ltv_lift - 1) * 100:+.0f}% vs non-members",
    )
    c.metric("Avg LTV — non-members", f"${float(non['avg_ltv']):,.0f}")

    st.caption(
        f"Members spend on average {ltv_lift:.1f}× what non-members do "
        f"(${float(members['avg_ltv']):,.0f} vs ${float(non['avg_ltv']):,.0f}). "
        "The membership program is your single biggest lever for LTV — every "
        "non-member install is a missed enrollment opportunity."
    )
else:
    empty_state("No membership data joined yet.")

st.divider()

# ---- Top customers by lifetime revenue ----
st.subheader("Top 25 customers — lifetime")
st.caption("Sorted by lifetime revenue. **Days since last visit** flags fade risk.")
top = cust.nlargest(25, "revenue").copy()
top["days_since"] = (pd.Timestamp(today) - top["last_invoice"]).dt.days
top["first_invoice"] = top["first_invoice"].dt.strftime("%Y-%m-%d")
top["last_invoice"] = top["last_invoice"].dt.strftime("%Y-%m-%d")
top["revenue"] = top["revenue"].map(lambda v: f"${v:,.0f}")
top = top.rename(columns={
    "customer_name": "Customer",
    "invoices": "Invoices",
    "revenue": "Lifetime $",
    "first_invoice": "First visit",
    "last_invoice": "Last visit",
    "days_since": "Days since",
})[["Customer", "Invoices", "Lifetime $", "First visit", "Last visit", "Days since"]]
st.dataframe(top, use_container_width=True, hide_index=True, height=420)

# ---- CSV export ----
all_csv = cust.assign(
    revenue=lambda d: d["revenue"].round(2),
    first_invoice=lambda d: d["first_invoice"].dt.strftime("%Y-%m-%d"),
    last_invoice=lambda d: d["last_invoice"].dt.strftime("%Y-%m-%d"),
)[["customer_id", "customer_name", "invoices", "revenue", "first_invoice", "last_invoice"]].to_csv(
    index=False
).encode("utf-8")
st.download_button(
    "Download full customer list CSV",
    all_csv,
    file_name="customer_lifetime.csv",
    mime="text/csv",
)
