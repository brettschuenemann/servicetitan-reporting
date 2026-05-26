"""Shared outcome catalog + helpers for the CSR call workflow.

Used by both pages/12_Outcomes.py (the email-link handler) and
pages/13_Call_List.py (Fey's interactive workspace). Centralizes the
outcome definitions, cooldown windows, and DB operations so both views
stay in sync.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# Outcome catalog. expires_days = None means permanent.
OUTCOME_CONFIG: dict[str, dict[str, dict]] = {
    "membership": {
        "enrolled":     {"label": "Enrolled",     "expires_days": None, "color": "#10B981"},
        "declined":     {"label": "Declined",     "expires_days": 180,  "color": "#EF4444"},
        # No answer / VM has a short cooldown — she didn't actually reach
        # them, so try again in a few days. Matches the auto-detected
        # short-cooldown window for membership (SUPPRESS_DAYS_SHORT).
        "voicemail":    {"label": "No answer / VM", "expires_days": 4,  "color": "#F59E0B"},
        # "Try later" tightened from 60d → 30d: 60d let warm leads cool off
        # and missed seasonal momentum (HVAC interest swings fast). Sleeping
        # customers keep the longer 60d because that relationship is calmer.
        "try_later":    {"label": "Try later",    "expires_days": 30,   "color": "#F59E0B"},
        "wrong_number": {"label": "Wrong number", "expires_days": None, "color": "#6B7280"},
    },
    "sleeping": {
        "reactivated":  {"label": "Reactivated",  "expires_days": None, "color": "#10B981"},
        "declined":     {"label": "Declined",     "expires_days": 180,  "color": "#EF4444"},
        # Same idea as membership — short cooldown for missed connections.
        # 10 days matches the auto-detected SUPPRESS_DAYS_SHORT for sleeping.
        "voicemail":    {"label": "No answer / VM", "expires_days": 10, "color": "#F59E0B"},
        "try_later":    {"label": "Try later",    "expires_days": 60,   "color": "#F59E0B"},
        "wrong_number": {"label": "Wrong number", "expires_days": None, "color": "#6B7280"},
    },
    "missed": {
        "followed_up":  {"label": "Followed up",  "expires_days": None, "color": "#10B981"},
        "voicemail":    {"label": "Voicemail",    "expires_days": 7,    "color": "#F59E0B"},
        "try_later":    {"label": "Try later",    "expires_days": 7,    "color": "#F59E0B"},
        "wrong_number": {"label": "Wrong number", "expires_days": None, "color": "#6B7280"},
    },
    # Aging open estimates (>30 days). Jake handles the fresh ones; Fey
    # works the stale tail. A customer can have multiple open estimates,
    # so dedup is per-estimate (estimate_id passed via the call_id slot).
    "estimate": {
        "sold":         {"label": "Sold",           "expires_days": None, "color": "#10B981"},
        "working":      {"label": "Working it",     "expires_days": 7,    "color": "#0066EE"},
        "declined":     {"label": "Declined",       "expires_days": 180,  "color": "#EF4444"},
        "voicemail":    {"label": "No answer / VM", "expires_days": 4,    "color": "#F59E0B"},
        "try_later":    {"label": "Try later",      "expires_days": 14,   "color": "#F59E0B"},
        "wrong_number": {"label": "Wrong number",   "expires_days": None, "color": "#6B7280"},
    },
}


def dedup_key(kind: str, customer_id: int | None, call_id: int | None = None) -> str:
    """Stable key for de-duping recommendations / outcomes across days.

    Must match the implementation in scripts/send_csr_daily_email.py.
    The `call_id` parameter doubles as a generic "secondary id":
      - For missed calls it's the call_id (multiple missed calls from
        the same customer count separately).
      - For estimates it's the estimate_id (a customer can have several
        open quotes, each tracked independently).
    """
    if kind == "missed" and call_id is not None:
        return f"{kind}:call:{call_id}"
    if kind == "estimate" and call_id is not None:
        return f"{kind}:est:{call_id}"
    return f"{kind}:cust:{customer_id}"


def record_outcome(
    conn,
    kind: str,
    customer_id: int | None,
    call_id: int | None,
    outcome: str,
    notes: str | None = None,
) -> tuple[int, dict]:
    """Insert an outcome row. Returns (new_id, config_dict)."""
    cfg = OUTCOME_CONFIG[kind][outcome]
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=cfg["expires_days"])
        if cfg["expires_days"] else None
    )
    key = dedup_key(kind, customer_id, call_id)
    with conn.cursor() as cur:
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


def undo_outcome(conn, outcome_id: int) -> None:
    """Soft-undo via a sentinel 'undone' row that supersedes the prior one."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, customer_id, call_id, dedup_key "
            "FROM csr_customer_outcomes WHERE id = %s",
            (outcome_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        cur.execute(
            """
            INSERT INTO csr_customer_outcomes
              (kind, customer_id, call_id, dedup_key, outcome, expires_at, notes)
            VALUES (%s, %s, %s, %s, 'undone', NULL, %s)
            """,
            (row["kind"], row["customer_id"], row["call_id"], row["dedup_key"],
             f"Undid outcome {outcome_id}"),
        )
    conn.commit()


def load_todays_outcomes(conn) -> list[dict]:
    """Outcomes recorded today (Chicago time), newest first. For the
    'completed today' rollup on the call list page.

    Excludes both the 'undone' sentinel rows AND any outcomes that have
    been superseded by a later 'undone' row — so when Fey clicks Undo,
    the customer fully disappears from this list and the KPI count
    drops to match.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH names AS (
              SELECT customer_id, MIN(customer_name) AS name FROM invoices
              WHERE customer_name IS NOT NULL GROUP BY customer_id
            )
            SELECT o.id, o.recorded_at, o.kind, o.outcome, o.customer_id,
                   o.call_id, o.notes,
                   COALESCE(n.name, 'Customer ' || o.customer_id::text) AS customer
            FROM csr_customer_outcomes o
            LEFT JOIN names n ON n.customer_id = o.customer_id
            WHERE (o.recorded_at AT TIME ZONE 'America/Chicago')::date =
                  (NOW()         AT TIME ZONE 'America/Chicago')::date
              AND o.outcome <> 'undone'
              AND NOT EXISTS (
                SELECT 1 FROM csr_customer_outcomes u
                WHERE u.dedup_key = o.dedup_key
                  AND u.recorded_at > o.recorded_at
                  AND u.outcome = 'undone'
              )
            ORDER BY o.recorded_at DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


def save_note(conn, customer_id: int, note: str) -> int:
    """Append a note for this customer. Returns the new row id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO csr_customer_notes (customer_id, note) "
            "VALUES (%s, %s) RETURNING id",
            (customer_id, note.strip()),
        )
        new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


def load_notes(conn, customer_id: int, limit: int = 5) -> list[dict]:
    """Recent notes for a customer, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, note FROM csr_customer_notes "
            "WHERE customer_id = %s ORDER BY created_at DESC LIMIT %s",
            (customer_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def load_notes_bulk(conn, customer_ids: list[int]) -> dict[int, list[dict]]:
    """Get recent notes for many customers in one round trip."""
    if not customer_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, customer_id, created_at, note FROM csr_customer_notes "
            "WHERE customer_id = ANY(%s) ORDER BY created_at DESC",
            (customer_ids,),
        )
        out: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            out.setdefault(int(r["customer_id"]), []).append(dict(r))
    return out
