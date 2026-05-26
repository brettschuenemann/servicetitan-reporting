"""Margin — gross profit per invoice, monthly trend, service-line breakdown.

Pulls line-item cost data from `invoice_items` (populated by sync). Revenue is
*pre-tax* (invoices.sub_total); COGS is sum(quantity × cost) across items,
excluding lines with item_type='Discount'.

If items haven't been backfilled yet, the page prompts the user to do it.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.loaders import get_client
from lib.style import apply_mobile_styles, chart_height, empty_state
from lib.sync import backfill_invoice_items_from_raw

st.set_page_config(page_title="Margin · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Gross margin — what we actually earn")
st.caption(
    "Revenue is pre-tax (invoice subtotal). COGS is the sum of line-item "
    "**quantity × cost** across all items except discount lines. Margin = "
    "(Revenue − COGS) / Revenue."
)

with st.sidebar:
    st.header("Filters")
    today = date.today()
    start = st.date_input("From", today.replace(day=1))   # month-to-date
    end = st.date_input("To", today)

if end < start:
    st.error("End date is before start date.")
    st.stop()

# ---- Backfill prompt if items table is empty ----
with db() as _conn:
    with _conn.cursor() as _cur:
        _cur.execute("SELECT COUNT(*) AS n FROM invoice_items")
        _items_count = _cur.fetchone()["n"]
        _cur.execute("SELECT COUNT(*) AS n FROM invoices")
        _invoices_count = _cur.fetchone()["n"]

if _invoices_count and _items_count == 0:
    st.warning(
        f"Line items haven't been backfilled yet. We have {_invoices_count:,} cached "
        "invoices with raw payloads but no extracted items. Click below to backfill "
        "(~1–2 minutes, no API calls — pure DB work)."
    )
    if st.button("Backfill line items now", type="primary"):
        progress_box = st.empty()
        try:
            with db() as _conn:
                backfill_invoice_items_from_raw(
                    _conn, progress=lambda m: progress_box.info(m)
                )
            progress_box.success("Backfill complete. Reloading…")
            st.cache_data.clear()
            st.rerun()
        except Exception as exc:
            progress_box.error(f"Backfill failed: {exc}")
    st.stop()


@st.cache_data(ttl=300, show_spinner="Loading margin data…")
def load_margin(s: date, e: date) -> dict:
    """Pull invoice-level margin aggregates + item-type breakdown for the range."""
    with db() as conn, conn.cursor() as cur:
        # Invoice-level margin: subtotal as revenue, sum(item totalCost) as COGS.
        # Exclude Discount lines from COGS so a discount doesn't show as negative cost.
        # Per-invoice rollup. We separately track:
        #   - item_count: any non-discount line on the invoice
        #   - items_with_cost: lines where cost or total_cost is > 0
        # so we can distinguish "no items in raw" from "items present but cost=0"
        # (ST data-entry gap — most common cause of phantom 100%-margin invoices).
        cur.execute(
            """
            SELECT
              i.id,
              i.invoice_date,
              i.customer_name,
              i.business_unit_name,
              i.summary,
              i.sub_total                                  AS revenue,
              COALESCE(c.cogs, 0)                          AS cogs,
              COALESCE(c.item_count, 0)                    AS item_count,
              COALESCE(c.items_with_cost, 0)               AS items_with_cost
            FROM invoices i
            LEFT JOIN (
                SELECT
                  invoice_id,
                  SUM(COALESCE(total_cost, cost*quantity, 0)) AS cogs,
                  COUNT(*)                                    AS item_count,
                  COUNT(*) FILTER (
                    WHERE COALESCE(total_cost, cost*quantity, 0) > 0
                  )                                           AS items_with_cost
                FROM invoice_items
                WHERE COALESCE(item_type, '') <> 'Discount'
                GROUP BY invoice_id
            ) c ON c.invoice_id = i.id
            WHERE i.invoice_date BETWEEN %s AND %s
              AND COALESCE(i.sub_total, 0) > 0
            """,
            (s, e),
        )
        per_invoice = pd.DataFrame([dict(r) for r in cur.fetchall()])

        # Item-type breakdown — only counts lines with cost > 0 in the cogs
        # column so missing-cost items don't deflate the per-type margin.
        cur.execute(
            """
            SELECT
              COALESCE(NULLIF(it.item_type, ''), 'Unspecified') AS item_type,
              SUM(COALESCE(it.total, it.price * it.quantity, 0))       AS revenue,
              SUM(COALESCE(it.total_cost, it.cost * it.quantity, 0))   AS cogs,
              COUNT(*)                                                  AS items,
              COUNT(*) FILTER (
                WHERE COALESCE(it.total_cost, it.cost * it.quantity, 0) > 0
              )                                                         AS items_with_cost
            FROM invoice_items it
            JOIN invoices i ON i.id = it.invoice_id
            WHERE i.invoice_date BETWEEN %s AND %s
            GROUP BY item_type
            ORDER BY revenue DESC
            """,
            (s, e),
        )
        by_type = pd.DataFrame([dict(r) for r in cur.fetchall()])

        # Diagnostic: do invoices in this range even have items in their raw
        # JSONB? If raw->'items' is empty across the board, ServiceTitan's
        # invoice endpoint isn't returning items inline and we'd need a
        # separate items fetch.
        cur.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (
                WHERE raw ? 'items' AND jsonb_typeof(raw->'items') = 'array'
                  AND jsonb_array_length(raw->'items') > 0
              ) AS with_items_in_raw
            FROM invoices
            WHERE invoice_date BETWEEN %s AND %s
              AND COALESCE(sub_total, 0) > 0
            """,
            (s, e),
        )
        raw_diag = dict(cur.fetchone())

    return {"per_invoice": per_invoice, "by_type": by_type, "raw_diag": raw_diag}


