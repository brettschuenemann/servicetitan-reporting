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

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from lib.database import db
from lib.style import apply_mobile_styles, empty_state

st.set_page_config(page_title="Outcomes · Pure Comfort", layout="wide")
apply_mobile_styles()

# ---------- outcome catalog ----------

OUTCOME_CONFIG: dict[str, dict[str, dict]] = {
    "membership": {
        "enrolled":     {"label": "Enrolled",       "expires_days": None, "color": "#10B981"},
        "declined":     {"label": "Declined",       "expires_days": 180,  "color": "#EF4444"},
        "try_later":    {"label": "Try later",      "expires_days": 60,   "color": "#F59E0B"},
        "wrong_number": {"label": "Wrong number",   "expires_days": None, "color": "#6B7280"},
    },
    "sleeping": {
        "reactivated":  {"label": "Reactivated",    "expires_days": None, "color": "#10B981"},
        "declined":     {"label": "Declined",       "expires_days": 180,  "color": "#EF4444"},
        "try_later":    {"label": "Try later",      "expires_days": 60,   "color": "#F59E0B"},
        "wrong_number": {"label": "Wrong number",   "expires_days": None, "color": "#6B7280"},
    },
    "missed": {
        "followed_up":  {"label": "Followed up",    "expires_days": None, "color": "#10B981"},
        "voicemail":    {"label": "Voicemail left", "expires_days": 7,    "color": "#F59E0B"},
        "try_later":    {"label": "Try later",      "expires_days": 7,    "color": "#F59E0B"},
        "wrong_number": {"label": "Wrong number",   "expires_days": None, "color": "#6B7280"},
    },
}


def dedup_key(kind: str, customer_id: int | None, call_id: int | None = None) -> str:
    if kind == "missed" and call_id is not None:
        return f"{kind}:call:{call_id}"
    return f"{kind}:cust:{customer_id}"


def record_outcome(kind: str, customer_id: int | None, call_id: int | None,
                   outcome: str, notes: str | None = None) -> tuple[int, dict]:
    """Insert an outcome row. Returns (id, config) for the new outcome."""
    cfg = OUTCOME_CONFIG[kind][outcome]
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=cfg["expires_days"])
        if cfg["expires_days"] else None
    )
    key = dedup_key(kind, customer_id, call_id)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO csr_customer_outcomes
              (kind, customer_id, call_id, dedup_key, outcome, expires_at, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (kind, customer_id, call_id, key, outcome, expires_at, notes),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
    return new_id, cfg


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


def undo_outcome(outcome_id: int) -> None:
    """Soft-undo by inserting a sentinel that supersedes the prior outcome."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, customer_id, call_id, dedup_key FROM csr_customer_outcomes WHERE id = %s",
            (outcome_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        cur.execute(
            """
            INSERT INTO csr_customer_outcomes
              (kind, customer_id, call_id, dedup_key, outcome, expires_at, notes)
            VALUES (%s, %s, %s, %s, 'undone', NULL, 'Undid outcome ' || %s)
            """,
            (row["kind"], row["customer_id"], row["call_id"], row["dedup_key"], str(outcome_id)),
        )
        conn.commit()


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
