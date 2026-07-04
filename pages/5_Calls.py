"""Calls — CSR performance, call funnel, hour-of-day, and abandoned-call followups."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from lib.auth import require_password
from lib.call_intents import INTENT_DISPLAY
from lib.database import db
from lib.style import apply_mobile_styles, chart_height, style_status_columns

st.set_page_config(page_title="Calls · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("Calls — CSR performance and call funnel")
st.caption(
    "All call data from ServiceTitan's telecom log. Inbound calls only unless "
    "noted. CSR booking rate measures **bookable** calls only (Booked + Unbooked "
    "+ Abandoned) — excluded Excused/NotLead so wrong-numbers and vendor calls "
    "don't drag the rate down."
)

with st.sidebar:
    st.header("Filters")
    today = date.today()
    start = st.date_input("From", today.replace(day=1))   # month-to-date by default
    end = st.date_input("To", today)

if end < start:
    st.error("End date is before start date.")
    st.stop()


@st.cache_data(ttl=120, show_spinner="Loading calls…")
def load_calls(s: date, e: date) -> pd.DataFrame:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.received_on, c.created_on, c.direction, c.call_type,
                   c.duration_seconds, c.from_phone, c.to_phone,
                   c.agent_id, c.agent_name, c.customer_id, c.customer_name,
                   c.campaign_name, c.job_id, c.job_number,
                   c.business_unit_name, c.recording_url, c.voicemail_url, c.reason,
                   cs.intent
            FROM calls c
            LEFT JOIN call_scores cs ON cs.call_id = c.id
            WHERE c.created_on::date BETWEEN %s AND %s
            ORDER BY c.created_on DESC
            """,
            (s, e),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


df = load_calls(start, end)
if df.empty:
    st.info("No calls in this range.")
    st.stop()

inbound = df[df["direction"] == "Inbound"].copy()
outbound = df[df["direction"] == "Outbound"].copy()

# ---- Top KPI row ----
booked = int((inbound["call_type"] == "Booked").sum())
unbooked = int((inbound["call_type"] == "Unbooked").sum())
abandoned = int((inbound["call_type"] == "Abandoned").sum())
excused = int((inbound["call_type"] == "Excused").sum())
not_lead = int((inbound["call_type"] == "NotLead").sum())
bookable = booked + unbooked + abandoned
book_rate = (booked / bookable * 100) if bookable else 0
jobs_linked = int(inbound["job_id"].notna().sum())

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Inbound calls", f"{len(inbound):,}")
k2.metric(
    "Booking rate",
    f"{book_rate:.0f}%",
    help=f"{booked:,} booked / {bookable:,} bookable (excludes Excused/NotLead)",
)
k3.metric("Abandoned", f"{abandoned:,}", help="Caller hung up before booking — missed revenue")
k4.metric("Linked to a job", f"{jobs_linked:,}")
k5.metric("Outbound", f"{len(outbound):,}")

st.divider()

# ---- Funnel ----
st.subheader("Call funnel (inbound)")
funnel_df = pd.DataFrame(
    [
        ("Booked", booked, "#2ca02c"),
        ("Unbooked", unbooked, "#d62728"),
        ("Abandoned", abandoned, "#d62728"),
        ("Excused", excused, "#9e9e9e"),
        ("NotLead", not_lead, "#9e9e9e"),
    ],
    columns=["Type", "Count", "color"],
)
fig = px.bar(
    funnel_df,
    x="Type",
    y="Count",
    color="Type",
    text="Count",
    color_discrete_map={r.Type: r.color for r in funnel_df.itertuples()},
)
fig.update_traces(textposition="outside")
fig.update_layout(
    margin=dict(l=0, r=0, t=10, b=0),
    height=chart_height("default"),
    showlegend=False,
)
st.plotly_chart(fig, use_container_width=True)