_data = load_margin(start, end)
per_inv = _data["per_invoice"]
by_type = _data["by_type"]
raw_diag = _data["raw_diag"]

if per_inv.empty:
    empty_state("No invoices in this range.")
    st.stop()

# Compute per-invoice margin
per_inv["gross_profit"] = per_inv["revenue"] - per_inv["cogs"]
per_inv["margin_pct"] = (per_inv["gross_profit"] / per_inv["revenue"].replace(0, float("nan"))) * 100

# Three buckets for diagnostics:
#   has_cost      → at least one line has a positive cost → real margin number
#   items_no_cost → items exist but every line has cost = 0 (ST data gap)
#   no_items      → no items at all in invoice_items (raw didn't have them)
per_inv["has_cost"]      = per_inv["items_with_cost"] > 0
per_inv["items_no_cost"] = (per_inv["item_count"] > 0) & (per_inv["items_with_cost"] == 0)
per_inv["no_items"]      = per_inv["item_count"] == 0

# ---- Data quality banner + breakdown ----
total_inv     = len(per_inv)
with_cost     = int(per_inv["has_cost"].sum())
items_no_cost = int(per_inv["items_no_cost"].sum())
no_items      = int(per_inv["no_items"].sum())
coverage      = (with_cost / total_inv * 100) if total_inv else 0
raw_total     = int(raw_diag.get("total") or 0)
raw_with      = int(raw_diag.get("with_items_in_raw") or 0)
raw_pct       = (raw_with / raw_total * 100) if raw_total else 0

if coverage < 95:
    rev_no_cost = float(per_inv.loc[~per_inv["has_cost"], "revenue"].sum())
    st.warning(
        f"⚠️ **Margin coverage: {coverage:.0f}%** "
        f"({with_cost:,} of {total_inv:,} invoices have usable cost data). "
        f"The remaining {total_inv - with_cost:,} invoices "
        f"(${rev_no_cost:,.0f} revenue) are **excluded** from the margin numbers "
        "below because counting them as 100% margin would lie to you."
    )

