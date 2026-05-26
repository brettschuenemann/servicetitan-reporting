"""Outcome handler — endpoint for the action links in Fey's daily email.

Links from the email look like:
  https://app/Outcomes?cust=1003&kind=membership&outcome=declined

This page reads the query params, records the outcome to
`csr_customer_outcomes`, and shows a confirmation. The morning email's
source/suppression queries respect the most recent active outcome per
customer (per dedup_key, really — so missed calls work per call_id too).

If no params are present (Fey navigated here directly), the page shows
a recent-outcomes table so she can audit / undo.

⚠️ Auth note: this page intentionally does NOT require a password so Fey
can tap action links from email on her phone without logging in. Inputs
are validated against an allow-list (kind + outcome) and the worst-case
impact of a polluted outcome is one customer disappearing from the list
for up to 180 days — recoverable by deleting rows from the audit table.
If you want a stricter posture, add HMAC-signed tokens to the URLs.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib.csr_outcomes import (
    OUTCOME_CONFIG,
    record_outcome as _record_outcome,
    undo_outcome as _undo_outcome,
)
from lib.database import db
from lib.style import apply_mobile_styles, empty_state

st.set_page_config(page_title="Outcomes · Pure Comfort", layout="wide")
apply_mobile_styles()


def record_outcome(kind, customer_id, call_id, outcome, notes=None):
    """Wrapper that opens its own DB connection."""
    with db() as conn:
        return _record_outcome(conn, kind, customer_id, call_id, outcome, notes)


def undo_outcome(outcome_id: int) -> None:
    with db() as conn:
        _undo_outcome(conn, outcome_id)


def lookup_customer_name(customer_id: int | None) -> str:
    if not customer_id:
        return "(unmatched customer)"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(customer_name) AS name FROM invoices WHERE customer_id = %s",
            (customer_id,),
        )
        row = cur.fetchone()
    return (row and row["name"]) or f"Customer {customer_id}"


def load_recent_outcomes(limit: int = 30) -> pd.DataFrame:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH names AS (
              SELECT customer_id, MIN(customer_name) AS name FROM invoices
              WHERE customer_name IS NOT NULL GROUP BY customer_id
            )
            SELECT o.id, o.recorded_at, o.kind, o.outcome, o.customer_id,
                   o.call_id, o.expires_at, o.notes,
                   COALESCE(n.name, 'Customer ' || o.customer_id::text) AS customer
            FROM csr_customer_outcomes o
            LEFT JOIN names n ON n.customer_id = o.customer_id
            ORDER BY o.recorded_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


# ---------- main page ----------

params = st.query_params

# Action mode: query params present → record + show confirmation
if params.get("kind") and params.get("outcome"):
    kind = params.get("kind")
    outcome = params.get("outcome")
    try:
        customer_id = int(params.get("cust") or 0) or None
    except ValueError:
        customer_id = None
    try:
        call_id = int(params.get("call") or 0) or None
    except ValueError:
        call_id = None

    if kind not in OUTCOME_CONFIG or outcome not in OUTCOME_CONFIG[kind]:
        st.error(f"Unknown outcome `{outcome}` for kind `{kind}`.")
        st.stop()

    if customer_id is None and call_id is None:
        st.error("Missing customer or call ID in the link.")
        st.stop()

    name = lookup_customer_name(customer_id)

    st.title("Outcome recorded ✓")
    new_id, cfg = record_outcome(kind, customer_id, call_id, outcome)
    expires_label = (
        f"Suppresses this customer in the {kind} section for {cfg['expires_days']} days."
        if cfg["expires_days"] else f"This customer is permanently removed from the {kind} section."
    )
    st.success(
        f"**{name}** → marked as **{cfg['label']}** in the {kind} section.\n\n{expires_label}"
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("↩️ Undo", use_container_width=True):
            undo_outcome(new_id)
            st.info("Undone. The previous state is restored.")
            st.rerun()
    with col2:
        st.caption("Tip: bookmark the dashboard. The morning email always links straight back here.")

    st.divider()
    st.subheader("Recent outcomes (last 30)")
    recent = load_recent_outcomes(30)
else:
    st.title("Call outcomes")
    st.caption(
        "Log what happened after a call. Suppresses the customer from the morning email "
        "for the appropriate cooldown (e.g. 'Declined' → 6 months, 'Try later' → 60 days). "
        "Usually you'd land here via the action links at the bottom of each row in the email."
    )
    st.subheader("Recent outcomes (last 30)")
    recent = load_recent_outcomes(30)

if recent.empty:
    empty_state("No outcomes recorded yet — the action links in the morning email feed this list.")
else:
    display = recent.assign(
        recorded=lambda d: pd.to_datetime(d["recorded_at"]).dt.strftime("%Y-%m-%d %H:%M"),
        expires=lambda d: pd.to_datetime(d["expires_at"]).dt.strftime("%Y-%m-%d").fillna("forever"),
    ).rename(columns={
        "customer": "Customer",
        "kind": "Section",
        "outcome": "Outcome",
        "recorded": "Recorded",
        "expires": "Expires",
    })[["Customer", "Section", "Outcome", "Recorded", "Expires"]]
    st.dataframe(display, use_container_width=True, hide_index=True, height=420)
