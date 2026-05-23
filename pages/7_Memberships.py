"""Memberships — active count, net growth, churn, and upcoming expirations.

This tenant's memberships move from Active → Expired (no "Cancelled" status
in use), so an "Expired without renewal" is the meaningful churn signal.
Renewal = same customer has a new membership starting ≤ 60 days after the
prior membership's end date.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles, chart_height

st.set_page_config(page_title="Memberships · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Maintenance contracts — active, churn, renewals")
st.caption(
    "Tracks the membership/maintenance contract book over time. 'Expiring' is "
    "Active contracts whose term ends in the next N days — the renewal queue. "
    "Note: this tenant doesn't use the 'Cancelled' status; everything moves "
    "Active → Expired. Renewals = same customer has a new contract starting "
    "within 60 days of the prior term ending."
)


@st.cache_data(ttl=120, show_spinner="Loading membership data…")
def load_memberships() -> pd.DataFrame:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH cust_name AS (
              SELECT customer_id, MIN(customer_name) AS name
              FROM invoices WHERE customer_name IS NOT NULL GROUP BY customer_id
            )
            SELECT
              m.id, m.customer_id, m.from_date, m.to_date, m.status,
              m.billing_amount, m.billing_frequency, m.created_on, m.modified_on,
              COALESCE(cn.name, 'Customer ' || m.customer_id::text) AS customer
            FROM memberships m
            LEFT JOIN cust_name cn ON cn.customer_id = m.customer_id
            """
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


df = load_memberships()
if df.empty:
    st.info("No memberships in the cache yet. Hit Sync from the home page.")
    st.stop()

df["from_date"] = pd.to_datetime(df["from_date"], errors="coerce")
df["to_date"] = pd.to_datetime(df["to_date"], errors="coerce")
df["billing_amount"] = df["billing_amount"].fillna(0).astype(float)

today = pd.Timestamp(date.today())

# ---- KPIs ----
active = df[df["status"] == "Active"]
active_count = len(active)
active_value = float(active["billing_amount"].sum())

exp_30 = active[(active["to_date"] >= today) & (active["to_date"] <= today + pd.Timedelta(days=30))]
exp_60 = active[(active["to_date"] >= today) & (active["to_date"] <= today + pd.Timedelta(days=60))]

k1, k2, k3, k4 = st.columns(4)
k1.metric("Active contracts", f"{active_count:,}")
k2.metric("Annual contract value (active)", f"${active_value:,.0f}")
k3.metric("Expiring in 30 days", f"{len(exp_30):,}", help=f"${float(exp_30['billing_amount'].sum()):,.0f} at risk")
k4.metric("Expiring in 60 days", f"{len(exp_60):,}", help=f"${float(exp_60['billing_amount'].sum()):,.0f} at risk")

st.divider()

# ---- New vs Expiring trend (monthly) ----
st.subheader("New vs expiring contracts by month")
# New: rows where from_date is in the month
new_by_month = (
    df.dropna(subset=["from_date"])
    .assign(month=lambda d: d["from_date"].dt.to_period("M").dt.to_timestamp())
    .groupby("month").size().reset_index(name="count")
)
new_by_month["type"] = "New"

# Expiring: rows where to_date is in the month and status was/is Expired-or-active-at-expiry
exp_by_month = (
    df.dropna(subset=["to_date"])
    .assign(month=lambda d: d["to_date"].dt.to_period("M").dt.to_timestamp())
    .groupby("month").size().reset_index(name="count")
)
exp_by_month["count"] = -exp_by_month["count"]  # negative for visual contrast
exp_by_month["type"] = "Expired"

trend = pd.concat([new_by_month, exp_by_month], ignore_index=True)
# Filter to a sensible window: last 24 months
cutoff = today - pd.DateOffset(months=24)
trend = trend[trend["month"] >= cutoff]