with st.expander("📋 Why are some jobs at $0 COGS? (data-quality breakdown)"):
    st.markdown(
        f"""
**Invoices in this date range with subtotal > $0:** **{total_inv:,}**

| Bucket | Count | Share | What it means |
|---|---:|---:|---|
| ✅ Has cost data | {with_cost:,} | {coverage:.0f}% | At least one line item has a positive cost; margin is real. |
| ⚠️ Has items, no cost | {items_no_cost:,} | {(items_no_cost/total_inv*100) if total_inv else 0:.0f}% | Items exist on the invoice but every line has cost = 0 in ServiceTitan. Usually means cost wasn't entered on that SKU/pricebook item. |
| ❌ No items at all | {no_items:,} | {(no_items/total_inv*100) if total_inv else 0:.0f}% | The invoice has no line items in our cache. Either ServiceTitan didn't return them in the invoice payload, or it's a manual invoice. |

**Raw JSONB check:** {raw_with:,} of {raw_total:,} cached invoices ({raw_pct:.0f}%) have a non-empty `items` array in the raw payload from ServiceTitan.
"""
    )

    if no_items > total_inv * 0.3:
        st.error(
            "**Likely issue:** ServiceTitan's invoice endpoint isn't returning "
            "items inline for most of your invoices. We'd need to fetch items "
            "per-invoice via a separate API call to fill the gap. Let me know and "
            "I'll add that sync."
        )
    elif items_no_cost > total_inv * 0.3:
        st.info(
            "**Most likely fix:** the cost field on your pricebook items in "
            "ServiceTitan isn't populated for many SKUs. Updating cost on "
            "high-volume items in ST and re-syncing will close the gap — no "
            "code change needed on my side."
        )

    # Show a concrete example so the user can verify the diagnosis in ST.
    if items_no_cost > 0:
        sample = per_inv[per_inv["items_no_cost"]].nlargest(1, "revenue").iloc[0]
        st.caption(
            f"Example of 'items but no cost': invoice **{sample['id']}** for "
            f"**{sample['customer_name']}** ({sample['invoice_date']}), "
            f"revenue ${sample['revenue']:,.0f}, items on file: {int(sample['item_count'])}. "
            "Open this invoice in ServiceTitan and check whether the line-item costs are blank."
        )
    if no_items > 0:
        sample = per_inv[per_inv["no_items"]].nlargest(1, "revenue").iloc[0]
        st.caption(
            f"Example of 'no items': invoice **{sample['id']}** for "
            f"**{sample['customer_name']}** ({sample['invoice_date']}), "
            f"revenue ${sample['revenue']:,.0f}."
        )

# Restrict margin calcs to invoices with usable cost data
costed = per_inv[per_inv["has_cost"]].copy()

# ---- Top KPI row ----
total_rev = float(costed["revenue"].sum())
total_cogs = float(costed["cogs"].sum())
gross_profit = total_rev - total_cogs
margin_pct = (gross_profit / total_rev * 100) if total_rev else 0
avg_margin = float(costed["margin_pct"].mean()) if not costed.empty else 0

k1, k2, k3, k4 = st.columns(4)
k1.metric("Revenue (pre-tax)", f"${total_rev:,.0f}", help="Sum of invoice subtotals for invoices with cost data")
k2.metric("COGS", f"${total_cogs:,.0f}", help="Sum of quantity × cost across all non-discount line items")
k3.metric("Gross profit", f"${gross_profit:,.0f}")
k4.metric(
    "Gross margin",
    f"{margin_pct:.1f}%",
    help=f"Avg per-invoice margin: {avg_margin:.1f}% (unweighted)",
)

st.divider()

# ---- Monthly trend: revenue, cost, margin% ----
st.subheader("Monthly gross profit & margin")
monthly = (
    costed.assign(
        month=lambda d: pd.to_datetime(d["invoice_date"]).dt.to_period("M").dt.to_timestamp()
    )
    .groupby("month", as_index=False)
    .agg(revenue=("revenue", "sum"), cogs=("cogs", "sum"))
)
monthly["gross_profit"] = monthly["revenue"] - monthly["cogs"]
monthly["margin_pct"] = (monthly["gross_profit"] / monthly["revenue"].replace(0, float("nan"))) * 100

if len(monthly) > 1:
    fig = go.Figure()
    fig.add_bar(
        x=monthly["month"],
        y=monthly["gross_profit"],
        name="Gross profit",
        marker_color="#0066EE",
        text=monthly["gross_profit"].map(lambda v: f"${v:,.0f}"),
        textposition="outside",
        yaxis="y",
    )
    fig.add_scatter(
        x=monthly["month"],
        y=monthly["margin_pct"],
        name="Margin %",
        mode="lines+markers",
        line=dict(color="#F34039", width=3),
        marker=dict(size=8),
        yaxis="y2",
    )
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=chart_height("tall"),
        yaxis=dict(title="Gross profit ($)"),
        yaxis2=dict(title="Margin %", overlaying="y", side="right", ticksuffix="%", range=[0, 100]),
        xaxis=dict(tickformat="%b %Y", dtick="M1"),
        legend=dict(orientation="h", y=-0.2),
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Need at least 2 months of data for a trend. Widen the date range.")

# ---- Margin by business unit ----
col_bu, col_type = st.columns(2)

