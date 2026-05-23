"""ServiceTitan Reporting — home dashboard.

Revenue = invoices (via `invoiceDate`). Matches the accountant's "Total for Income"
to within ~0.5% over Jan 2024 – Sep 2025. Maintenance contracts are surfaced as an
informational section but NOT added to revenue (those billings are already inside
the invoice ledger — adding them double-counts ~$120k of 2025 revenue).
"""
from __future__ import annotations

import calendar
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.loaders import (
    get_client,
    load_invoices,
    load_jobs,
    load_memberships_with_billing,
    membership_revenue_in_range,
)
from lib.reporting import invoices_to_dataframe, jobs_to_dataframe
from lib.sync import sync_all


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _months_back(d: date, n: int) -> date:
    """Return the first day of the month n months before d."""
    y, m = d.year, d.month - n
    while m < 1:
        m += 12
        y -= 1
    return date(y, m, 1)


st.set_page_config(page_title="ServiceTitan Reporting", layout="wide")
require_password()

st.title("ServiceTitan Reporting")
st.caption(
    "Revenue is summed from the ServiceTitan invoice ledger by `invoiceDate`. "
    "Reconciles with the accountant's P&L to within 0.5% over Jan 2024 – Sep 2025."
)

# First-time setup: populate Postgres if it's empty (e.g., fresh DB).
with db() as _conn:
    with _conn.cursor() as _cur:
        _cur.execute("SELECT COUNT(*) AS n FROM sync_state WHERE entity = 'invoices'")
        _already_synced = _cur.fetchone()["n"]
if not _already_synced:
    st.warning(
        "First-time setup detected. Syncing data from ServiceTitan — this takes "
        "about 5 minutes. The dashboard will load automatically when it's done."
    )
    progress_box = st.empty()
    try:
        with db() as _conn:
            sync_all(get_client(), _conn, progress=lambda m: progress_box.info(m))
        progress_box.success("Initial sync complete. Reloading…")
        st.cache_data.clear()
        st.rerun()
    except Exception as exc:
        progress_box.error(f"Initial sync failed: {exc}")
        st.stop()


def pick_date_range() -> tuple[date, date]:
    """All ranges snap to whole calendar months."""
    today = date.today()
    this_month_end = _month_end(today)
    presets = {
        "This month": (_month_start(today), this_month_end),
        "Last 3 months": (_months_back(today, 2), this_month_end),
        "Last 6 months": (_months_back(today, 5), this_month_end),
        "Last 12 months": (_months_back(today, 11), this_month_end),
        "Year to date": (date(today.year, 1, 1), this_month_end),
        "Custom": None,
    }
    with st.sidebar:
        st.header("Filters")
        choice = st.selectbox("Date range", list(presets.keys()), index=1)
        if presets[choice] is not None:
            return presets[choice]
        default_start = _months_back(today, 2)
        default_end = this_month_end
        start = st.date_input("From", default_start)
        end = st.date_input("To", default_end)
        return _month_start(start), _month_end(end)


with st.sidebar:
    st.divider()
    st.header("Data cache")
    try:
        with db() as _conn:
            with _conn.cursor() as _cur:
                _cur.execute(
                    "SELECT entity, last_sync_at, row_count FROM sync_state ORDER BY entity"
                )
                sync_rows = _cur.fetchall()
    except Exception as _exc:
        sync_rows = []
        st.error(f"Cannot reach Postgres: {_exc}")

    if sync_rows:
        for r in sync_rows:
            st.caption(f"**{r['entity']}**: {r['row_count']:,} rows · synced {r['last_sync_at']}")
    elif sync_rows == []:
        st.warning("No data cached. Click below to sync.")

    if st.button("Sync from ServiceTitan", use_container_width=True):
        progress_box = st.empty()
        try:
            with db() as _conn:
                sync_all(get_client(), _conn, progress=lambda m: progress_box.info(m))
            progress_box.success("Sync complete.")
            st.cache_data.clear()
        except Exception as exc:
            progress_box.error(f"Sync failed: {exc}")

start, end = pick_date_range()
if end < start:
    st.error("End date is before start date.")
    st.stop()

