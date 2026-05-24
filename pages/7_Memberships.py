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
from lib.loaders import get_client
from lib.style import apply_mobile_styles, chart_height


@st.cache_data(ttl=86400, show_spinner=False)
def _lookup_contact(customer_id: int) -> tuple[str, str]:
    """One API call per customer; returns (best_phone, best_email). 24h cache."""
    try:
        contacts = get_client().get_customer_contacts(customer_id)
    except Exception:
        return "", ""
    phones = sorted(
        (c for c in contacts
         if c.get("value") and c.get("type") in ("MobilePhone", "Phone")),
        key=lambda c: 0 if c["type"] == "MobilePhone" else 1,
    )
    emails = [c["value"] for c in contacts
              if c.get("value") and c.get("type") == "Email"]
    return (phones[0]["value"] if phones else ""), (emails[0] if emails else "")


def _format_phone(raw: str) -> str:
    if not raw:
        return "—"
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw

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

st.divider()

# ---- Missed membership opportunities (recent installs, no membership) -----
st.subheader("Missed membership opportunities — recent installs")
st.caption(
    "Every install without a membership is a missed enrollment. **Non-member** "
    "= the customer has no Active membership on the books today, so they're "
    "still fresh outreach targets. Use the toggles below to pick which signals "
    "qualify as an install — combine them with OR."
)

# Configurable install signals. Default: BU name + Equipment item — the two
# zero-setup signals that work even if one is mistagged.
c1, c2, c3, c4, c5 = st.columns([1, 1.2, 1.2, 1.6, 1])
with c1:
    _window_days = st.selectbox(
        "Lookback",
        [30, 60, 90, 180, 365],
        index=2,
        format_func=lambda d: f"Last {d} days",
        key="missed_attach_window",
    )
with c2:
    _sig_bu = st.checkbox("BU name contains 'install'", value=True, key="sig_bu")
with c3:
    _sig_equip = st.checkbox("Has Equipment line item", value=True, key="sig_equip")
with c4:
    _sig_threshold_on = st.checkbox("Invoice ≥ $", value=False, key="sig_threshold_on")
with c5:
    _threshold = st.number_input(
        "min $", min_value=500, max_value=20000, value=3000, step=500,
        label_visibility="collapsed",
        disabled=not _sig_threshold_on,
        key="sig_threshold_val",
    )

if not (_sig_bu or _sig_equip or _sig_threshold_on):
    st.warning("Pick at least one install signal above.")
    st.stop()