# ---- CSR leaderboard ----
st.subheader("CSR performance (inbound)")
st.caption("Booking rate = Booked / (Booked + Unbooked + Abandoned). Excused and NotLead excluded.")
csr_inbound = inbound[inbound["agent_name"].notna()].copy()
if not csr_inbound.empty:
    csr = (
        csr_inbound.groupby("agent_name", as_index=False)
        .agg(
            total=("id", "count"),
            booked=("call_type", lambda s: int((s == "Booked").sum())),
            unbooked=("call_type", lambda s: int((s == "Unbooked").sum())),
            abandoned=("call_type", lambda s: int((s == "Abandoned").sum())),
            excused=("call_type", lambda s: int((s == "Excused").sum())),
            not_lead=("call_type", lambda s: int((s == "NotLead").sum())),
        )
        .assign(
            bookable=lambda d: d["booked"] + d["unbooked"] + d["abandoned"],
            book_rate=lambda d: (d["booked"] / d["bookable"].replace(0, float("nan")) * 100).round(1),
        )
        .sort_values("total", ascending=False)
    )
    csr["book_rate"] = csr["book_rate"].fillna(0)
    display = csr.rename(
        columns={
            "agent_name": "Agent",
            "total": "Total calls",
            "booked": "Booked",
            "unbooked": "Unbooked",
            "abandoned": "Abandoned",
            "excused": "Excused",
            "not_lead": "NotLead",
            "bookable": "Bookable",
            "book_rate": "Book rate %",
        }
    )[["Agent", "Total calls", "Bookable", "Booked", "Unbooked", "Abandoned",
       "Excused", "NotLead", "Book rate %"]]
    st.dataframe(display, use_container_width=True, hide_index=True)
else:
    st.info("No agent-assigned inbound calls in this range.")

# ---- Hour-of-day pattern ----
st.divider()
st.subheader("Inbound call volume by hour (Central time)")
hour_df = inbound.dropna(subset=["created_on"]).copy()
hour_df["created_on"] = pd.to_datetime(hour_df["created_on"], utc=True)
hour_df["local_hour"] = hour_df["created_on"].dt.tz_convert("America/Chicago").dt.hour
hours = hour_df.groupby("local_hour", as_index=False).size().rename(columns={"size": "calls"})
all_hours = pd.DataFrame({"local_hour": range(24)})
hours = all_hours.merge(hours, on="local_hour", how="left").fillna(0)
fig = px.bar(
    hours,
    x="local_hour",
    y="calls",
    labels={"local_hour": "Hour (Central)", "calls": "Inbound calls"},
)
fig.update_layout(
    margin=dict(l=0, r=0, t=10, b=0),
    height=chart_height("default"),
    xaxis=dict(tickmode="linear"),
)
st.plotly_chart(fig, use_container_width=True)

# ---- Missed-call followups (abandoned/unbooked inbound) ----
st.divider()
st.subheader("Missed-call followups")
st.caption(
    "Inbound calls that didn't convert. Each is a potential customer worth calling back. "
    "Sorted newest first."
)
missed = inbound[inbound["call_type"].isin(["Abandoned", "Unbooked"])].copy()
if missed.empty:
    st.success("No abandoned or unbooked inbound calls in this range.")
else:
    missed_display = missed.assign(
        received=lambda d: pd.to_datetime(d["created_on"]).dt.strftime("%Y-%m-%d %H:%M"),
        duration=lambda d: d["duration_seconds"].fillna(0).map(
            lambda s: f"{int(s)//60}:{int(s)%60:02d}" if s else "—"
        ),
        phone=lambda d: d["from_phone"].fillna("—"),
        cust=lambda d: d["customer_name"].fillna("(no match)"),
    ).rename(
        columns={
            "received": "Received",
            "phone": "Phone",
            "cust": "Customer",
            "call_type": "Type",
            "duration": "Length",
            "agent_name": "Agent",
            "reason": "Reason",
            "recording_url": "Recording",
        }
    )[["Received", "Phone", "Customer", "Type", "Length", "Agent", "Reason", "Recording"]]
    st.dataframe(
        style_status_columns(missed_display, ["Type"]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Recording": st.column_config.LinkColumn("Recording", display_text="listen"),
        },
        height=400,
    )

    csv = missed[
        ["id", "created_on", "from_phone", "customer_name", "call_type",
         "duration_seconds", "agent_name", "reason", "recording_url"]
    ].to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, file_name="missed_calls.csv", mime="text/csv")


# ──────────────── 🤖 Intent analytics ─────────────────────────────
# Each scored call's transcript runs through Claude nightly and gets
# tagged with one of 12 intent buckets. Lets us answer:
#   - what does our inbound mix actually look like?
#   - which intents convert to paid jobs at the highest rate?
#   - are emergencies clustering at certain times of day?
st.divider()
st.subheader("🤖 Intent breakdown")

intent_df = inbound.dropna(subset=["intent"]).copy()
if intent_df.empty:
    st.info(
        "No classified intents in this date range yet. Run "
        "`python3 scripts/classify_call_intents.py` or wait for the nightly cron."
    )
