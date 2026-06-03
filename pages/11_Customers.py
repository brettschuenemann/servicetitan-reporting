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

        # New vs returning revenue per month (last 24 months).
        # invoice_date IS NOT NULL guards against migration artifacts where an
        # invoice has total > 0 but a null date — those would produce NaN buckets.
        cur.execute(
            """
            WITH firsts AS (
              SELECT customer_id, MIN(invoice_date) AS first_date
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
                AND invoice_date IS NOT NULL
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
              AND i.invoice_date IS NOT NULL
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
                AND invoice_date IS NOT NULL
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
              AND i.invoice_date IS NOT NULL
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
if cohort_raw.empty:
    empty_state("Not enough history for a cohort view yet.")
else:
    # Drop any row missing the dimensions we need before casting — guards
    # against migration-era rows where invoice_date or first_date were null
    # (would otherwise produce NaN months_since and crash .astype(int)).
    cohort_raw = cohort_raw.dropna(subset=["cohort_month", "months_since"]).copy()
    cohort_raw["cohort_month"] = pd.to_datetime(cohort_raw["cohort_month"])
    cohort_raw["revenue"] = pd.to_numeric(cohort_raw["revenue"], errors="coerce").fillna(0.0)
    cohort_raw["months_since"] = (
        pd.to_numeric(cohort_raw["months_since"], errors="coerce").fillna(0).astype(int)
    )
    cohort_raw["customers"] = (
        pd.to_numeric(cohort_raw["customers"], errors="coerce").fillna(0).astype(int)
    )

    if cohort_raw.empty:
        empty_state("Not enough history for a cohort view yet.")
    else:
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

        # Convert to revenue-per-customer using the cohort's size. Cohorts
        # missing a size (shouldn't happen post-cleanup, but be safe) get NaN
        # which we render as "—".
        per_cust = cumulative.div(sizes, axis=0)

        buckets = [0, 3, 6, 12, 24]
        available = [b for b in buckets if b in per_cust.columns]
        today_ts = pd.Timestamp(today.replace(day=1))

        def _months_between(a: pd.Timestamp, b: pd.Timestamp) -> int:
            return (b.year - a.year) * 12 + (b.month - a.month)

        rows = []
        for cohort_month in per_cust.index[::-1][:18]:  # newest 18 cohorts
            months_elapsed = _months_between(cohort_month, today_ts)
            size = sizes.get(cohort_month, 0)
            row = {
                "Cohort": cohort_month.strftime("%Y-%m"),
                "Customers": int(size) if pd.notna(size) else 0,
            }
            for b in available:
                if b > months_elapsed:
                    row[f"M{b}"] = "—"
                else:
                    v = per_cust.loc[cohort_month, b] if cohort_month in per_cust.index else None
                    row[f"M{b}"] = f"${v:,.0f}" if pd.notna(v) else "—"
            rows.append(row)

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

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


# ──────────────────────── Customer detail lookup ────────────────────────
# Single-customer drill-down — useful when Fey/Jake want to scan one
# customer's history end-to-end (invoices, calls, SMS thread).
from html import escape as _esc

st.divider()
st.subheader("🔎 Customer detail lookup")
st.caption("Type a customer name or ID to see their full history including SMS thread.")

# Build a searchable label list once (cust is already in scope)
_cust_labels = (
    cust.assign(
        label=lambda d: d["customer_name"].astype(str) + "  ·  $" +
                        d["revenue"].round(0).astype(int).astype(str) +
                        "  ·  id " + d["customer_id"].astype(str)
    )
    .sort_values("revenue", ascending=False)
)
_label_to_id = dict(zip(_cust_labels["label"], _cust_labels["customer_id"]))

_picked = st.selectbox(
    "Customer",
    options=[""] + list(_cust_labels["label"]),
    index=0,
    label_visibility="collapsed",
)

if _picked:
    picked_id = int(_label_to_id[_picked])
    with db() as _dconn:
        with _dconn.cursor() as _dcur:
            # Recent invoices
            _dcur.execute("""
                SELECT invoice_date, total, summary
                FROM invoices WHERE customer_id = %s AND total > 0
                ORDER BY invoice_date DESC LIMIT 8
            """, (picked_id,))
            inv_rows = list(_dcur.fetchall())

            # Recent calls
            _dcur.execute("""
                SELECT received_on, direction, call_type, duration_seconds, agent_name
                FROM calls WHERE customer_id = %s
                ORDER BY received_on DESC LIMIT 10
            """, (picked_id,))
            call_rows = list(_dcur.fetchall())

            # SMS thread (everything we've exchanged)
            _dcur.execute("""
                SELECT direction, body, channel, sent_at, status, sent_by
                FROM sms_messages WHERE customer_id = %s
                ORDER BY sent_at ASC LIMIT 50
            """, (picked_id,))
            sms_rows = list(_dcur.fetchall())

    # Two-col layout: timeline left, SMS right
    cL, cR = st.columns([3, 2])

    with cL:
        st.markdown("**📄 Recent invoices**")
        if inv_rows:
            for r in inv_rows:
                summ = (r.get("summary") or "(no summary)").strip()[:90]
                st.markdown(
                    f"<div style='padding:4px 0;border-bottom:1px solid #F1F5F9'>"
                    f"<b>{r['invoice_date']}</b> · ${float(r['total']):,.0f}"
                    f"<br><span style='font-size:12px;color:#6B7280'>{_esc(summ)}</span>"
                    f"</div>", unsafe_allow_html=True,
                )
        else:
            st.caption("No paid invoices on file.")

        st.markdown("**☎️ Recent calls**")
        if call_rows:
            for r in call_rows:
                dur = int(r.get("duration_seconds") or 0)
                outcome = "real conv" if dur >= 90 else ("voicemail" if dur >= 30 else "no answer/hangup")
                color = "#10B981" if dur >= 90 else ("#F59E0B" if dur >= 30 else "#9CA3AF")
                agent = r.get("agent_name") or "(no agent)"
                st.markdown(
                    f"<div style='padding:4px 0;border-bottom:1px solid #F1F5F9'>"
                    f"<b>{r['received_on']:%Y-%m-%d %H:%M}</b> · "
                    f"<span style='color:{color}'>{_esc(r['direction'])}</span> · "
                    f"{_esc(r['call_type'] or '?')} · {dur}s ({outcome}) · "
                    f"<span style='color:#6B7280;font-size:12px'>{_esc(agent)}</span>"
                    f"</div>", unsafe_allow_html=True,
                )
        else:
            st.caption("No call history.")

    with cR:
        st.markdown("**💬 SMS thread**")
        if sms_rows:
            for m in sms_rows:
                is_out = m["direction"] == "outbound"
                bg = "#0066EE" if is_out else "#E5E7EB"
                fg = "white" if is_out else "#111827"
                align = "right" if is_out else "left"
                margin = "margin-left:40px" if is_out else "margin-right:40px"
                when = m["sent_at"].strftime("%b %d %H:%M")
                channel = m.get("channel") or ""
                meta = f"{when}" + (f" · {channel}" if channel and channel != "manual" else "")
                st.markdown(
                    f"<div style='margin:6px 0;{margin}'>"
                    f"<div style='background:{bg};color:{fg};padding:6px 10px;"
                    f"border-radius:12px;display:inline-block;max-width:95%;"
                    f"white-space:pre-wrap;font-size:13px;line-height:1.4'>"
                    f"{_esc(m['body'] or '')}</div>"
                    f"<div style='font-size:10px;color:#6B7280;text-align:{align};"
                    f"margin-top:2px'>{_esc(meta)}</div></div>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("No SMS history yet for this customer.")