st.markdown(
    f"**Date range:** {start.strftime('%b %Y')} – {end.strftime('%b %Y')} "
    f"({start.isoformat()} → {end.isoformat()}, whole months)"
)

try:
    with st.spinner("Loading jobs and invoices…"):
        jobs = load_jobs(start, end)
        invoices = load_invoices(start, end)
except Exception as exc:
    st.error(f"Failed to fetch data from ServiceTitan: {exc}")
    st.stop()

try:
    with st.spinner("Loading maintenance contracts…"):
        memberships_all = load_memberships_with_billing()
except Exception as exc:
    st.warning(f"Could not load maintenance contracts: {exc}")
    memberships_all = []

jobs_df = jobs_to_dataframe(jobs)
invoices_df = invoices_to_dataframe(invoices)

total_jobs = len(jobs_df)
completed_jobs = (
    int((jobs_df["jobStatus"] == "Completed").sum()) if "jobStatus" in jobs_df.columns else 0
)
total_revenue = float(invoices_df["total"].sum()) if "total" in invoices_df.columns else 0.0
avg_ticket = total_revenue / len(invoices_df) if len(invoices_df) else 0.0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total jobs", f"{total_jobs:,}")
c2.metric("Completed jobs", f"{completed_jobs:,}")
c3.metric("Total revenue", f"${total_revenue:,.0f}")
c4.metric("Avg invoice", f"${avg_ticket:,.0f}")

st.divider()

# ---------- Year-over-year (invoices only) ----------
today_d = date.today()
yoy_years = sorted({end.year, end.year - 1, end.year - 2})

try:
    with st.spinner(f"Loading {yoy_years[0]}–{yoy_years[-1]} invoices for comparison…"):
        yoy_df = invoices_to_dataframe(
            load_invoices(date(yoy_years[0], 1, 1), date(yoy_years[-1], 12, 31))
        )
except Exception as exc:
    st.error(f"Failed to load comparison data: {exc}")
    yoy_df = None

if yoy_df is not None and not yoy_df.empty and "invoiceDate" in yoy_df.columns:
    yoy_df = yoy_df.dropna(subset=["invoiceDate"]).copy()
    yoy_df["year"] = yoy_df["invoiceDate"].dt.year
    yoy_df["month_num"] = yoy_df["invoiceDate"].dt.month
    yoy_df = yoy_df[yoy_df["year"].isin(yoy_years)]

    # YTD = Jan 1 → today's month/day, applied to each year.
    ytd: dict[int, float] = {}
    for y in yoy_years:
        last_day = calendar.monthrange(y, today_d.month)[1]
        cutoff = date(y, today_d.month, min(today_d.day, last_day))
        mask = (yoy_df["year"] == y) & (yoy_df["invoiceDate"].dt.date <= cutoff)
        ytd[y] = float(yoy_df.loc[mask, "total"].sum())

    cur_y, prior_y, two_back_y = yoy_years[-1], yoy_years[-2], yoy_years[-3]

    def _pct(new: float, old: float) -> str | None:
        if not old:
            return None
        return f"{(new - old) / old * 100:+.1f}% vs ${old:,.0f}"

    st.subheader("Year-to-date comparison")
    st.caption(f"YTD = Jan 1 → {today_d.strftime('%b %d')} in each year.")
    m1, m2, m3 = st.columns(3)
    m1.metric(f"{cur_y} YTD", f"${ytd[cur_y]:,.0f}", delta=_pct(ytd[cur_y], ytd[prior_y]))
    m2.metric(f"{prior_y} YTD", f"${ytd[prior_y]:,.0f}", delta=_pct(ytd[prior_y], ytd[two_back_y]))
    m3.metric(f"{two_back_y} YTD", f"${ytd[two_back_y]:,.0f}")

    # Cumulative annual revenue line
    cum = (
        yoy_df.assign(
            month_start=lambda df: df["invoiceDate"].dt.to_period("M").dt.to_timestamp()
        )
        .groupby(["year", "month_start"], as_index=False)["total"]
        .sum()
        .sort_values(["year", "month_start"])
    )
    cum["month_num"] = cum["month_start"].dt.month
    cum = cum[~((cum["year"] == today_d.year) & (cum["month_num"] > today_d.month))]
    cum["cumulative"] = cum.groupby("year")["total"].cumsum()
    cum["month"] = cum["month_num"].apply(lambda m: calendar.month_abbr[m])
    cum["Year"] = cum["year"].astype(str)

    fig = px.line(
        cum,
        x="month",
        y="cumulative",
        color="Year",
        markers=True,
        category_orders={
            "month": list(calendar.month_abbr[1:]),
            "Year": [str(y) for y in yoy_years],
        },
        labels={"month": "Month", "cumulative": "Cumulative revenue ($)"},
    )
    fig.update_traces(hovertemplate="%{x}: $%{y:,.0f}<extra>%{fullData.name}</extra>")
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=400)
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Not enough revenue data for the year-over-year section.")