else:
    # Intent filter chip row
    available_intents = sorted(intent_df["intent"].dropna().unique().tolist())
    filt = st.multiselect(
        "Filter calls by intent",
        options=available_intents,
        format_func=lambda i: f"{INTENT_DISPLAY.get(i, ('','',''))[0]} "
                              f"{INTENT_DISPLAY.get(i, ('','',i))[2]}",
        default=[],
        help="Pick one or more intents to filter the breakdown below.",
    )
    if filt:
        intent_df = intent_df[intent_df["intent"].isin(filt)]

    # Distribution chart
    dist = (intent_df["intent"].value_counts().rename_axis("intent")
            .reset_index(name="calls"))
    dist["label"] = dist["intent"].apply(
        lambda i: f"{INTENT_DISPLAY.get(i, ('','',''))[0]} "
                  f"{INTENT_DISPLAY.get(i, ('','',i))[2]}"
    )
    dist["color"] = dist["intent"].apply(
        lambda i: INTENT_DISPLAY.get(i, ('','#6B7280',''))[1]
    )
    fig_dist = px.bar(
        dist, x="calls", y="label", orientation="h",
        color="label",
        color_discrete_map={r["label"]: r["color"] for _, r in dist.iterrows()},
    )
    fig_dist.update_layout(
        showlegend=False, height=chart_height("compact"),
        yaxis_title="", xaxis_title="Calls",
        yaxis={'categoryorder': 'total ascending'},
    )
    st.plotly_chart(fig_dist, use_container_width=True)

    # Conversion by intent — does this intent become a paid invoice?
    with db() as _cconn, _cconn.cursor() as _ccur:
        _ccur.execute(
            """
            WITH classified_calls AS (
              SELECT c.id, c.customer_id, c.received_on, cs.intent
              FROM calls c
              JOIN call_scores cs ON cs.call_id = c.id
              WHERE c.direction = 'Inbound'
                AND cs.intent IS NOT NULL
                AND c.created_on::date BETWEEN %s AND %s
            )
            SELECT
              cc.intent,
              COUNT(*) AS calls,
              COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM invoices i
                WHERE i.customer_id = cc.customer_id
                  AND i.total > 0
                  AND i.invoice_date >  (cc.received_on AT TIME ZONE 'UTC')::date
                  AND i.invoice_date <= (cc.received_on AT TIME ZONE 'UTC')::date + 30
              )) AS converted_30d,
              COALESCE(SUM((
                SELECT SUM(i.total) FROM invoices i
                WHERE i.customer_id = cc.customer_id AND i.total > 0
                  AND i.invoice_date >  (cc.received_on AT TIME ZONE 'UTC')::date
                  AND i.invoice_date <= (cc.received_on AT TIME ZONE 'UTC')::date + 30
              )), 0)::int AS revenue_30d
            FROM classified_calls cc
            GROUP BY cc.intent ORDER BY revenue_30d DESC
            """, (start, end),
        )
        conv_rows = [dict(r) for r in _ccur.fetchall()]

    if conv_rows:
        st.markdown("**Conversion rate by intent** (call → paid invoice within 30 days)")
        conv_df = pd.DataFrame(conv_rows)
        conv_df["rate"] = (conv_df["converted_30d"] / conv_df["calls"] * 100).round(1)
        conv_df["avg_ticket"] = (
            conv_df["revenue_30d"] / conv_df["converted_30d"].replace(0, float("nan"))
        ).round(0)
        conv_df["label"] = conv_df["intent"].apply(
            lambda i: f"{INTENT_DISPLAY.get(i, ('','',''))[0]} "
                      f"{INTENT_DISPLAY.get(i, ('','',i))[2]}"
        )
        display_conv = conv_df.assign(
            calls=conv_df["calls"].astype(int),
            converted=conv_df["converted_30d"].astype(int),
            rate=conv_df["rate"].map(lambda v: f"{v:.0f}%"),
            revenue=conv_df["revenue_30d"].map(lambda v: f"${v:,.0f}"),
            avg=conv_df["avg_ticket"].map(
                lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
            ),
        ).rename(columns={
            "label": "Intent",
            "calls": "Calls",
            "converted": "Paid (30d)",
            "rate": "Conv. rate",
            "revenue": "Revenue (30d)",
            "avg": "Avg ticket",
        })[["Intent", "Calls", "Paid (30d)", "Conv. rate", "Revenue (30d)", "Avg ticket"]]
        st.dataframe(display_conv, use_container_width=True, hide_index=True)
        st.caption(
            "_'Conv. rate' is the % of inbound calls in this intent that ended up "
            "with the customer paying us within 30 days. **High-revenue, low-conv-rate** "
            "intents are where Fey could earn the most by improving the script._"
        )

    # Hour-of-day pattern for emergencies (special interest)
    emerg = intent_df[intent_df["intent"] == "emergency"].copy()
    if len(emerg) >= 3:
        st.markdown("**🚨 Emergency calls by hour of day** (Chicago time)")
        emerg["hour"] = pd.to_datetime(emerg["received_on"]).dt.tz_convert(
            "America/Chicago"
        ).dt.hour
        hr_counts = emerg["hour"].value_counts().sort_index()
        st.bar_chart(hr_counts, height=200)


