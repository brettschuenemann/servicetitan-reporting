"""Sleeping customers — high-value customers who've gone quiet.

Definition: customer spent $X+ during a *prior* window (the "loyal period")
and $0 in the *recent* window (the "quiet period"). These are warm leads —
they already know you, they used to spend money with you, and something
caused them to stop. Reactivation is the highest-ROI proactive call list
in HVAC.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.loaders import get_client
from lib.style import apply_mobile_styles


@st.cache_data(ttl=86400, show_spinner=False)
def _lookup_phone(customer_id: int) -> str:
    """One API call per customer to fetch the best phone number. 24h cache."""
    try:
        contacts = get_client().get_customer_contacts(customer_id)
    except Exception:
        return ""
    ranked = sorted(
        (c for c in contacts
         if c.get("value") and c.get("type") in ("MobilePhone", "Phone")),
        key=lambda c: 0 if c["type"] == "MobilePhone" else 1,
    )
    return ranked[0]["value"] if ranked else ""


def _format_phone(raw: str) -> str:
    if not raw:
        return "—"
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw

st.set_page_config(page_title="Sleeping customers · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Sleeping customers — proactive call list")
st.caption(
    "High-value customers who've gone quiet. They spent meaningfully in the "
    "*loyal period*, then stopped in the *quiet period*. Sorted by historical "
    "spend so you call the biggest losses first."
)

with st.sidebar:
    st.header("Filters")
    quiet_months = st.slider(
        "Quiet period (months without a purchase)",
        min_value=3, max_value=18, value=6, step=1,
    )
    loyal_months = st.slider(
        "Loyal period (look this many months *before* the quiet period)",
        min_value=6, max_value=36, value=24, step=3,
    )
    min_spend = st.number_input(
        "Min historical spend ($)",
        min_value=0, value=500, step=100,
        help="Customer must have spent at least this much during the loyal period to count as 'sleeping'.",
    )

# Compute the windows
today = date.today()
quiet_start = today - timedelta(days=30 * quiet_months)
loyal_end = quiet_start - timedelta(days=1)
loyal_start = loyal_end - timedelta(days=30 * loyal_months)

st.markdown(
    f"**Loyal period:** {loyal_start} → {loyal_end} &nbsp;·&nbsp; "
    f"**Quiet period:** {quiet_start} → {today}"
)


@st.cache_data(ttl=300, show_spinner="Finding sleeping customers…")
def find_sleepers(
    loyal_start: date, loyal_end: date, quiet_start: date,
    today: date, min_spend: int,
) -> pd.DataFrame:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH stats AS (
              SELECT
                customer_id,
                MIN(customer_name) AS customer,
                COALESCE(SUM(total) FILTER (
                  WHERE invoice_date BETWEEN %s AND %s
                ), 0) AS loyal_revenue,
                COUNT(*) FILTER (
                  WHERE invoice_date BETWEEN %s AND %s
                ) AS loyal_invoices,
                COALESCE(SUM(total) FILTER (
                  WHERE invoice_date BETWEEN %s AND %s
                ), 0) AS quiet_revenue,
                MAX(invoice_date) AS last_invoice
              FROM invoices
              WHERE customer_id IS NOT NULL AND customer_name IS NOT NULL
              GROUP BY customer_id
            )
            SELECT * FROM stats
            WHERE loyal_revenue >= %s AND quiet_revenue = 0
            ORDER BY loyal_revenue DESC
            """,
            (loyal_start, loyal_end, loyal_start, loyal_end,
             quiet_start, today, min_spend),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


df = find_sleepers(loyal_start, loyal_end, quiet_start, today, min_spend)

if df.empty:
    st.success("No sleeping customers matching your filters.")
    st.stop()

df["loyal_revenue"] = df["loyal_revenue"].astype(float)
df["last_invoice"] = pd.to_datetime(df["last_invoice"]).dt.date
df["days_quiet"] = df["last_invoice"].map(lambda d: (today - d).days)

# ---- KPIs ----
total = len(df)
total_lost = float(df["loyal_revenue"].sum())
top_loss = float(df["loyal_revenue"].max())

c1, c2, c3, c4 = st.columns(4)
c1.metric("Sleeping customers", f"{total:,}")
c2.metric("Total historical spend", f"${total_lost:,.0f}")
c3.metric("Avg per customer", f"${total_lost / total:,.0f}" if total else "—")
c4.metric("Largest single loss", f"${top_loss:,.0f}")

st.divider()

# ---- Phone number lookup (cached 24h per customer) ----
phone_box = st.empty()
phone_box.caption(f"Looking up phone numbers for {total} customers (cached for 24h, so this is fast on subsequent loads)…")
progress = st.progress(0.0)
raw_phones = []
for i, cid in enumerate(df["customer_id"].tolist(), 1):
    raw_phones.append(_lookup_phone(int(cid)))
    if i % 5 == 0 or i == total:
        progress.progress(i / total)
progress.empty()
phone_box.empty()
df["phone"] = [_format_phone(p) for p in raw_phones]

# ---- Table ----
display = df.assign(
    revenue=lambda d: d["loyal_revenue"].map(lambda v: f"${v:,.2f}"),
    last=lambda d: d["last_invoice"].astype(str),
).rename(
    columns={
        "customer": "Customer",
        "phone": "Phone",
        "revenue": "Loyal-period spend",
        "loyal_invoices": "Invoices in loyal period",
        "last": "Last invoice",
        "days_quiet": "Days quiet",
        "customer_id": "Customer ID",
    }
)[
    [
        "Customer",
        "Phone",
        "Loyal-period spend",
        "Invoices in loyal period",
        "Last invoice",
        "Days quiet",
        "Customer ID",
    ]
]
st.dataframe(display, use_container_width=True, hide_index=True, height=500)

csv = df[
    ["customer_id", "customer", "phone", "loyal_revenue", "loyal_invoices",
     "last_invoice", "days_quiet"]
].to_csv(index=False).encode("utf-8")
st.download_button("Download CSV", csv, file_name="sleeping_customers.csv", mime="text/csv")
