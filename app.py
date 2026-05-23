"""ServiceTitan Reporting — home dashboard.

Revenue = invoices (via `invoiceDate`). Matches the accountant's "Total for Income"
to within ~0.5% over Jan 2024 – Sep 2025. Maintenance contracts are surfaced as an
informational section but NOT added to revenue (those billings are already inside
the invoice ledger — adding them double-counts ~$120k of 2025 revenue).
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from lib.ai_summary import SUMMARY_DAYS, generate_summary
from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles, chart_height
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
apply_mobile_styles()
require_password()

st.title("ServiceTitan Reporting")
st.caption(
    "Revenue is summed from the ServiceTitan invoice ledger by `invoiceDate`. "
    "Reconciles with the accountant's P&L to within 0.5% over Jan 2024 – Sep 2025."
)

# ---------- AI summary (top of page, independent of date filter) ----------
_summary_today = date.today().isoformat()
_summary_header, _summary_button = st.columns([4, 1])
with _summary_header:
    st.subheader(f"AI summary — last {SUMMARY_DAYS} days")
with _summary_button:
    if st.button("Refresh", use_container_width=True, help="Force a new summary now"):
        generate_summary.clear()

try:
    with st.spinner("Generating summary with Claude…"):
        _summary_md, _summary_brief = generate_summary(_summary_today)
    with st.container(border=True):
        st.markdown(_summary_md)
    with st.expander("Show the raw data Claude saw"):
        st.code(_summary_brief, language="text")
except RuntimeError as exc:
    st.info(f"⚙️ AI summary disabled: {exc}")
except Exception as exc:
    st.warning(f"Couldn't generate AI summary: {exc}")

st.divider()

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
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=chart_height("tall"))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Not enough revenue data for the year-over-year section.")

st.divider()

# ---------- This week's daily revenue (always today + the rest of this week) ----------
# This section is independent of the sidebar date filter — it always shows the
# current calendar week so today's number is front-and-center.
_today = date.today()
_week_start = _today - timedelta(days=(_today.weekday() + 1) % 7)  # Sunday
_week_end = _week_start + timedelta(days=6)                         # Saturday
_last_week_start = _week_start - timedelta(days=7)
_last_week_end = _week_end - timedelta(days=7)

st.subheader(f"This week ({_week_start.strftime('%b %d')} – {_week_end.strftime('%b %d, %Y')})")
st.caption(
    f"Today is **{_today.strftime('%A, %b %d')}**. "
    "This view is fixed to the current calendar week; the sidebar filter doesn't affect it."
)

_week_invoices = invoices_to_dataframe(load_invoices(_week_start, _week_end))
_last_week_invoices = invoices_to_dataframe(load_invoices(_last_week_start, _last_week_end))

# Build a Sun–Sat series with zero-fill for days that have no revenue yet
_week_days = pd.date_range(start=_week_start, end=_week_end, freq="D")
if not _week_invoices.empty and "invoiceDate" in _week_invoices.columns:
    _week_daily = (
        _week_invoices.dropna(subset=["invoiceDate"])
        .set_index("invoiceDate")
        .resample("D")["total"]
        .sum()
        .reindex(_week_days, fill_value=0.0)
    )
else:
    _week_daily = pd.Series(0.0, index=_week_days)

if not _last_week_invoices.empty and "invoiceDate" in _last_week_invoices.columns:
    _last_week_daily = (
        _last_week_invoices.dropna(subset=["invoiceDate"])
        .set_index("invoiceDate")
        .resample("D")["total"]
        .sum()
        .reindex(pd.date_range(start=_last_week_start, end=_last_week_end, freq="D"), fill_value=0.0)
    )
else:
    _last_week_daily = pd.Series(
        0.0, index=pd.date_range(start=_last_week_start, end=_last_week_end, freq="D")
    )

# Stats
_today_ts = pd.Timestamp(_today)
today_rev = float(_week_daily.loc[_today_ts]) if _today_ts in _week_daily.index else 0.0
wtd = float(_week_daily.loc[:_today_ts].sum())
# Match-up "same WTD" last week: through the same DOW position
_match_through = _last_week_start + timedelta(days=(_today - _week_start).days)
last_week_wtd = float(_last_week_daily.loc[: pd.Timestamp(_match_through)].sum())
last_week_full = float(_last_week_daily.sum())

# Same day last week (for "today vs same day last week")
_last_same_day = pd.Timestamp(_today - timedelta(days=7))
last_same_day_rev = (
    float(_last_week_daily.loc[_last_same_day]) if _last_same_day in _last_week_daily.index else 0.0
)


def _delta(new: float, old: float) -> str | None:
    if not old:
        return None
    return f"{(new - old) / old * 100:+.1f}% vs ${old:,.0f}"


d1, d2, d3, d4 = st.columns(4)
d1.metric(f"Today ({_today.strftime('%a')})", f"${today_rev:,.0f}", delta=_delta(today_rev, last_same_day_rev))
d2.metric("Week-to-date", f"${wtd:,.0f}", delta=_delta(wtd, last_week_wtd))
d3.metric("Last week (same WTD)", f"${last_week_wtd:,.0f}")
d4.metric("Last week (full)", f"${last_week_full:,.0f}")

# Bar chart for the 7 days of this week with today highlighted
_chart_df = pd.DataFrame({"date": _week_days, "total": _week_daily.values})
_chart_df["day_label"] = _chart_df["date"].dt.strftime("%a %m/%d")
_chart_df["is_today"] = _chart_df["date"].dt.date == _today
_chart_df["is_future"] = _chart_df["date"].dt.date > _today

import plotly.graph_objects as go

_colors = [
    "#1f77b4" if not row.is_today else "#ff7f0e"
    for row in _chart_df.itertuples()
]
# Future days get a lighter shade
for i, row in enumerate(_chart_df.itertuples()):
    if row.is_future:
        _colors[i] = "#cccccc"

fig_week = go.Figure(
    go.Bar(
        x=_chart_df["day_label"],
        y=_chart_df["total"],
        marker_color=_colors,
        text=_chart_df["total"].map(lambda v: f"${v:,.0f}" if v else ""),
        textposition="outside",
        hovertemplate="%{x}: $%{y:,.0f}<extra></extra>",
    )
)
fig_week.update_layout(
    margin=dict(l=0, r=0, t=10, b=0),
    height=chart_height("default"),
    yaxis_title="Revenue ($)",
    showlegend=False,
)
st.plotly_chart(fig_week, use_container_width=True)
st.caption(
    "🟠 today · 🔵 past days · ⚪ days not yet started · "
    f"Compared with last week ({_last_week_start.strftime('%b %d')} – {_last_week_end.strftime('%b %d')})."
)

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
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=chart_height("tall"))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No invoice data for this range.")

with right:
    st.subheader("Jobs by status")
    if not jobs_df.empty and "jobStatus" in jobs_df.columns:
        counts = jobs_df["jobStatus"].value_counts().reset_index()
        counts.columns = ["Status", "Count"]
        fig = px.bar(counts, x="Status", y="Count", color="Status")
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=chart_height("tall"), showlegend=False)
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
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=chart_height("tall"), legend=dict(orientation="h", y=-0.2))
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
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=chart_height("compact"))
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
