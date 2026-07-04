"""Estimate Pipeline — the first-week close-rate command center.

Winning quotes close in 1-4 days; anything untouched for a week is
mostly dead. This page owns that window:

  1. Fresh-quote workbench (last 14 days) — every open quote ranked by
     value, with last-touch detection and a 🔴 flag when a quote has
     gone 48h+ with no recorded outreach.
  2. Weekly close-rate trend — created-week cohorts, so the number
     someone owns is visible.
  3. Close rate by value band and by tech (90 days).

"Touch" = any of: outbound call to the customer, outbound SMS, or a
CSR outcome recorded — all AFTER the estimate was created. It only
sees what's logged in ServiceTitan/our DB; follow-ups from personal
cells are invisible (which is its own argument for logging them).
"""
from __future__ import annotations

import os, sys
from datetime import date, datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles, chart_height, page_header

st.set_page_config(page_title="Estimate Pipeline • Pure Comfort",
                   layout="wide", page_icon="📋")
apply_mobile_styles()
require_password()

page_header(
    "Estimate pipeline",
    "Quotes close in the first few days or not at all. Work the fresh ones "
    "before they cool — a 🔴 means no logged touch in 48+ hours.",
)


# ──────────────── Data ────────────────────────────────────────────

@st.cache_data(ttl=180, show_spinner="Loading pipeline…")
def load_pipeline() -> dict:
    with db() as conn, conn.cursor() as cur:
        # KPIs
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE status_name='Open' AND active) AS open_n,
              COALESCE(SUM(subtotal) FILTER (WHERE status_name='Open' AND active),0)::numeric AS open_val,
              COUNT(*) FILTER (WHERE created_on >= NOW() - INTERVAL '14 days'
                               AND subtotal >= 500) AS fresh_n,
              COALESCE(SUM(subtotal) FILTER (WHERE created_on >= NOW() - INTERVAL '14 days'
                               AND status_name='Open' AND active AND subtotal >= 500),0)::numeric AS fresh_open_val
            FROM estimates WHERE subtotal > 0
        """)
        kpis = dict(cur.fetchone())

        # Close rate: cohort created 14-44 days ago (mature enough to judge)
        cur.execute("""
            SELECT COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE sold_on IS NOT NULL) AS sold
            FROM estimates
            WHERE subtotal >= 1000
              AND created_on BETWEEN NOW() - INTERVAL '44 days' AND NOW() - INTERVAL '14 days'
        """)
        r = cur.fetchone()
        kpis["cohort_n"] = r["n"]
        kpis["cohort_sold"] = r["sold"]

        # Fresh workbench rows with touch detection
        cur.execute("""
            WITH fresh AS (
              SELECT e.id, e.customer_id, e.job_id, e.name, e.summary,
                     e.subtotal, e.created_on, e.business_unit_name
              FROM estimates e
              WHERE e.created_on >= NOW() - INTERVAL '14 days'
                AND e.subtotal >= 500
                AND e.status_name = 'Open' AND e.active = TRUE
            ),
            named AS (
              SELECT f.*,
                COALESCE(
                  (SELECT MIN(customer_name) FROM invoices i
                   WHERE i.customer_id = f.customer_id AND i.customer_name IS NOT NULL),
                  (SELECT MIN(customer_name) FROM calls c
                   WHERE c.customer_id = f.customer_id AND c.customer_name IS NOT NULL)
                ) AS customer_name,
                (SELECT MIN(phone) FROM customer_contacts cc
                 WHERE cc.customer_id = f.customer_id AND cc.phone IS NOT NULL) AS phone,
                (SELECT STRING_AGG(DISTINCT aa.technician_name, ', ')
                 FROM appointment_assignments aa
                 WHERE aa.job_id = f.job_id
                   AND aa.technician_name <> 'Imported Default Technician') AS tech
              FROM fresh f
            )
            SELECT n.*,
              GREATEST(
                COALESCE((SELECT MAX(c.received_on) FROM calls c
                  WHERE c.customer_id = n.customer_id AND c.direction = 'Outbound'
                    AND c.received_on > n.created_on), '1970-01-01'::timestamptz),
                COALESCE((SELECT MAX(s.sent_at) FROM sms_messages s
                  WHERE s.customer_id = n.customer_id AND s.direction = 'outbound'
                    AND s.sent_at > n.created_on), '1970-01-01'::timestamptz),
                COALESCE((SELECT MAX(o.recorded_at) FROM csr_customer_outcomes o
                  WHERE o.customer_id = n.customer_id
                    AND o.recorded_at > n.created_on), '1970-01-01'::timestamptz)
              ) AS last_touch,
              -- Did the customer call US since the quote? (engagement signal)
              (SELECT MAX(c.received_on) FROM calls c
               WHERE c.customer_id = n.customer_id AND c.direction = 'Inbound'
                 AND c.received_on > n.created_on) AS last_inbound
            FROM named n
            ORDER BY n.subtotal DESC
        """)
        workbench = pd.DataFrame([dict(r) for r in cur.fetchall()])

        # Weekly created-cohort close rate, 12 weeks
        cur.execute("""
            SELECT DATE_TRUNC('week', created_on)::date AS wk,
                   COUNT(*) AS created,
                   COUNT(*) FILTER (WHERE sold_on IS NOT NULL) AS sold,
                   COALESCE(SUM(subtotal) FILTER (WHERE sold_on IS NOT NULL),0)::numeric AS sold_val
            FROM estimates
            WHERE subtotal >= 1000 AND created_on >= NOW() - INTERVAL '12 weeks'
            GROUP BY wk ORDER BY wk
        """)
        weekly = pd.DataFrame([dict(r) for r in cur.fetchall()])

        # Close rate by band + by tech, 90d
        cur.execute("""
            SELECT CASE WHEN subtotal < 1000 THEN '<$1k'
                        WHEN subtotal < 5000 THEN '$1k-5k'
                        WHEN subtotal < 15000 THEN '$5k-15k'
                        ELSE '$15k+' END AS band,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE sold_on IS NOT NULL) AS sold
            FROM estimates
            WHERE subtotal > 0 AND created_on >= NOW() - INTERVAL '90 days'
            GROUP BY band
        """)
        bands = pd.DataFrame([dict(r) for r in cur.fetchall()])

        cur.execute("""
            SELECT aa.technician_name AS tech,
                   COUNT(DISTINCT e.id) AS n,
                   COUNT(DISTINCT e.id) FILTER (WHERE e.sold_on IS NOT NULL) AS sold,
                   COALESCE(SUM(e.subtotal) FILTER (WHERE e.sold_on IS NOT NULL),0)::numeric AS sold_val
            FROM estimates e
            JOIN appointment_assignments aa ON aa.job_id = e.job_id
              AND aa.technician_name <> 'Imported Default Technician'
            WHERE e.subtotal >= 1000 AND e.created_on >= NOW() - INTERVAL '90 days'
            GROUP BY aa.technician_name
            HAVING COUNT(DISTINCT e.id) >= 5
            ORDER BY sold_val DESC
        """)
        techs = pd.DataFrame([dict(r) for r in cur.fetchall()])

    return {"kpis": kpis, "workbench": workbench,
            "weekly": weekly, "bands": bands, "techs": techs}


data = load_pipeline()
kpis = data["kpis"]

# ──────────────── KPI strip ───────────────────────────────────────
wb = data["workbench"]
now_utc = datetime.now(timezone.utc)
if not wb.empty:
    wb["age_h"] = (now_utc - pd.to_datetime(wb["created_on"], utc=True)).dt.total_seconds() / 3600
    wb["touched"] = pd.to_datetime(wb["last_touch"], utc=True) > pd.Timestamp("1971-01-01", tz="UTC")
    wb["needs_attention"] = (~wb["touched"]) & (wb["age_h"] >= 48)
    untouched_48 = int(wb["needs_attention"].sum())
    untouched_val = float(wb.loc[wb["needs_attention"], "subtotal"].sum())
else:
    untouched_48, untouched_val = 0, 0.0

cohort_rate = (100 * kpis["cohort_sold"] / kpis["cohort_n"]) if kpis["cohort_n"] else 0

k1, k2, k3, k4 = st.columns(4)
k1.metric("Open pipeline", f"${float(kpis['open_val']):,.0f}",
          help=f"{kpis['open_n']} open estimates, all ages")
k2.metric("Fresh quotes (14d)", f"${float(kpis['fresh_open_val']):,.0f}",
          help="Open quotes ≥$500 created in the last 14 days")
k3.metric("🔴 Untouched 48h+", f"{untouched_48} (${untouched_val:,.0f})",
          help="Fresh quotes with NO logged outbound call, SMS, or CSR outcome "
               "since the quote was created")
k4.metric("Close rate (≥$1k)", f"{cohort_rate:.0f}%",
          help=f"Quotes created 14-44 days ago: {kpis['cohort_sold']}/{kpis['cohort_n']} sold. "
               "Uses a matured cohort so the number is meaningful.")

# ──────────────── Fresh-quote workbench ───────────────────────────
st.divider()
st.subheader("🔥 Fresh quotes — work these first")
st.caption(
    "Open quotes from the last 14 days, biggest first. **Touch** = logged "
    "outbound call / SMS / CSR outcome after the quote date. 📞 in the "
    "Engaged column means the CUSTOMER called us since quoting — hot."
)

if wb.empty:
    st.info("No open quotes ≥$500 created in the last 14 days.")
else:
    view = wb.copy()
    view["Status"] = view.apply(
        lambda r: "🔴 no touch" if r["needs_attention"]
        else ("🟡 fresh" if not r["touched"] else "🟢 touched"), axis=1)
    view["Quote"] = view["subtotal"].map(lambda v: f"${float(v):,.0f}")
    view["Age"] = view["age_h"].map(lambda h: f"{h/24:.1f}d")
    view["Last touch"] = view.apply(
        lambda r: pd.to_datetime(r["last_touch"]).strftime("%m-%d %H:%M")
        if r["touched"] else "—", axis=1)
    view["Engaged"] = view["last_inbound"].map(
        lambda v: "📞 called us" if pd.notna(v) else "")
    view["Customer"] = view["customer_name"].fillna("(unknown)")
    view["Tech"] = view["tech"].fillna("—")
    view["What"] = view["summary"].fillna(view["name"]).fillna("").str.slice(0, 60)
    out = view.rename(columns={"phone": "Phone", "business_unit_name": "BU"})[
        ["Status", "Quote", "Age", "Customer", "Phone", "Tech", "BU",
         "Last touch", "Engaged", "What"]]
    st.dataframe(out, use_container_width=True, hide_index=True, height=430)

    csv = view[["id", "customer_id", "customer_name", "phone", "subtotal",
                "created_on", "tech", "touched", "needs_attention"]
               ].to_csv(index=False).encode("utf-8")
    st.download_button("📥 Download workbench CSV", csv,
                       file_name=f"fresh_quotes_{date.today()}.csv", mime="text/csv")

# ──────────────── Weekly close-rate trend ─────────────────────────
st.divider()
st.subheader("📈 Weekly close rate — quotes ≥$1k by created week")
weekly = data["weekly"]
if weekly.empty:
    st.caption("Not enough data.")
else:
    weekly["rate"] = (100 * weekly["sold"] / weekly["created"]).round(0)
    fig = px.bar(weekly, x="wk", y="rate",
                 text=weekly.apply(lambda r: f"{r['sold']}/{r['created']}", axis=1),
                 labels={"wk": "Week created", "rate": "Close rate %"})
    fig.update_traces(marker_color="#0066EE", textposition="outside")
    fig.update_layout(height=chart_height("compact"), yaxis_range=[0, 100],
                      margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("_Recent weeks are still maturing — quotes can sell for another "
               "few weeks, so the last 2-3 bars will rise._")

# ──────────────── Close rate by band + tech ───────────────────────
st.divider()
c_l, c_r = st.columns(2)
with c_l:
    st.subheader("By value band (90d)")
    bands = data["bands"]
    if not bands.empty:
        order = ["<$1k", "$1k-5k", "$5k-15k", "$15k+"]
        bands["band"] = pd.Categorical(bands["band"], categories=order, ordered=True)
        bands = bands.sort_values("band")
        bands["Close rate"] = (100 * bands["sold"] / bands["n"]).round(0).map(lambda v: f"{v:.0f}%")
        st.dataframe(
            bands.rename(columns={"band": "Band", "n": "Quotes", "sold": "Sold"})[
                ["Band", "Quotes", "Sold", "Close rate"]],
            use_container_width=True, hide_index=True)
with c_r:
    st.subheader("By tech (90d, quotes ≥$1k)")
    techs = data["techs"]
    if techs.empty:
        st.caption("No tech-attributable estimates yet.")
    else:
        techs["Close rate"] = (100 * techs["sold"] / techs["n"]).round(0).map(lambda v: f"{v:.0f}%")
        techs["Sold value"] = techs["sold_val"].map(lambda v: f"${float(v):,.0f}")
        st.dataframe(
            techs.rename(columns={"tech": "Tech", "n": "Quotes", "sold": "Sold"})[
                ["Tech", "Quotes", "Sold", "Close rate", "Sold value"]],
            use_container_width=True, hide_index=True)
        st.caption("_Attribution: tech assigned to the job the estimate came from._")