# ──────────────── ⚠️ Possibly misclassified calls ─────────────────
# Calls a CSR tagged Excused/NotLead — removing them from every lead
# metric — but whose TRANSCRIPT classifies as a lead-like intent.
# Each one is a potentially discarded lead. Excused calls get intents
# from the nightly coaching pipeline; NotLead calls are covered by
# scripts/audit_call_classification.py.
st.divider()
st.subheader("⚠️ Possibly misclassified calls")
st.caption(
    "Tagged **Excused/NotLead** in ServiceTitan (excluded from lead metrics) "
    "but the transcript reads like a real lead. Listen before re-engaging — "
    "the classifier errs toward flagging."
)

LEAD_LIKE = ("schedule_new", "emergency", "accept_quote", "reschedule")

@st.cache_data(ttl=300, show_spinner=False)
def load_misclassified(s: date, e: date) -> pd.DataFrame:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.received_on, c.call_type, c.customer_name,
                   c.from_phone, c.duration_seconds, c.recording_url,
                   cs.intent, LEFT(cs.transcript, 160) AS excerpt,
                   (c.customer_id IS NOT NULL AND EXISTS (
                      SELECT 1 FROM invoices i
                      WHERE i.customer_id = c.customer_id AND i.total > 0
                        AND i.invoice_date > (c.received_on AT TIME ZONE 'UTC')::date
                   )) AS later_paid
            FROM calls c
            JOIN call_scores cs ON cs.call_id = c.id
            WHERE c.direction = 'Inbound'
              AND c.call_type IN ('Excused', 'NotLead')
              AND cs.intent IN %s
              AND c.created_on::date BETWEEN %s AND %s
            ORDER BY c.received_on DESC
            """,
            (LEAD_LIKE, s, e),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])

mis = load_misclassified(start, end)
if mis.empty:
    st.caption("None found in this date range. ✅")
else:
    hide_recovered = st.checkbox(
        "Hide callers who later paid us anyway",
        value=True,
        help="A later paid invoice means the lead was recovered through "
             "another channel — the tag was still wrong, but no money lost.",
    )
    view = mis[~mis["later_paid"]] if hide_recovered else mis
    st.caption(f"**{len(view)} flagged call(s)** "
               f"({int(mis['later_paid'].sum())} of {len(mis)} later paid).")

    view = view.copy()
    view["When"] = pd.to_datetime(view["received_on"]).dt.tz_convert(
        "America/Chicago").dt.strftime("%m-%d %H:%M")
    view["Len"] = view["duration_seconds"].fillna(0).astype(int).map(lambda v: f"{v}s")
    view["Intent"] = view["intent"].map(
        lambda i: f"{INTENT_DISPLAY.get(i, ('','',''))[0]} "
                  f"{INTENT_DISPLAY.get(i, ('','',i))[2]}"
    )
    view["Recovered"] = view["later_paid"].map(lambda v: "✓ paid later" if v else "✗ never paid")
    view = view.rename(columns={
        "call_type": "ST tag", "customer_name": "Customer",
        "from_phone": "Phone", "excerpt": "Transcript start",
        "recording_url": "Recording",
    })[["When", "ST tag", "Intent", "Customer", "Phone", "Len",
        "Recovered", "Transcript start", "Recording"]]
    st.dataframe(
        view, use_container_width=True, hide_index=True, height=420,
        column_config={
            "Recording": st.column_config.LinkColumn("Recording", display_text="listen"),
        },
    )