st.divider()

# ---------- Daily revenue (granular look at the selected range) ----------
st.subheader("Daily revenue")
if not invoices_df.empty and "invoiceDate" in invoices_df.columns:
    days_in_range = (end - start).days + 1
    full_range = pd.date_range(start=start, end=end, freq="D")
    daily = (
        invoices_df.dropna(subset=["invoiceDate"])
        .set_index("invoiceDate")
        .resample("D")["total"]
        .sum()
        .reindex(full_range, fill_value=0.0)
    )

    avg_day = float(daily.mean())
    median_day = float(daily.median())
    best_day_val = float(daily.max())
    best_day_idx = daily.idxmax()
    worst_nonzero_val = float(daily[daily > 0].min()) if (daily > 0).any() else 0.0
    days_with_rev = int((daily > 0).sum())
    days_zero = days_in_range - days_with_rev

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Avg per day", f"${avg_day:,.0f}")
    d2.metric("Median per day", f"${median_day:,.0f}")
    d3.metric(
        "Best day",
        f"${best_day_val:,.0f}",
        help=f"{best_day_idx.strftime('%a, %b %d, %Y')}",
    )
    d4.metric(
        "Days with revenue",
        f"{days_with_rev:,} / {days_in_range:,}",
        help=f"{days_zero:,} days had no invoiced revenue.",
    )

    # Daily bar chart with mean line
    daily_df = daily.reset_index()
    daily_df.columns = ["date", "total"]
    fig = px.bar(
        daily_df,
        x="date",
        y="total",
        labels={"date": "Date", "total": "Revenue ($)"},
    )
    fig.add_hline(
        y=avg_day,
        line_dash="dash",
        line_color="gray",
        annotation_text=f"avg ${avg_day:,.0f}",
        annotation_position="top right",
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=300)
    st.plotly_chart(fig, use_container_width=True)

    # Day-of-week breakdown
    dow_df = daily_df.copy()
    dow_df["dow"] = dow_df["date"].dt.day_name()
    dow_df["dow_num"] = dow_df["date"].dt.dayofweek
    dow_avg = (
        dow_df.groupby(["dow_num", "dow"], as_index=False)["total"]
        .mean()
        .sort_values("dow_num")
    )
    dow_total = (
        dow_df.groupby(["dow_num", "dow"], as_index=False)["total"]
        .sum()
        .sort_values("dow_num")
    )

    dow_left, dow_right = st.columns(2)
    with dow_left:
        st.caption("Average revenue per day of week")
        fig_dow = px.bar(
            dow_avg,
            x="dow",
            y="total",
            labels={"dow": "", "total": "Avg revenue ($)"},
        )
        fig_dow.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=260)
        st.plotly_chart(fig_dow, use_container_width=True)
    with dow_right:
        st.caption("Total revenue by day of week")
        fig_dow2 = px.bar(
            dow_total,
            x="dow",
            y="total",
            labels={"dow": "", "total": "Total revenue ($)"},
            color_discrete_sequence=["#2ca02c"],
        )
        fig_dow2.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=260)
        st.plotly_chart(fig_dow2, use_container_width=True)
else:
    st.info("No revenue data in this range.")

st.divider()