with col_bu:
    st.subheader("Margin by business unit")
    bu_df = (
        costed.dropna(subset=["business_unit_name"])
        .groupby("business_unit_name", as_index=False)
        .agg(revenue=("revenue", "sum"), cogs=("cogs", "sum"), invoices=("id", "count"))
    )
    if not bu_df.empty:
        bu_df["gross_profit"] = bu_df["revenue"] - bu_df["cogs"]
        bu_df["margin_pct"] = (bu_df["gross_profit"] / bu_df["revenue"].replace(0, float("nan"))) * 100
        bu_df = bu_df.sort_values("revenue", ascending=False)
        display = bu_df.rename(
            columns={
                "business_unit_name": "Business unit",
                "invoices": "Invoices",
                "revenue": "Revenue",
                "cogs": "COGS",
                "gross_profit": "Gross profit",
                "margin_pct": "Margin %",
            }
        )
        display["Revenue"] = display["Revenue"].map(lambda v: f"${v:,.0f}")
        display["COGS"] = display["COGS"].map(lambda v: f"${v:,.0f}")
        display["Gross profit"] = display["Gross profit"].map(lambda v: f"${v:,.0f}")
        display["Margin %"] = display["Margin %"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        empty_state("No business-unit data.")

with col_type:
    st.subheader("Margin by item type")
    if not by_type.empty:
        by_type["gross_profit"] = by_type["revenue"] - by_type["cogs"]
        by_type["margin_pct"] = (by_type["gross_profit"] / by_type["revenue"].replace(0, float("nan"))) * 100
        display = by_type.rename(
            columns={
                "item_type": "Type",
                "items": "Lines",
                "revenue": "Revenue",
                "cogs": "COGS",
                "gross_profit": "Gross profit",
                "margin_pct": "Margin %",
            }
        )
        display["Revenue"] = display["Revenue"].map(lambda v: f"${v:,.0f}")
        display["COGS"] = display["COGS"].map(lambda v: f"${v:,.0f}")
        display["Gross profit"] = display["Gross profit"].map(lambda v: f"${v:,.0f}")
        display["Margin %"] = display["Margin %"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        empty_state("No item-type data.")

st.divider()

# ---- Highest/lowest margin invoices ----
st.subheader("Margin outliers")
st.caption(
    "Min $500 revenue (smaller invoices have noisy margins). **Top** = your most "
    "profitable jobs — replicate them. **Bottom** = potential losers — investigate "
    "pricing, scope creep, or tech inefficiency."
)
sig = costed[costed["revenue"] >= 500].copy()

c_top, c_bot = st.columns(2)

with c_top:
    st.markdown("**Top 25 by gross profit**")
    top = sig.nlargest(25, "gross_profit")[
        ["invoice_date", "customer_name", "revenue", "cogs", "gross_profit", "margin_pct", "summary"]
    ].copy()
    top["revenue"] = top["revenue"].map(lambda v: f"${v:,.0f}")
    top["cogs"] = top["cogs"].map(lambda v: f"${v:,.0f}")
    top["gross_profit"] = top["gross_profit"].map(lambda v: f"${v:,.0f}")
    top["margin_pct"] = top["margin_pct"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
    top["invoice_date"] = pd.to_datetime(top["invoice_date"]).dt.strftime("%Y-%m-%d")
    top = top.rename(columns={
        "invoice_date": "Date", "customer_name": "Customer", "revenue": "Revenue",
        "cogs": "COGS", "gross_profit": "Profit", "margin_pct": "Margin %", "summary": "Work",
    })
    st.dataframe(top, use_container_width=True, hide_index=True, height=400)

with c_bot:
    st.markdown("**Bottom 25 by margin %**")
    bot = sig.nsmallest(25, "margin_pct")[
        ["invoice_date", "customer_name", "revenue", "cogs", "gross_profit", "margin_pct", "summary"]
    ].copy()
    bot["revenue"] = bot["revenue"].map(lambda v: f"${v:,.0f}")
    bot["cogs"] = bot["cogs"].map(lambda v: f"${v:,.0f}")
    bot["gross_profit"] = bot["gross_profit"].map(lambda v: f"${v:,.0f}")
    bot["margin_pct"] = bot["margin_pct"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
    bot["invoice_date"] = pd.to_datetime(bot["invoice_date"]).dt.strftime("%Y-%m-%d")
    bot = bot.rename(columns={
        "invoice_date": "Date", "customer_name": "Customer", "revenue": "Revenue",
        "cogs": "COGS", "gross_profit": "Profit", "margin_pct": "Margin %", "summary": "Work",
    })
    st.dataframe(bot, use_container_width=True, hide_index=True, height=400)

# ---- CSV export ----
st.divider()
csv = costed.assign(
    margin_pct=lambda d: d["margin_pct"].round(1),
    gross_profit=lambda d: d["gross_profit"].round(2),
)[["id", "invoice_date", "customer_name", "business_unit_name", "revenue",
   "cogs", "gross_profit", "margin_pct", "summary"]].to_csv(index=False).encode("utf-8")
st.download_button("Download margin detail CSV", csv, file_name="margin_detail.csv", mime="text/csv")
