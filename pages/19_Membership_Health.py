"""Membership health — renewal pipeline and churn recapture.

The existing Memberships page (pages/7_Memberships.py) is a reporting view —
charts, trends, KPIs. This page is the *action* view: who needs a call/text
THIS WEEK to renew or recapture.

Three sections:
  1. ⚠️ Expiring soon (30/60/90 days) — proactive renewal
  2. 💀 Recently lapsed (last 90 days) — recapture window still open
  3. 📈 Churn metrics — rate, $ at risk, renewal performance over time
"""
from __future__ import annotations

import os, sys
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles, chart_height, page_header

st.set_page_config(page_title="Membership Health • Pure Comfort",
                   layout="wide", page_icon="🤝")
apply_mobile_styles()
require_password()

page_header(
    "Membership health",
    "Who needs a renewal touch this week — and who lapsed recently "
    "but might still come back.",
)


# ──────────────── Top-line metrics ────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def topline() -> dict:
    """Memberships-at-risk and renewal performance, one quick scan."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH active AS (
                  SELECT id, customer_id, to_date, billing_amount, from_date
                  FROM memberships
                  WHERE active = TRUE AND to_date IS NOT NULL
                ),
                -- Customer is over-covered for THIS membership if they have
                -- ANY other membership extending past this one's end date.
                uncovered AS (
                  SELECT a.* FROM active a
                  WHERE NOT EXISTS (
                    SELECT 1 FROM memberships m2
                    WHERE m2.customer_id = a.customer_id
                      AND m2.id <> a.id
                      AND m2.to_date > a.to_date
                  )
                )
                SELECT
                  COUNT(*) FILTER (WHERE to_date >= CURRENT_DATE) AS active_now,
                  COUNT(*) FILTER (WHERE to_date BETWEEN CURRENT_DATE
                                                     AND CURRENT_DATE + INTERVAL '30 days') AS exp_30,
                  COUNT(*) FILTER (WHERE to_date BETWEEN CURRENT_DATE + INTERVAL '31 days'
                                                     AND CURRENT_DATE + INTERVAL '60 days') AS exp_60,
                  COUNT(*) FILTER (WHERE to_date BETWEEN CURRENT_DATE + INTERVAL '61 days'
                                                     AND CURRENT_DATE + INTERVAL '90 days') AS exp_90,
                  COALESCE(SUM(billing_amount) FILTER (WHERE to_date BETWEEN CURRENT_DATE
                                                       AND CURRENT_DATE + INTERVAL '90 days'), 0)::numeric AS at_risk_dollars
                FROM uncovered
            """)
            return dict(cur.fetchone())

t = topline()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Active memberships", f"{t['active_now']:,}")
c2.metric("⚠️ Expiring ≤30 days", f"{t['exp_30']:,}",
          help="Need a renewal touch THIS WEEK")
c3.metric("Expiring 31–60 days", f"{t['exp_60']:,}")
c4.metric("$ at risk (next 90d)", f"${float(t['at_risk_dollars']):,.0f}",
          help="Sum of billing_amount across memberships expiring in 90d")


# ──────────────── Expiring soon — action list ─────────────────────
st.divider()
st.subheader("⚠️ Expiring in the next 30 days")
st.caption(
    "Ranked by lifetime revenue (highest-value customers first). Anyone "
    "without a future renewal already on file is in this list."
)

