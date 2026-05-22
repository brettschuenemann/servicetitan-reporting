"""Revenue report — trend, top business units, top customers, CSV export."""
from __future__ import annotations

from datetime import date, timedelta

import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.loaders import load_invoices
from lib.reporting import invoices_to_dataframe

st.set_page_config(page_title="Revenue · ServiceTitan Reporting", layout="wide")
require_password()
st.title("Revenue report")

with st.sidebar:
    st.header("Filters")
    today = date.today()
    start = st.date_input("From", today - timedelta(days=30))
    end = st.date_input("To", today)
    granularity = st.radio("Granularity", ("Day", "Week", "Month"), horizontal=True)

if end < start:
    st.error("End date is before start date.")
    st.stop()

try:
    with st.spinner("Loading invoices…"):
        invoices = load_invoices(start, end)
except Exception as exc:
    st.error(f"Failed to fetch invoices: {exc}")
    st.stop()

df = invoices_to_dataframe(invoices)
if df.empty or "total" not in df.columns:
    st.info("No invoices found for this date range.")
    st.stop()

total_revenue = float(df["total"].sum())
invoice_count = len(df)
avg_ticket = total_revenue / invoice_count if invoice_count else 0
outstanding = float(df["balance"].sum()) if "balance" in df.columns else 0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Revenue", f"${total_revenue:,.0f}")
c2.metric("Invoices", f"{invoice_count:,}")
c3.metric("Average ticket", f"${avg_ticket:,.0f}")
c4.metric("Outstanding balance", f"${outstanding:,.0f}")

st.subheader("Revenue trend")
date_col = "invoiceDate" if "invoiceDate" in df.columns else "createdOn"
if date_col in df.columns:
    rule = {"Day": "D", "Week": "W", "Month": "MS"}[granularity]
    trend = (
        df.dropna(subset=[date_col])
        .set_index(date_col)
        .resample(rule)["total"]
        .sum()
        .reset_index()
    )
    fig = px.line(
        trend,
        x=date_col,
        y="total",
        markers=True,
        labels={date_col: "Date", "total": "Revenue ($)"},
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=340)
    st.plotly_chart(fig, use_container_width=True)

left, right = st.columns(2)

with left:
    st.subheader("Top business units")
    if "businessUnitName" in df.columns:
        by_bu = (
            df.groupby("businessUnitName", dropna=True)["total"]
            .sum()
            .sort_values(ascending=False)
            .head(10)
            .reset_index()
        )
        fig = px.bar(
            by_bu,
            x="total",
            y="businessUnitName",
            orientation="h",
            labels={"total": "Revenue ($)", "businessUnitName": "Business unit"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=360, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Business unit name not present on invoices.")

with right:
    st.subheader("Top customers")
    if "customerName" in df.columns:
        by_cust = (
            df.groupby("customerName", dropna=True)["total"]
            .sum()
            .sort_values(ascending=False)
            .head(10)
            .reset_index()
        )
        fig = px.bar(
            by_cust,
            x="total",
            y="customerName",
            orientation="h",
            labels={"total": "Revenue ($)", "customerName": "Customer"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=360, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Customer name not present on invoices.")

st.subheader("Invoices")
display_cols = [
    c
    for c in (
        "id",
        "referenceNumber",
        "invoiceDate",
        "customerName",
        "businessUnitName",
        "subTotal",
        "salesTax",
        "total",
        "balance",
    )
    if c in df.columns
]
st.dataframe(
    df[display_cols] if display_cols else df,
    use_container_width=True,
    hide_index=True,
)

csv = df.to_csv(index=False).encode("utf-8")
st.download_button("Download CSV", csv, file_name="invoices.csv", mime="text/csv")