@st.cache_data(ttl=300, show_spinner="Finding recent installs…")
def load_recent_installs(
    days: int,
    use_bu: bool,
    use_equip: bool,
    use_threshold: bool,
    threshold: int,
) -> pd.DataFrame:
    """Recent installs joined to current membership status. Install criteria
    is the OR of the enabled signals; we also tag *which* signal matched
    so you can see why each row landed in the list."""
    cutoff = date.today() - timedelta(days=days)

    # Build OR clause dynamically — at least one signal is guaranteed enabled
    # by the caller (UI blocks the all-off case).
    or_parts: list[str] = []
    params: list = [cutoff]
    if use_bu:
        or_parts.append("i.business_unit_name ILIKE '%%install%%'")
    if use_equip:
        or_parts.append("EXISTS (SELECT 1 FROM invoice_items it2 "
                        "WHERE it2.invoice_id = i.id AND it2.item_type = 'Equipment')")
    if use_threshold:
        or_parts.append("COALESCE(i.sub_total, 0) >= %s")
        params.append(threshold)
    where_install = " OR ".join(or_parts)

    sql = f"""
        WITH installs AS (
          SELECT
            i.id                            AS invoice_id,
            i.customer_id,
            i.customer_name,
            i.invoice_date,
            i.sub_total                     AS install_value,
            i.business_unit_name,
            i.summary,
            -- Show equipment description when available (informational)
            (
              SELECT string_agg(DISTINCT it3.description, '; ' ORDER BY it3.description)
              FROM invoice_items it3
              WHERE it3.invoice_id = i.id AND it3.item_type = 'Equipment'
            )                               AS equipment,
            -- Which signal(s) matched, for transparency
            ARRAY_REMOVE(ARRAY[
              CASE WHEN i.business_unit_name ILIKE '%%install%%' THEN 'BU' END,
              CASE WHEN EXISTS (
                SELECT 1 FROM invoice_items it4
                WHERE it4.invoice_id = i.id AND it4.item_type = 'Equipment'
              ) THEN 'Equipment' END,
              CASE WHEN COALESCE(i.sub_total, 0) >= 3000 THEN '≥$3k' END
            ], NULL)                        AS signals
          FROM invoices i
          WHERE i.invoice_date >= %s
            AND i.customer_id IS NOT NULL
            AND COALESCE(i.sub_total, 0) > 0
            AND ({where_install})
        ),
        active_mem AS (
          SELECT DISTINCT customer_id
          FROM memberships
          WHERE status = 'Active' AND customer_id IS NOT NULL
        )
        SELECT
          ins.*,
          (am.customer_id IS NOT NULL) AS is_member
        FROM installs ins
        LEFT JOIN active_mem am ON am.customer_id = ins.customer_id
        ORDER BY ins.invoice_date DESC
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


installs = load_recent_installs(
    _window_days, _sig_bu, _sig_equip, _sig_threshold_on, int(_threshold)
)

if installs.empty:
    enabled = []
    if _sig_bu:        enabled.append("BU name contains 'install'")
    if _sig_equip:     enabled.append("has Equipment line item")
    if _sig_threshold_on: enabled.append(f"invoice ≥ ${_threshold:,}")
    st.info(
        f"No matching installs found in the last {_window_days} days with the "
        f"enabled signals ({' OR '.join(enabled)}). Try widening the lookback or "
        "enabling another signal."
    )
else:
    # Multiple installs per customer collapse to one row for the attach view —
    # otherwise the same customer's two installs would inflate the denominator.
    per_customer = (
        installs.sort_values("invoice_date", ascending=False)
        .groupby("customer_id", as_index=False)
        .agg(
            customer_name=("customer_name", "first"),
            business_unit_name=("business_unit_name", "first"),
            most_recent_install=("invoice_date", "max"),
            install_count=("invoice_id", "count"),
            total_install_value=("install_value", "sum"),
            equipment=("equipment", lambda s: "; ".join(sorted({e for x in s if x for e in x.split("; ")}))),
            signals=("signals", lambda s: ", ".join(sorted({sig for row in s if row for sig in row}))),
            is_member=("is_member", "max"),
        )
    )

    total_customers = len(per_customer)
    member_customers = int(per_customer["is_member"].sum())
    non_member_customers = total_customers - member_customers
    attach_rate = (member_customers / total_customers * 100) if total_customers else 0
    missed_value = float(
        per_customer.loc[~per_customer["is_member"], "total_install_value"].sum()
    )

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(
        f"Install customers (last {_window_days}d)",
        f"{total_customers:,}",
        help=f"{len(installs):,} install invoices across {total_customers:,} unique customers",
    )
    k2.metric(
        "Already on membership",
        f"{member_customers:,}",
        help="Active membership on the books today",
    )
    k3.metric(
        "Not on membership",
        f"{non_member_customers:,}",
        help="The outreach target list below",
    )
    k4.metric(
        "Attach rate",
        f"{attach_rate:.0f}%",
        help=f"${missed_value:,.0f} of install revenue went to non-members",
    )

    non_members = per_customer[~per_customer["is_member"]].copy()

    if non_members.empty:
        st.success("Every recent install customer is already on a membership. 🎉")
    else:
        st.markdown(
            f"**{non_member_customers:,} non-member install customers** "
            f"(${missed_value:,.0f} in install revenue) — sorted by most recent install first."
        )

        # Phone + email lookup (cached 24h per customer). Best-effort.
        with st.spinner("Looking up contact info…"):
            phones, emails = [], []
            for cid in non_members["customer_id"]:
                p, e = _lookup_contact(int(cid))
                phones.append(p)
                emails.append(e)
        non_members["phone"] = [_format_phone(p) for p in phones]
        non_members["email"] = [e or "—" for e in emails]

        display = non_members.assign(
            most_recent=lambda d: pd.to_datetime(d["most_recent_install"]).dt.strftime("%Y-%m-%d"),
            value=lambda d: d["total_install_value"].map(lambda v: f"${v:,.0f}"),
            bu=lambda d: d["business_unit_name"].fillna("—"),
            sig=lambda d: d["signals"].fillna("—"),
        ).rename(
            columns={
                "customer_name": "Customer",
                "most_recent": "Most recent install",
                "install_count": "# installs",
                "value": "Install value",
                "bu": "Business unit",
                "sig": "Matched on",
                "equipment": "Equipment",
                "phone": "Phone",
                "email": "Email",
            }
        )[["Customer", "Most recent install", "# installs", "Install value",
           "Business unit", "Matched on", "Equipment", "Phone", "Email"]]
        st.dataframe(display, use_container_width=True, hide_index=True, height=500)

        csv = non_members[[
            "customer_id", "customer_name", "business_unit_name", "signals",
            "most_recent_install", "install_count", "total_install_value",
            "equipment", "phone", "email",
        ]].to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download non-member install list (CSV)",
            csv,
            file_name=f"missed_membership_attach_{_window_days}d.csv",
            mime="text/csv",
        )