@st.cache_data(ttl=300, show_spinner="Loading expiring memberships…")
def expiring_soon(days_out: int = 30) -> pd.DataFrame:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH expiring AS (
                  SELECT m.customer_id, m.id AS membership_id, m.from_date,
                         m.to_date, m.billing_amount, m.billing_frequency,
                         m.membership_type_id
                  FROM memberships m
                  WHERE m.active = TRUE
                    AND m.to_date BETWEEN CURRENT_DATE AND CURRENT_DATE + (%s * INTERVAL '1 day')
                ),
                ltv AS (
                  SELECT customer_id, SUM(total)::numeric AS lifetime_revenue,
                         MAX(invoice_date) AS last_visit, COUNT(*) AS visits
                  FROM invoices WHERE total > 0 AND customer_id IS NOT NULL
                  GROUP BY customer_id
                ),
                nm AS (
                  SELECT customer_id, MIN(customer_name) AS name FROM invoices
                  WHERE customer_name IS NOT NULL AND customer_id IS NOT NULL
                  GROUP BY customer_id
                ),
                -- A customer is "already covered" if they have ANY other
                -- membership (not just future-dated ones — many renewals
                -- start BEFORE the old one expires) whose end date extends
                -- past the expiring item's end date.
                covered AS (
                  SELECT DISTINCT e.membership_id
                  FROM expiring e
                  WHERE EXISTS (
                    SELECT 1 FROM memberships m2
                    WHERE m2.customer_id = e.customer_id
                      AND m2.id <> e.membership_id
                      AND m2.to_date > e.to_date
                  )
                )
                SELECT e.customer_id, e.membership_id, e.from_date, e.to_date,
                       e.billing_amount, e.billing_frequency,
                       nm.name AS customer_name,
                       COALESCE(ltv.lifetime_revenue, 0)::numeric AS lifetime_revenue,
                       ltv.last_visit, ltv.visits,
                       (e.to_date - CURRENT_DATE) AS days_to_expiry,
                       (e.membership_id IN (SELECT membership_id FROM covered)) AS already_renewed
                FROM expiring e
                LEFT JOIN ltv ON ltv.customer_id = e.customer_id
                LEFT JOIN nm ON nm.customer_id = e.customer_id
                ORDER BY COALESCE(ltv.lifetime_revenue, 0) DESC, e.to_date ASC
            """, (days_out,))
            return pd.DataFrame([dict(r) for r in cur.fetchall()])

exp = expiring_soon(30)
if exp.empty:
    st.success("Nothing expiring in the next 30 days. ✅")
else:
    # Filter out already-renewed by default — show toggle
    show_renewed = st.checkbox(
        "Show memberships already renewed (next term on file)",
        value=False,
    )
    if not show_renewed:
        exp = exp[~exp["already_renewed"]]
    if exp.empty:
        st.success("All expiring memberships already have a renewal on file. ✅")
    else:
        st.caption(f"**{len(exp)} memberships need attention.**")
        exp_display = exp.copy()
        exp_display["lifetime_revenue"] = exp_display["lifetime_revenue"].astype(float)
        exp_display["billing_amount"] = exp_display["billing_amount"].astype(float)
        exp_display = exp_display.assign(
            customer=lambda d: d["customer_name"].fillna(
                d["customer_id"].astype(str).map(lambda v: f"cust {v}")
            ),
            ltv=lambda d: d["lifetime_revenue"].map(lambda v: f"${v:,.0f}"),
            billing=lambda d: d["billing_amount"].map(lambda v: f"${v:,.0f}"),
            expires=lambda d: pd.to_datetime(d["to_date"]).dt.strftime("%Y-%m-%d"),
            days=lambda d: d["days_to_expiry"],
            last_visit=lambda d: pd.to_datetime(d["last_visit"]).dt.strftime("%Y-%m-%d"),
        ).rename(columns={
            "customer": "Customer", "ltv": "Lifetime $",
            "billing": "Billing $", "expires": "Expires",
            "days": "Days left", "visits": "Visits",
            "last_visit": "Last visit",
            "billing_frequency": "Cadence",
        })[["Customer", "Lifetime $", "Visits", "Last visit",
            "Billing $", "Cadence", "Expires", "Days left"]]
        st.dataframe(exp_display, use_container_width=True, hide_index=True, height=500)

        csv = exp[
            ["customer_id", "customer_name", "lifetime_revenue", "visits",
             "billing_amount", "billing_frequency", "from_date", "to_date",
             "days_to_expiry"]
        ].to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Download expiring CSV", csv,
            file_name=f"memberships_expiring_{date.today()}.csv",
            mime="text/csv",
        )


# ──────────────── Recently lapsed — recapture pool ────────────────
st.divider()
st.subheader("💀 Recently lapsed — recapture window")
st.caption(
    "Memberships whose `to_date` has passed in the last 90 days WITHOUT a "
    "renewal on file. Within ~60 days these are still warm — they had the "
    "habit; a single call/text often re-enrolls them."
)

@st.cache_data(ttl=300, show_spinner="Loading lapsed…")
def recently_lapsed(days_back: int = 90) -> pd.DataFrame:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH lapsed AS (
                  -- Most recent lapsed membership per customer
                  SELECT DISTINCT ON (m.customer_id)
                         m.customer_id, m.id AS membership_id,
                         m.to_date AS lapsed_on, m.billing_amount
                  FROM memberships m
                  WHERE m.to_date BETWEEN CURRENT_DATE - (%s * INTERVAL '1 day')
                                      AND CURRENT_DATE - INTERVAL '1 day'
                  ORDER BY m.customer_id, m.to_date DESC
                ),
                -- Suppress if customer has ANY other membership still valid
                -- (running past today). Catches both future-dated renewals
                -- AND already-active ones that overlap.
                still_covered AS (
                  SELECT DISTINCT l.customer_id
                  FROM lapsed l
                  WHERE EXISTS (
                    SELECT 1 FROM memberships m2
                    WHERE m2.customer_id = l.customer_id
                      AND m2.id <> l.membership_id
                      AND m2.to_date > CURRENT_DATE
                  )
                ),
                ltv AS (
                  SELECT customer_id, SUM(total)::numeric AS lifetime_revenue
                  FROM invoices WHERE total > 0 GROUP BY customer_id
                ),
                nm AS (
                  SELECT customer_id, MIN(customer_name) AS name FROM invoices
                  WHERE customer_name IS NOT NULL GROUP BY customer_id
                )
                SELECT l.customer_id, l.membership_id, l.lapsed_on, l.billing_amount,
                       nm.name AS customer_name,
                       COALESCE(ltv.lifetime_revenue, 0)::numeric AS lifetime_revenue,
                       (CURRENT_DATE - l.lapsed_on) AS days_since_lapse
                FROM lapsed l
                LEFT JOIN ltv ON ltv.customer_id = l.customer_id
                LEFT JOIN nm ON nm.customer_id = l.customer_id
                WHERE l.customer_id NOT IN (SELECT customer_id FROM still_covered)
                ORDER BY ltv.lifetime_revenue DESC NULLS LAST, l.lapsed_on DESC
            """, (days_back,))
            return pd.DataFrame([dict(r) for r in cur.fetchall()])