left, right = st.columns([3, 2])

with left:
    st.subheader("Monthly revenue")
    if not invoices_df.empty and "invoiceDate" in invoices_df.columns:
        monthly = (
            invoices_df.dropna(subset=["invoiceDate"])
            .set_index("invoiceDate")
            .resample("MS")["total"]
            .sum()
            .reset_index()
        )
        fig = px.bar(
            monthly,
            x="invoiceDate",
            y="total",
            text="total",
            labels={"invoiceDate": "Month", "total": "Revenue ($)"},
        )
        fig.update_traces(texttemplate="$%{text:,.0f}", textposition="outside")
        fig.update_xaxes(tickformat="%b %Y", dtick="M1")
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=360)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No invoice data for this range.")

with right:
    st.subheader("Jobs by status")
    if not jobs_df.empty and "jobStatus" in jobs_df.columns:
        counts = jobs_df["jobStatus"].value_counts().reset_index()
        counts.columns = ["Status", "Count"]
        fig = px.bar(counts, x="Status", y="Count", color="Status")
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=360, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No job data for this range.")

st.divider()
st.subheader("Monthly revenue: year-over-year")
st.caption(f"Comparing {' vs '.join(str(y) for y in yoy_years)}.")
if yoy_df is not None and not yoy_df.empty:
    grouped = yoy_df.groupby(["month_num", "year"], as_index=False)["total"].sum()
    grouped["month"] = grouped["month_num"].apply(lambda m: calendar.month_abbr[m])
    grouped["Year"] = grouped["year"].astype(str)
    fig = px.bar(
        grouped,
        x="month",
        y="total",
        color="Year",
        barmode="group",
        category_orders={
            "month": list(calendar.month_abbr[1:]),
            "Year": [str(y) for y in yoy_years],
        },
        labels={"month": "Month", "total": "Revenue ($)"},
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=400, legend=dict(orientation="h", y=-0.2))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Not enough invoice data to show a year-over-year comparison.")

# ---------- Maintenance contracts (informational only) ----------
st.divider()
st.subheader("Maintenance contracts")
st.caption(
    "Informational view of maintenance/membership activity in the selected range. "
    "These billings are **already inside invoice revenue above** — they are not added on top."
)

mem_billed_in_range = [
    m for m in memberships_all
    if (m.get("from") or "")[:10] and start.isoformat() <= m["from"][:10] <= end.isoformat()
    and float(m.get("billingAmount") or 0) > 0
]
mem_count = len(mem_billed_in_range)
mem_customers = len({m.get("customerId") for m in mem_billed_in_range if m.get("customerId")})
mem_value = sum(float(m.get("billingAmount") or 0) for m in mem_billed_in_range)

mc1, mc2, mc3 = st.columns(3)
mc1.metric("Contracts billed", f"{mem_count:,}")
mc2.metric("Unique customers", f"{mem_customers:,}")
mc3.metric("Contract value", f"${mem_value:,.0f}")

if mem_billed_in_range:
    mem_df = pd.DataFrame([
        {"date": pd.to_datetime(m["from"][:10]), "total": float(m["billingAmount"]), "customer_id": m.get("customerId")}
        for m in mem_billed_in_range
    ])
    monthly_mem = (
        mem_df.set_index("date").resample("MS")["total"].sum().reset_index()
    )
    fig = px.bar(
        monthly_mem,
        x="date",
        y="total",
        labels={"date": "Month", "total": "Contract value ($)"},
    )
    fig.update_xaxes(tickformat="%b %Y", dtick="M1")
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280)
    st.plotly_chart(fig, use_container_width=True)

st.divider()
st.subheader("Recent jobs")
if not jobs_df.empty:
    cols = [
        c
        for c in ("jobNumber", "jobStatus", "createdOn", "completedOn", "summary")
        if c in jobs_df.columns
    ]
    recent = jobs_df.sort_values("createdOn", ascending=False).head(25) if "createdOn" in jobs_df.columns else jobs_df.head(25)
    st.dataframe(recent[cols] if cols else recent, use_container_width=True, hide_index=True)
else:
    st.info("No jobs to show.")