fig = px.bar(
    trend, x="month", y="count", color="type",
    barmode="relative",
    color_discrete_map={"New": "#2ca02c", "Expired": "#d62728"},
    labels={"month": "Month", "count": "Contracts (Expired shown negative)", "type": ""},
)
fig.update_xaxes(tickformat="%b %Y", dtick="M3")
fig.update_layout(
    margin=dict(l=0, r=0, t=10, b=0), height=chart_height("default"),
    legend=dict(orientation="h", y=-0.2),
)
st.plotly_chart(fig, use_container_width=True)

# Net growth this month
this_month_start = today.replace(day=1)
new_mtd = int(
    df.dropna(subset=["from_date"])
    .pipe(lambda d: d[d["from_date"] >= this_month_start]).shape[0]
)
exp_mtd = int(
    df.dropna(subset=["to_date"])
    .pipe(lambda d: d[(d["to_date"] >= this_month_start) & (d["to_date"] <= today)]).shape[0]
)
net = new_mtd - exp_mtd
st.caption(f"Month-to-date: **{new_mtd}** new contracts, **{exp_mtd}** expired → net **{net:+d}**")

st.divider()

# ---- Renewal analysis ----
st.subheader("Renewal rate")
st.caption(
    "Of contracts that already ended, what % had the same customer sign a new "
    "contract within 60 days? Counts only contracts whose end date is >60 days "
    "ago (so the renewal window has fully passed)."
)
# Build per-customer chronological list
ended = df.dropna(subset=["to_date"])
ended = ended[ended["to_date"] < today - pd.Timedelta(days=60)]
renewed_count = 0
ended_count = 0
for cust_id, group in ended.groupby("customer_id"):
    group_sorted = group.sort_values("to_date")
    all_starts = df[df["customer_id"] == cust_id].sort_values("from_date")["from_date"].dropna()
    for _, ended_row in group_sorted.iterrows():
        ended_count += 1
        # Did this customer have a NEW from_date within 60 days after this ended?
        cutoff_end = ended_row["to_date"] + pd.Timedelta(days=60)
        # Look for any from_date > ended_row.to_date and <= cutoff_end
        new_starts = all_starts[(all_starts > ended_row["to_date"]) & (all_starts <= cutoff_end)]
        if len(new_starts) > 0:
            renewed_count += 1

renewal_rate = (renewed_count / ended_count * 100) if ended_count else 0
r1, r2, r3 = st.columns(3)
r1.metric("Ended contracts analyzed", f"{ended_count:,}")
r2.metric("Renewed within 60 days", f"{renewed_count:,}")
r3.metric("Renewal rate", f"{renewal_rate:.0f}%")

st.divider()

# ---- Upcoming expirations table ----
st.subheader("Upcoming expirations (next 90 days)")
upcoming = active[
    (active["to_date"] >= today)
    & (active["to_date"] <= today + pd.Timedelta(days=90))
].copy()
upcoming["days_until_end"] = (upcoming["to_date"] - today).dt.days
upcoming = upcoming.sort_values("to_date")

if upcoming.empty:
    st.success("No contracts expiring in the next 90 days.")
else:
    display = upcoming.assign(
        ends=lambda d: d["to_date"].dt.strftime("%Y-%m-%d"),
        value=lambda d: d["billing_amount"].map(lambda v: f"${v:,.2f}"),
    ).rename(
        columns={
            "customer": "Customer",
            "ends": "Term ends",
            "days_until_end": "Days left",
            "value": "Annual value",
            "billing_frequency": "Frequency",
            "id": "Membership ID",
        }
    )[["Customer", "Term ends", "Days left", "Annual value", "Frequency", "Membership ID"]]
    st.dataframe(display, use_container_width=True, hide_index=True, height=400)

    csv = upcoming[["id", "customer", "to_date", "days_until_end", "billing_amount",
                    "billing_frequency", "customer_id"]].to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, file_name="upcoming_expirations.csv", mime="text/csv")