lapsed = recently_lapsed(90)
if lapsed.empty:
    st.info("No recent lapses. Either you're killing it on renewals, or the "
            "renewal pipeline auto-renews everyone before they hit expired.")
else:
    st.caption(f"**{len(lapsed)} lapsed customers in the last 90 days.**")
    lapsed_display = lapsed.copy()
    lapsed_display["lifetime_revenue"] = lapsed_display["lifetime_revenue"].astype(float)
    lapsed_display["billing_amount"] = lapsed_display["billing_amount"].astype(float)
    lapsed_display = lapsed_display.assign(
        customer=lambda d: d["customer_name"].fillna(
            d["customer_id"].astype(str).map(lambda v: f"cust {v}")
        ),
        ltv=lambda d: d["lifetime_revenue"].map(lambda v: f"${v:,.0f}"),
        billing=lambda d: d["billing_amount"].map(lambda v: f"${v:,.0f}"),
        lapsed_on=lambda d: pd.to_datetime(d["lapsed_on"]).dt.strftime("%Y-%m-%d"),
    ).rename(columns={
        "customer": "Customer", "ltv": "Lifetime $",
        "billing": "Was billing $", "lapsed_on": "Lapsed on",
        "days_since_lapse": "Days since",
    })[["Customer", "Lifetime $", "Was billing $", "Lapsed on", "Days since"]]
    st.dataframe(lapsed_display, use_container_width=True, hide_index=True, height=500)


# ──────────────── Churn trend over time ───────────────────────────
st.divider()
st.subheader("📈 Renewal performance — last 12 months")

@st.cache_data(ttl=600, show_spinner=False)
def churn_trend() -> pd.DataFrame:
    """For each month, count: memberships that ended that month, and of
    those, how many had a subsequent renewal."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ended AS (
                  SELECT customer_id, to_date,
                         DATE_TRUNC('month', to_date)::date AS end_month
                  FROM memberships
                  WHERE to_date BETWEEN CURRENT_DATE - INTERVAL '12 months'
                                    AND CURRENT_DATE
                ),
                renewed AS (
                  SELECT e.customer_id, e.end_month
                  FROM ended e
                  WHERE EXISTS (
                    SELECT 1 FROM memberships m2
                    WHERE m2.customer_id = e.customer_id
                      AND m2.from_date BETWEEN e.to_date AND e.to_date + INTERVAL '60 days'
                  )
                )
                SELECT e.end_month AS month,
                       COUNT(*) AS ended_count,
                       (SELECT COUNT(*) FROM renewed r WHERE r.end_month = e.end_month) AS renewed_count
                FROM ended e
                GROUP BY e.end_month
                ORDER BY e.end_month
            """)
            return pd.DataFrame([dict(r) for r in cur.fetchall()])

trend = churn_trend()
if not trend.empty:
    trend["renewal_rate"] = (
        trend["renewed_count"] / trend["ended_count"].replace(0, float("nan")) * 100
    ).round(0)
    trend["churned"] = trend["ended_count"] - trend["renewed_count"]

    fig = px.bar(
        trend.melt(
            id_vars=["month"],
            value_vars=["renewed_count", "churned"],
            var_name="Status", value_name="count",
        ),
        x="month", y="count", color="Status",
        color_discrete_map={"renewed_count": "#10B981", "churned": "#F34039"},
        labels={"count": "Memberships", "month": "Month ended"},
    )
    fig.update_layout(height=chart_height("default"), barmode="stack",
                      legend_title="")
    st.plotly_chart(fig, use_container_width=True)

    avg_rate = trend["renewal_rate"].dropna().mean()
    st.caption(
        f"**Avg renewal rate over last 12 mo: {avg_rate:.0f}%.** "
        "Industry benchmark for residential HVAC MSPs is ~70–85%."
    )
else:
    st.caption("Not enough membership-end events in the last 12 months to chart yet.")
