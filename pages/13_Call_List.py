"""Call List — Fey's interactive daily workspace.

Replaces the static morning email as the primary call-list interface.
Live data, inline action buttons, search/filter, per-row notes, and a
counter strip showing what's done today.

The morning notification email now just links here. Brett can open the
same page to see real-time status without waiting for the EOD report.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from html import escape

import streamlit as st

# Allow `from scripts.send_csr_daily_email import ...` from within /pages
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.auth import require_csr_password
from lib.call_openers import _first_names, generate_openers
from lib.csr_outcomes import (
    OUTCOME_CONFIG,
    dedup_key,
    load_notes_bulk,
    load_todays_outcomes,
    record_outcome,
    save_note,
    undo_outcome,
)
from lib.database import db
from lib.loaders import get_client
from lib.style import apply_mobile_styles, empty_state
from scripts.send_csr_daily_email import (
    CALL_SCRIPTS,
    SECTION_CAPS,
    days_ago,
    fmt_money,
    fmt_phone,
    load_membership_opps,
    load_missed_calls,
    load_open_estimates,
    load_recommendation_state,
    load_sleeping_customers,
    lookup_contact,
    short,
    tel_href,
    to_central,
)

st.set_page_config(page_title="Call List · Pure Comfort", layout="wide")
apply_mobile_styles()
require_csr_password()


# ---------- session state defaults ----------

st.session_state.setdefault("call_list_filter", "All")
st.session_state.setdefault("call_list_search", "")


# ---------- data loading (cached for speed; cleared on action) ----------

@st.cache_data(ttl=300, show_spinner="Loading call list…")
def _load_sections() -> dict:
    """Pull all four sections + recommendation/outreach state + hot leads."""
    with db() as conn:
        state = load_recommendation_state(conn)
        memberships_all = load_membership_opps(conn)
        sleeping_all = load_sleeping_customers(conn, limit=SECTION_CAPS["sleeping"] * 4)
        missed_all = load_missed_calls(conn)
        # 30d+ aging estimates — Jake handles the fresh ones, these are Fey's.
        # Pull 4× the cap so suppressed/in-cooldown ones still leave a healthy
        # bench when she clears the visible list.
        estimates_all = load_open_estimates(conn, min_age_days=30)
        hot_leads = _load_hot_leads(conn)
    return {
        "state": state,
        "memberships_all": memberships_all,
        "sleeping_all": sleeping_all,
        "missed_all": missed_all,
        "estimates_all": estimates_all,
        "hot_leads": hot_leads,
    }


def _load_hot_leads(conn) -> list[dict]:
    """Customers Fey should call FIRST, regardless of which bucket they're in.

    A lead is "hot" if it matches ANY of these deterministic signals
    (highest-priority reason wins on dedupe):

      1. CALLBACK — they called us back after we tried to reach them.
         The gold-standard signal: customer-initiated re-contact.
      2. WARM QUOTE — open estimate ≥$2k AND inbound call in last 14d.
         They're actively engaging on a real-money decision.
      3. CAPTURED UNCLOSED — paid a diagnostic fee ($50-$300) in last
         30d but no follow-up job since. We earned their trust and
         have a foot in the door; need to close the recommended work.
      4. DECISION DEADLINE — open estimate ≥$5k aged 7-21 days. Sweet
         spot before quotes go cold; >21d decay sharply.
      5. HIGH-LTV NEW SIGNAL — $5k+ lifetime customer with ANY new
         activity in last 14 days (inbound, new estimate, new job).
         Loyal-customer reappearance — biggest relationship to protect.

    Excludes customers with a permanent suppressing outcome
    (sold/declined/wrong#) on file.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            -- Customers we should never re-surface even on a hot signal:
            -- they have a fresh permanent outcome that resolves the lead.
            WITH permanent_suppress AS (
              SELECT DISTINCT customer_id FROM csr_customer_outcomes
              WHERE outcome IN ('sold','enrolled','declined','wrong_number',
                                'reactivated','followed_up')
                AND (expires_at IS NULL OR expires_at > NOW())
                AND recorded_at >= NOW() - INTERVAL '180 day'
                AND customer_id IS NOT NULL
            ),
            -- Customers with an active future-looking job. If a customer
            -- has a scheduled / in-progress / dispatched job created in
            -- the last 30 days, they're in motion — the lead has converted
            -- (or is on its way) and Fey shouldn't be chasing them.
            already_scheduled AS (
              SELECT DISTINCT customer_id FROM jobs
              WHERE customer_id IS NOT NULL
                AND job_status IN ('Scheduled', 'Dispatched',
                                    'InProgress', 'Working', 'Hold')
                AND completed_on IS NULL
                AND created_on >= NOW() - INTERVAL '30 day'
            ),
            -- Customer name cache for display
            cust_name AS (
              SELECT customer_id, MIN(customer_name) AS name
              FROM invoices
              WHERE customer_name IS NOT NULL AND customer_id IS NOT NULL
              GROUP BY customer_id
            ),
            history AS (
              SELECT customer_id,
                     COUNT(*) AS lifetime_invoices,
                     SUM(total) AS lifetime_revenue,
                     MAX(invoice_date) AS last_visit
              FROM invoices
              WHERE customer_id IS NOT NULL AND total > 0
              GROUP BY customer_id
            ),

            -- ── Signal 1: CALLBACK ───────────────────────────────────
            -- Customer called us back after our outreach. We use the
            -- same logic the existing outreach detector uses:
            -- a customer in csr_recommendations who later made an
            -- inbound call after the reference point.
            signal_callback AS (
              SELECT DISTINCT
                cr.customer_id,
                'CALLBACK' AS reason,
                1 AS priority,
                MAX(c.received_on) AS signal_at,
                'Called us back after our outreach' AS detail
              FROM csr_recommendations cr
              JOIN calls c ON c.customer_id = cr.customer_id
              WHERE c.direction = 'Inbound'
                AND c.received_on >= cr.sent_at
                AND cr.sent_at >= NOW() - INTERVAL '60 day'
                AND cr.customer_id IS NOT NULL
              GROUP BY cr.customer_id
              HAVING MAX(c.received_on) >= NOW() - INTERVAL '7 day'
            ),

            -- ── Signal 2: WARM QUOTE ─────────────────────────────────
            -- Open estimate >= $2k AND customer has an inbound call in
            -- the last 14 days. Active engagement on real money.
            signal_warm_quote AS (
              SELECT DISTINCT
                e.customer_id,
                'WARM_QUOTE' AS reason,
                2 AS priority,
                MAX(c.received_on) AS signal_at,
                'Open $' || ROUND(MAX(e.subtotal))::text
                  || ' quote + recent inbound call' AS detail
              FROM estimates e
              JOIN calls c ON c.customer_id = e.customer_id
              WHERE e.status_name = 'Open' AND e.active = TRUE
                AND e.subtotal >= 2000
                AND e.customer_id IS NOT NULL
                AND c.direction = 'Inbound'
                AND c.received_on >= NOW() - INTERVAL '14 day'
              GROUP BY e.customer_id
            ),

            -- ── Signal 3: CAPTURED UNCLOSED ──────────────────────────
            -- They paid a small diagnostic/service fee in the last 30
            -- days, but no follow-up job has been created since. We
            -- have a foot in the door — close the recommended work.
            signal_captured AS (
              SELECT DISTINCT
                i.customer_id,
                'CAPTURED_UNCLOSED' AS reason,
                3 AS priority,
                MAX(i.invoice_date)::timestamptz AS signal_at,
                'Paid $' || ROUND(MAX(i.total))::text
                  || ' diagnostic on ' || TO_CHAR(MAX(i.invoice_date), 'Mon DD')
                  || ' — no follow-up booked' AS detail
              FROM invoices i
              WHERE i.customer_id IS NOT NULL
                AND i.total BETWEEN 50 AND 300
                AND i.invoice_date >= CURRENT_DATE - INTERVAL '30 day'
                AND NOT EXISTS (
                  SELECT 1 FROM jobs j
                  WHERE j.customer_id = i.customer_id
                    AND j.created_on > i.invoice_date::timestamp
                    AND j.no_charge = FALSE
                )
              GROUP BY i.customer_id
            ),

            -- ── Signal 4: DECISION DEADLINE ──────────────────────────
            -- Open estimate >= $5k aged 7-21 days. Past the "fresh"
            -- window where customers are still researching; before
            -- the "dead" window where they've moved on.
            signal_deadline AS (
              SELECT DISTINCT
                e.customer_id,
                'DECISION_DEADLINE' AS reason,
                4 AS priority,
                e.created_on AS signal_at,
                '$' || ROUND(e.subtotal)::text || ' quote — day '
                  || EXTRACT(DAY FROM NOW() - e.created_on)::int::text
                  || ' of decision window' AS detail
              FROM estimates e
              WHERE e.status_name = 'Open' AND e.active = TRUE
                AND e.subtotal >= 5000
                AND e.customer_id IS NOT NULL
                AND e.created_on BETWEEN NOW() - INTERVAL '21 day'
                                     AND NOW() - INTERVAL '7 day'
            ),

            -- ── Signal 5: HIGH-LTV NEW SIGNAL ────────────────────────
            -- Customer with $5k+ lifetime revenue who showed ANY new
            -- activity in last 14 days. Inbound call, new estimate,
            -- or new job (paid or unpaid).
            signal_high_ltv AS (
              SELECT DISTINCT
                h.customer_id,
                'HIGH_LTV_SIGNAL' AS reason,
                5 AS priority,
                GREATEST(
                  COALESCE((SELECT MAX(received_on) FROM calls
                            WHERE customer_id = h.customer_id
                              AND direction = 'Inbound'
                              AND received_on >= NOW() - INTERVAL '14 day'),
                           '1970-01-01'::timestamptz),
                  COALESCE((SELECT MAX(created_on) FROM estimates
                            WHERE customer_id = h.customer_id
                              AND created_on >= NOW() - INTERVAL '14 day'),
                           '1970-01-01'::timestamptz),
                  COALESCE((SELECT MAX(created_on) FROM jobs
                            WHERE customer_id = h.customer_id
                              AND created_on >= NOW() - INTERVAL '14 day'),
                           '1970-01-01'::timestamptz)
                ) AS signal_at,
                'Loyal $' || ROUND((h.lifetime_revenue/1000.0)::numeric, 1)::text
                  || 'k customer with new activity' AS detail
              FROM history h
              WHERE h.lifetime_revenue >= 5000
                AND (
                  EXISTS (SELECT 1 FROM calls
                          WHERE customer_id = h.customer_id
                            AND direction = 'Inbound'
                            AND received_on >= NOW() - INTERVAL '14 day')
                  OR EXISTS (SELECT 1 FROM estimates
                             WHERE customer_id = h.customer_id
                               AND created_on >= NOW() - INTERVAL '14 day')
                  OR EXISTS (SELECT 1 FROM jobs
                             WHERE customer_id = h.customer_id
                               AND created_on >= NOW() - INTERVAL '14 day')
                )
            ),

            -- Union all signals; dedupe by customer_id, picking the
            -- highest-priority reason (lowest priority number wins).
            all_signals AS (
              SELECT * FROM signal_callback
              UNION ALL SELECT * FROM signal_warm_quote
              UNION ALL SELECT * FROM signal_captured
              UNION ALL SELECT * FROM signal_deadline
              UNION ALL SELECT * FROM signal_high_ltv
            ),
            ranked AS (
              SELECT DISTINCT ON (customer_id)
                customer_id, reason, priority, signal_at, detail
              FROM all_signals
              ORDER BY customer_id, priority
            )

            SELECT
              r.customer_id, r.reason, r.priority, r.detail, r.signal_at,
              COALESCE(cn.name, 'Customer ' || r.customer_id::text) AS customer_name,
              COALESCE(h.lifetime_invoices, 0) AS lifetime_invoices,
              COALESCE(h.lifetime_revenue, 0)  AS lifetime_revenue,
              h.last_visit
            FROM ranked r
            LEFT JOIN cust_name cn ON cn.customer_id = r.customer_id
            LEFT JOIN history h    ON h.customer_id = r.customer_id
            LEFT JOIN permanent_suppress ps ON ps.customer_id = r.customer_id
            LEFT JOIN already_scheduled sched ON sched.customer_id = r.customer_id
            WHERE ps.customer_id IS NULL
              AND sched.customer_id IS NULL
            ORDER BY r.priority, r.signal_at DESC
            LIMIT 12
            """
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- contact + opener caches now live in Postgres ----------
# Rationale: a Streamlit Cloud container restart used to wipe the
# in-memory cache, triggering 40 ServiceTitan API calls + ~30 Claude
# calls on the next page load (10-15s cold load). Persisting both to
# Postgres means restarts cost ~0; only genuinely new customers /
# new estimates ever trigger an upstream call.

def _bulk_lookup_contacts(customer_ids: list[int],
                          max_workers: int = 12) -> dict[int, tuple[str, str]]:
    """Resolve phone/email per customer, DB-first.

    1. Read everything we have from `customer_contacts`
    2. For misses, parallel-fetch from ServiceTitan
    3. Persist new fetches back to `customer_contacts` so the next page
       load (and any other process) skips the API entirely
    """
    unique_ids = list({int(c) for c in customer_ids if c})
    if not unique_ids:
        return {}

    # 1. Cached rows from Postgres
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT customer_id, phone, email FROM customer_contacts "
            "WHERE customer_id = ANY(%s)",
            (unique_ids,),
        )
        result: dict[int, tuple[str, str]] = {
            int(r["customer_id"]): (r["phone"] or "", r["email"] or "")
            for r in cur.fetchall()
        }

    # 2. Parallel fetch the misses (if any)
    missing = [c for c in unique_ids if c not in result]
    if not missing:
        return result

    client = get_client()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fetched = dict(zip(missing,
                           ex.map(lambda c: lookup_contact(client, c), missing)))

    # 3. Persist back so future loads (and other processes) read from DB
    if fetched:
        from psycopg2.extras import execute_values
        with db() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO customer_contacts (customer_id, phone, email) "
                "VALUES %s "
                "ON CONFLICT (customer_id) DO UPDATE SET "
                "  phone = EXCLUDED.phone, "
                "  email = EXCLUDED.email, "
                "  fetched_at = NOW()",
                [(cid, p, e) for cid, (p, e) in fetched.items()],
            )
            conn.commit()
        result.update(fetched)

    return result


def _opener_keys_for(customer_inputs: list[dict]) -> list[tuple[str, int, int] | None]:
    """Composite (kind, customer_id, secondary_id) for each input.

    secondary_id is the estimate_id for estimate rows (so a customer
    with two open quotes gets two distinct openers); 0 for everyone
    else. Returns None for entries without a customer_id.
    """
    keys: list[tuple[str, int, int] | None] = []
    for c in customer_inputs:
        cid = c.get("customer_id")
        if not cid:
            keys.append(None)
            continue
        kind = c.get("kind", "")
        sec = c.get("estimate_id") or 0 if kind == "estimate" else 0
        keys.append((kind, int(cid), int(sec)))
    return keys


def _get_openers_with_cache(customer_inputs: list[dict]) -> dict[int, str]:
    """Opener resolution, DB-first.

    Returns customer_id -> opener (keyed by cid for backward compat
    with the renderer). Reads existing openers from `csr_openers` so
    Streamlit Cloud restarts don't re-pay Claude. Only inputs without
    a stored row trigger a (batched) Claude call, and new openers get
    persisted immediately.
    """
    if not customer_inputs:
        return {}

    keys = _opener_keys_for(customer_inputs)
    needed = list({k for k in keys if k})
    if not needed:
        return {}

    # 1. Pull existing rows in a single round-trip
    with db() as conn, conn.cursor() as cur:
        placeholders = ",".join(["(%s,%s,%s)"] * len(needed))
        params = [v for t in needed for v in t]
        cur.execute(
            f"SELECT kind, customer_id, secondary_id, opener FROM csr_openers "
            f"WHERE (kind, customer_id, secondary_id) IN ({placeholders})",
            params,
        )
        db_openers: dict[tuple[str, int, int], str] = {
            (r["kind"], int(r["customer_id"]), int(r["secondary_id"])): r["opener"]
            for r in cur.fetchall()
        }

    # 2. Generate the misses (single batched Claude call amortizes the round-trip)
    to_generate = [c for c, k in zip(customer_inputs, keys)
                   if k and k not in db_openers]
    new_openers_by_cid: dict[int, str] = {}
    if to_generate:
        new_openers_by_cid = generate_openers(to_generate)

    # 3. Persist new openers (composite-key upsert)
    if new_openers_by_cid:
        # Dedupe by composite key — same as the sync pre-warmer.
        # Two missed-call rows for the same customer share ("missed", cid, 0);
        # ON CONFLICT raises CardinalityViolation if both are in one INSERT.
        rows: list[tuple] = []
        seen: set[tuple] = set()
        for c, k in zip(customer_inputs, keys):
            if not (k and k not in db_openers and k not in seen):
                continue
            cid = k[1]
            if cid in new_openers_by_cid:
                rows.append((k[0], k[1], k[2], new_openers_by_cid[cid]))
                seen.add(k)
        if rows:
            from psycopg2.extras import execute_values
            with db() as conn, conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO csr_openers "
                    "  (kind, customer_id, secondary_id, opener) VALUES %s "
                    "ON CONFLICT (kind, customer_id, secondary_id) DO UPDATE SET "
                    "  opener = EXCLUDED.opener, generated_at = NOW()",
                    rows,
                )
                conn.commit()

    # 4. Final map keyed by customer_id (renderer expects this)
    result: dict[int, str] = {}
    for c, k in zip(customer_inputs, keys):
        if not k:
            continue
        opener = db_openers.get(k) or new_openers_by_cid.get(k[1])
        if opener:
            result[k[1]] = opener
    return result


# ---------- action callbacks (clear caches + rerun handled by Streamlit) ----------
# Callbacks can't render — they must stash a "toast" in session_state and
# fire it on the next run. Cleaner UX than a silent action.

def _queue_toast(message: str, icon: str = "✅") -> None:
    st.session_state["pending_toast"] = (message, icon)


def _on_record_outcome(kind: str, customer_id: int | None,
                       call_id: int | None, outcome: str):
    """Write an outcome row, then clear data cache so the list refreshes."""
    try:
        with db() as conn:
            record_outcome(conn, kind, customer_id, call_id, outcome)
        _load_sections.clear()
        label = outcome.replace("_", " ").title()
        _queue_toast(f"Marked **{label}** — they'll drop from your list.")
    except Exception as exc:
        st.session_state["action_error"] = str(exc)


def _on_undo(outcome_id: int):
    try:
        with db() as conn:
            undo_outcome(conn, outcome_id)
        _load_sections.clear()
        _queue_toast("Undone — customer is back on the list.", icon="↩️")
    except Exception as exc:
        st.session_state["action_error"] = str(exc)


def _on_save_note(customer_id: int, note_key: str):
    text = (st.session_state.get(note_key) or "").strip()
    if not text:
        return
    try:
        with db() as conn:
            save_note(conn, customer_id, text)
        st.session_state[note_key] = ""  # clear after save
        _load_sections.clear()
        _queue_toast("Note saved.", icon="📝")
    except Exception as exc:
        st.session_state["action_error"] = str(exc)


# ---------- helpers ----------

def _current_season() -> str:
    """Return 'cooling' (Apr-Oct) or 'heating' (Nov-Mar) for [cooling/heating]."""
    return "cooling" if 4 <= date.today().month <= 10 else "heating"


def _personalize_script_line(line: str, row: dict, kind: str) -> str:
    """Replace [name], [install date], [last service], [cooling/heating]
    placeholders with values from this specific customer's record."""
    # Name handling — works for businesses ("there"), single customers,
    # and couples ("Andrew and Karen").
    firsts = _first_names(row.get("customer_name") or "")
    if not firsts:
        name_token = "there"  # business or no first name
    elif len(firsts) == 1:
        name_token = firsts[0]
    else:
        name_token = f"{firsts[0]} and {firsts[1]}"
    line = line.replace("[name]", name_token)

    # Season is always swap-able regardless of kind.
    line = line.replace("[cooling/heating]", _current_season())

    if kind == "membership":
        install_date = row.get("install_date")
        if install_date:
            line = line.replace("[install date]", install_date.strftime("%B %-d"))
        else:
            line = line.replace("[install date]", "your recent install")
    elif kind == "sleeping":
        last_summary = (row.get("last_summary") or "").strip()
        if last_summary and "imported default" not in last_summary.lower():
            short_summary = last_summary[:80].rstrip(".").lower()
            line = line.replace("[last service]", short_summary)
        else:
            line = line.replace("[last service]", "your last visit")
    elif kind == "estimate":
        # [tech], [estimate date], [estimate name], [estimate value]
        tech = (row.get("originating_tech") or "").strip()
        line = line.replace("[tech]", tech if tech else "our team")
        created = row.get("created_on")
        if created:
            line = line.replace("[estimate date]", created.strftime("%B %-d"))
        else:
            line = line.replace("[estimate date]", "earlier this season")
        ename = (row.get("estimate_name") or "").strip()
        line = line.replace("[estimate name]", ename if ename else "the work we proposed")
        value = float(row.get("subtotal") or 0)
        line = line.replace("[estimate value]", f"${value:,.0f}" if value else "the quoted amount")

    return line


def _row_passes_filter(row: dict, kind: str, outreach: dict | None) -> bool:
    """Apply the active filter chip + search box."""
    f = st.session_state["call_list_filter"]
    if f == "Hot leads only":
        if not (outreach and outreach.get("called_back_at")):
            return False
    elif f == "Untouched only":
        if outreach and (outreach.get("attempts", 0) > 0 or outreach.get("called_back_at")):
            return False
    elif f in ("Missed calls", "Memberships", "Sleeping", "Estimates"):
        # Section filter applied at the section render level — always pass
        # when filtering by kind matches; otherwise this row is hidden.
        target_kind = {"Missed calls": "missed", "Memberships": "membership",
                       "Sleeping": "sleeping", "Estimates": "estimate"}[f]
        if kind != target_kind:
            return False

    # Search box (customer name contains)
    q = st.session_state["call_list_search"].strip().lower()
    if q:
        name = (row.get("customer_name") or "").lower()
        if q not in name:
            return False

    return True


def _outreach_for(state: dict, customer_id: int | None) -> dict | None:
    if not customer_id:
        return None
    return state["outreach"].get(customer_id)


def _status_badge_html(pending_days: int | None, outreach: dict | None) -> str:
    """Same badge logic as the email, but inline-friendly."""
    info = outreach or {}
    if info.get("called_back_at"):
        days = (datetime.now(timezone.utc) - info["called_back_at"]).days
        label = "today" if days == 0 else f"{days}d ago"
        return _pill(f"📥 Called us back {label}", "#FED7AA", "#9A3412")
    if info.get("conversations", 0) > 0:
        days = (datetime.now(timezone.utc) - info["last_outbound"]).days if info.get("last_outbound") else None
        label = "today" if days == 0 else f"{days}d ago"
        return _pill(f"📞 Spoke {label}", "#D1FAE5", "#065F46")
    attempts = info.get("attempts", 0)
    if attempts > 0:
        days = (datetime.now(timezone.utc) - info["last_outbound"]).days if info.get("last_outbound") else None
        last = "today" if days == 0 else f"{days}d ago"
        v, n = info.get("voicemails", 0), info.get("no_answers", 0)
        if v > 0 and n == 0:
            text = f"📨 Voicemail ×{attempts}, {last}"
        elif n > 0 and v == 0:
            text = f"🔕 No answer ×{attempts}, {last}"
        else:
            text = f"📨 Tried ×{attempts}, {last}"
        return _pill(text, "#FEE2E2", "#991B1B")
    if pending_days and pending_days > 0:
        return _pill(f"🔁 Pending {pending_days}d", "#FEF3C7", "#92400E")
    return ""


def _pill(text: str, bg: str, fg: str) -> str:
    return (
        f"<span style='display:inline-block;background:{bg};color:{fg};"
        f"padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;"
        f"margin-left:8px'>{text}</span>"
    )


def _pending_days(first_seen) -> int | None:
    if not first_seen:
        return None
    days = (datetime.now(timezone.utc) - first_seen).days
    return days if days > 0 else None


# ---------- render: header strip ----------

st.title("📞 Call List")
today_str = date.today().strftime("%A, %B %d, %Y")

# Flush any pending toast queued by a callback on the previous run.
if "pending_toast" in st.session_state:
    _msg, _icon = st.session_state.pop("pending_toast")
    st.toast(_msg, icon=_icon)

# Header caption with last-load timestamp so Fey knows how fresh the data is.
_loaded_at = datetime.now().strftime("%-I:%M %p")
st.caption(f"{today_str} · Data loaded at {_loaded_at}")

if st.session_state.get("action_error"):
    st.error(f"Action failed: {st.session_state['action_error']}")
    del st.session_state["action_error"]

data = _load_sections()
state = data["state"]
suppress = state["suppress"]
first_seen_map = state["first_seen"]
outreach_map = state["outreach"]
memberships_all = data["memberships_all"]
sleeping_all = data["sleeping_all"]
missed_all = data["missed_all"]
estimates_all = data["estimates_all"]
hot_leads_rows = data["hot_leads"]

# Apply suppression
memberships = [r for r in memberships_all
               if dedup_key("membership", r.get("customer_id")) not in suppress["membership"]]
sleeping = [r for r in sleeping_all
            if dedup_key("sleeping", r.get("customer_id")) not in suppress["sleeping"]]
missed = [r for r in missed_all
          if dedup_key("missed", r.get("customer_id"), r.get("id")) not in suppress["missed"]]
estimates = [r for r in estimates_all
             if dedup_key("estimate", r.get("customer_id"), r.get("id")) not in suppress["estimate"]]

# Cap
memberships_visible = memberships[:SECTION_CAPS["membership"]]
sleeping_visible = sleeping[:SECTION_CAPS["sleeping"]]
missed_visible = missed[:SECTION_CAPS["missed"]]
estimates_visible = estimates[:SECTION_CAPS["estimate"]]

# Today's outcomes
with db() as _conn:
    todays_outcomes = load_todays_outcomes(_conn)

# Hot leads count (customers who called us back since their last rec)
hot_count = sum(
    1 for c in (memberships_visible + sleeping_visible + missed_visible + estimates_visible)
    if (outreach_map.get(c.get("customer_id")) or {}).get("called_back_at")
)
to_call_count = (len(memberships_visible) + len(sleeping_visible)
                 + len(missed_visible) + len(estimates_visible))

# ---------- KPI strip ----------
k1, k2, k3, k4 = st.columns(4)
k1.metric("To call", f"{to_call_count}",
          help="Total open leads across all sections")
k2.metric("Called today", f"{len(todays_outcomes)}",
          help="Outcomes you've recorded today")
k3.metric("🔥 Hot leads", f"{hot_count}",
          help="Customers who called us back — highest priority")
with k4:
    if st.button("🔄 Refresh data", use_container_width=True):
        _load_sections.clear()
        st.rerun()

st.divider()

# ---------- filter + search row ----------
fc1, fc2 = st.columns([2, 3])
with fc1:
    _filter_options = ["All", "Hot leads only", "Untouched only",
                       "Missed calls", "Memberships", "Sleeping", "Estimates"]
    st.session_state["call_list_filter"] = st.selectbox(
        "Filter",
        _filter_options,
        index=_filter_options.index(st.session_state["call_list_filter"]),
        label_visibility="collapsed",
    )
with fc2:
    st.session_state["call_list_search"] = st.text_input(
        "Search by customer name",
        value=st.session_state["call_list_search"],
        placeholder="Search by customer name…",
        label_visibility="collapsed",
    )

# ---------- contact + opener prep ----------

# Build opener input dicts + look up phones/emails for everything we'll show
today_d = date.today()
all_visible = (list(memberships_visible) + list(sleeping_visible)
               + list(missed_visible) + list(estimates_visible))

# Phone/email enrichment — bulk-parallel so cold-cache loads are fast
# (~1-2s for 40 customers vs ~12s serially).
_all_cids = [r.get("customer_id") for r in all_visible if r.get("customer_id")]
_contact_map = _bulk_lookup_contacts(_all_cids)

for r in memberships_visible + sleeping_visible + estimates_visible:
    cid = r.get("customer_id")
    if cid:
        r["phone"], r["email"] = _contact_map.get(int(cid), ("", ""))
for r in missed_visible:
    cid = r.get("customer_id")
    if cid:
        r["phone"], r["email"] = _contact_map.get(int(cid), ("", ""))
        if not r["phone"]:
            r["phone"] = r.get("from_phone") or ""
    else:
        r["phone"] = r.get("from_phone") or ""
        r["email"] = ""

# Build opener input list
opener_inputs: list[dict] = []
for r in memberships_visible:
    install_date = r.get("install_date")
    opener_inputs.append({
        "customer_id": r.get("customer_id"),
        "customer_name": r.get("customer_name"),
        "kind": "membership",
        "equipment": r.get("equipment"),
        "install_summary": r.get("install_summary"),
        "install_days_ago": (today_d - install_date).days if install_date else None,
        "install_value": float(r.get("install_value") or 0),
        "lifetime_revenue": float(r.get("lifetime_revenue") or 0),
        "lifetime_invoices": int(r.get("lifetime_invoices") or 0),
        "first_visit_year": r["first_visit"].year if r.get("first_visit") else None,
    })
for r in sleeping_visible:
    last_visit = r.get("last_visit")
    opener_inputs.append({
        "customer_id": r.get("customer_id"),
        "customer_name": r.get("customer_name"),
        "kind": "sleeping",
        "last_visit_days_ago": (today_d - last_visit).days if last_visit else None,
        "last_summary": r.get("last_summary"),
        "last_items": r.get("last_items"),
        "loyal_revenue": float(r.get("loyal_revenue") or 0),
        "loyal_invoices": int(r.get("loyal_invoices") or 0),
    })
for r in missed_visible:
    received = r.get("received_on")
    last_visit = r.get("last_visit")
    opener_inputs.append({
        "customer_id": r.get("customer_id"),
        "customer_name": r.get("customer_name") or "Unknown",
        "kind": "missed",
        "call_type": r.get("call_type"),
        "call_when": to_central(received).strftime("%a %I:%M %p") if received else "earlier",
        "lifetime_revenue": float(r.get("lifetime_revenue") or 0),
        "lifetime_invoices": int(r.get("lifetime_invoices") or 0),
        "last_visit_days_ago": (today_d - last_visit).days if last_visit else None,
        "last_invoice_summary": r.get("last_invoice_summary"),
    })
for r in estimates_visible:
    opener_inputs.append({
        "customer_id": r.get("customer_id"),
        "customer_name": r.get("customer_name"),
        "kind": "estimate",
        "estimate_name": r.get("estimate_name"),
        "summary": r.get("summary"),
        "subtotal": float(r.get("subtotal") or 0),
        "age_days": int(r.get("age_days") or 0),
        "originating_tech": r.get("originating_tech"),
        "business_unit_name": r.get("business_unit_name"),
        "lifetime_revenue": float(r.get("lifetime_revenue") or 0),
        "lifetime_invoices": int(r.get("lifetime_invoices") or 0),
    })

opener_map = _get_openers_with_cache(opener_inputs)

# Bulk-load notes for everyone visible (one round trip)
all_cust_ids = [r.get("customer_id") for r in all_visible if r.get("customer_id")]
with db() as _conn:
    notes_map = load_notes_bulk(_conn, all_cust_ids) if all_cust_ids else {}


# ---------- per-row renderer ----------

def render_row(r: dict, kind: str, call_id: int | None = None):
    cid = r.get("customer_id")
    outreach = _outreach_for(state, cid)

    if not _row_passes_filter(r, kind, outreach):
        return

    name = r.get("customer_name") or "Unknown"
    phone = r.get("phone") or ""
    email = r.get("email") or ""
    opener = opener_map.get(cid) if cid else None

    pending_days = _pending_days(first_seen_map.get(dedup_key(kind, cid, call_id)))
    badge = _status_badge_html(pending_days, outreach)

    # Phone link
    phone_disp = fmt_phone(phone) if phone else "no phone"
    phone_html = (
        f"<a href='{escape(tel_href(phone))}' style='color:#0066EE;"
        f"text-decoration:none;font-weight:600'>{escape(phone_disp)}</a>"
        if phone else "<span style='color:#999'>no phone on file</span>"
    )
    email_html = (
        f" · <a href='mailto:{escape(email)}' style='color:#666;text-decoration:none'>{escape(email)}</a>"
        if email else ""
    )

    # Primary context line per kind
    primary = ""
    if kind == "membership":
        install_date = r.get("install_date")
        primary = (
            f"<b>{fmt_money(r['install_value'])}</b> install on "
            f"{install_date:%a %b %d} ({days_ago(install_date)})"
            + (f" · <i>{escape(short(r['equipment'], 80))}</i>" if r.get("equipment") else "")
        )
    elif kind == "sleeping":
        last_visit = r.get("last_visit")
        primary = (
            f"Last visit <b>{days_ago(last_visit)}</b> ({last_visit:%b %d, %Y})"
            + (f" · <i>{escape(short(r['last_summary'], 80))}</i>" if r.get("last_summary") else "")
        )
    elif kind == "missed":
        received_local = to_central(r.get("received_on"))
        primary = (
            f"<b>{escape(r['call_type'])}</b> at "
            f"{received_local:%-I:%M %p} on {received_local:%a %b %d}"
            + (f" · CSR: {escape(r['agent_name'])}" if r.get('agent_name') else "")
        )
    elif kind == "estimate":
        created = r.get("created_on")
        age = int(r.get("age_days") or 0)
        value = float(r.get("subtotal") or 0)
        tech = (r.get("originating_tech") or "").strip()
        ename = (r.get("estimate_name") or "").strip()
        primary = (
            f"<b>{fmt_money(value)}</b> estimate"
            + (f" — <i>{escape(short(ename, 60))}</i>" if ename else "")
            + (f" · sent {created:%b %d}" if created else "")
            + f" · <b>{age}d old</b>"
            + (f" · tech: {escape(tech)}" if tech else "")
        )

    # History line
    if kind == "membership":
        history = (
            f"Lifetime: {fmt_money(r.get('lifetime_revenue'))} across "
            f"{int(r.get('lifetime_invoices') or 0)} visits"
        )
    elif kind == "sleeping":
        history = (
            f"Loyal-period: {fmt_money(r.get('loyal_revenue'))} across "
            f"{int(r.get('loyal_invoices') or 0)} visits"
        )
    elif kind == "missed":
        if r.get("customer_id"):
            history = (
                f"Existing customer · lifetime {fmt_money(r.get('lifetime_revenue'))} "
                f"across {int(r.get('lifetime_invoices') or 0)} visits"
            )
        else:
            history = "New caller — no prior history"
    elif kind == "estimate":
        if int(r.get("lifetime_invoices") or 0) > 0:
            history = (
                f"Existing customer · lifetime {fmt_money(r.get('lifetime_revenue'))} "
                f"across {int(r.get('lifetime_invoices') or 0)} visits"
            )
        else:
            history = "First-time prospect — this estimate is their only interaction"
    else:
        history = ""

    # Render the row card
    with st.container(border=True):
        st.markdown(
            f"<div style='font-size:16px;font-weight:600;color:#111'>{escape(name)}{badge}</div>"
            f"<div style='font-size:14px;margin:2px 0 4px'>{phone_html}{email_html}</div>"
            f"<div style='font-size:13px;color:#333'>{primary}</div>"
            f"<div style='font-size:12px;color:#777;margin-top:2px'>{history}</div>"
            + (
                f"<div style='margin-top:8px;padding:8px 10px;background:#EFF6FF;"
                f"border-left:3px solid #0066EE;border-radius:4px;font-size:13px;"
                f"color:#1E3A8A;line-height:1.45'>"
                f"<span style='font-size:11px;font-weight:700;color:#1E40AF;"
                f"text-transform:uppercase;letter-spacing:0.04em'>✨ Opener</span> "
                f"<i>{escape(opener)}</i></div>"
                if opener else ""
            ),
            unsafe_allow_html=True,
        )

        # Per-row personalized script. Expanded by default so the full
        # talking points are always in view next to this customer's data.
        # Placeholders are replaced with the customer's actual record so
        # Fey can read it almost verbatim.
        script_items = CALL_SCRIPTS.get(kind, [])
        if script_items:
            with st.expander("📋 Call script", expanded=True):
                for script_label, script_line in script_items:
                    personalized = _personalize_script_line(script_line, r, kind)
                    st.markdown(
                        f"**{escape(script_label).upper()}** — {escape(personalized)}",
                        unsafe_allow_html=False,
                    )

        # Action buttons row. Missed calls can be recorded against the
        # call_id even without a matched customer (unknown callers); the
        # other sections require a customer_id.
        cfg = OUTCOME_CONFIG.get(kind, {})
        has_identifier = bool(cid) or (kind == "missed" and bool(call_id))
        cols = st.columns(len(cfg))
        for i, (outcome_key, info) in enumerate(cfg.items()):
            cols[i].button(
                info["label"],
                key=f"{kind}_{cid}_{call_id or 0}_{outcome_key}",
                on_click=_on_record_outcome,
                args=(kind, cid, call_id, outcome_key),
                use_container_width=True,
                disabled=not has_identifier,
                help=(
                    f"Permanent" if info["expires_days"] is None
                    else f"Suppresses for {info['expires_days']} days"
                ),
            )

        # Notes section. Surface the most recent note inline (above the
        # expander) so Fey sees prior context without having to open it.
        if cid:
            existing_notes = notes_map.get(cid, [])
            if existing_notes:
                most_recent = existing_notes[0]
                preview = short(most_recent["note"], 140)
                st.markdown(
                    f"<div style='margin-top:8px;padding:6px 10px;"
                    f"background:#FFFBEB;border-left:3px solid #F59E0B;"
                    f"border-radius:4px;font-size:12px;color:#78350F'>"
                    f"<b>📝 Last note · {most_recent['created_at']:%b %d, %-I:%M %p}</b> — "
                    f"<i>{escape(preview)}</i></div>",
                    unsafe_allow_html=True,
                )
            label = (
                f"📝 Notes ({len(existing_notes)}) — add another"
                if existing_notes else "📝 Add a note"
            )
            with st.expander(label):
                if len(existing_notes) > 1:
                    st.caption("Older notes:")
                    for n in existing_notes[1:4]:
                        st.caption(
                            f"_{n['created_at']:%b %d, %-I:%M %p}_ — "
                            + escape(n["note"][:300])
                        )
                note_key = f"note_input_{cid}_{call_id or 0}"
                st.text_area(
                    "Add a note",
                    key=note_key,
                    placeholder="e.g., Called, no answer, try Tuesday morning…",
                    height=70,
                    label_visibility="collapsed",
                )
                st.button(
                    "Save note",
                    key=f"savenote_{cid}_{call_id or 0}",
                    on_click=_on_save_note,
                    args=(cid, note_key),
                )


# ---------- 🔥 hot leads: customer-initiated contact in last 48h ----------
# Top-of-page strip — these are customers who reached out to US, not the
# other way around. Highest-intent signal in the entire system. Beats
# every section below by definition.

# Hot-lead scripts — one per reason. {name} interpolation only;
# the "Why hot" detail line already carries the deal specifics
# (amount, age, etc.) so scripts stay short and reusable.
HOT_LEAD_SCRIPTS = {
    "CALLBACK": [
        ("Opener", "\"Hi {name}, this is Fey at Pure Comfort — thanks for calling back. What can I help you with today?\""),
        ("Listen first", "Let them lead. Whatever they're calling about IS the opportunity. Don't pivot to a script."),
        ("Close", "\"Let me get someone out to take a look. What's better for you, morning or afternoon this week?\""),
    ],
    "WARM_QUOTE": [
        ("Opener", "\"Hi {name}, this is Fey at Pure Comfort. I saw you called in about your quote — wanted to follow up. Have you had a chance to look it over?\""),
        ("Discover", "\"Anything specific you'd like to talk through, or just need a little more time?\""),
        ("Save / urgency", "\"If you can lock it in this week, I can usually pull your install forward — let's get you on the calendar before things fill up.\""),
        ("Close", "\"Want to do that, or would it help if I sent a quick summary you can look at on your own time?\""),
    ],
    "CAPTURED_UNCLOSED": [
        ("Opener", "\"Hi {name}, it's Fey from Pure Comfort. I'm following up after our tech was out — did you get a chance to think over the recommendations?\""),
        ("Reframe", "\"The diagnostic fee gets applied toward the work if you go ahead — so it's already paying for itself the moment we get this done.\""),
        ("Close", "\"Want me to get someone back out this week to take care of it?\""),
        ("If \"still thinking\"", "\"Totally fair. Mind if I send you a short email recap so it's easy to refer back to?\""),
    ],
    "DECISION_DEADLINE": [
        ("Opener", "\"Hi {name}, this is Fey at Pure Comfort — wanted to check in on the quote our team sent you. How are you feeling about it?\""),
        ("Soft urgency", "\"We're filling up our install calendar — I'd hate for the timing to slip. Anything specific holding you back?\""),
        ("Close", "\"If we can confirm this week, I can put you on next week's schedule. Want me to lock it in?\""),
    ],
    "HIGH_LTV_SIGNAL": [
        ("Opener", "\"Hi {name}, this is Fey at Pure Comfort — always good to hear from you. What's going on?\""),
        ("Listen first", "Recognize the relationship before pitching anything. They've been loyal — treat this like a check-in, not a sale."),
        ("Soft offer", "\"While I have you — anything around the house that's been on your list? We can usually get out within a few days.\""),
    ],
}


def _hot_lead_first_name(customer_name: str) -> str:
    """Extract first name from 'Last, First' or 'First Last' formats."""
    if not customer_name:
        return "there"
    name = customer_name.strip()
    if "," in name:
        # "Straus, Nancy & Barney" → "Nancy"
        first_part = name.split(",", 1)[1].strip()
        return first_part.split()[0] if first_part else "there"
    return name.split()[0] if name.split() else "there"


_active_filter = st.session_state.get("call_list_filter", "All")
if hot_leads_rows and _active_filter in ("All", "Hot leads only"):
    # Color + label per signal kind. Same colors as Streamlit's alert
    # palette so they read intuitively (callback = green/inbound,
    # warm quote = blue/in-flight, captured = amber/needs action, etc).
    REASON_META = {
        "CALLBACK": {
            "emoji": "🔁", "label": "Called us back",
            "bg": "#D1FAE5", "fg": "#065F46",
        },
        "WARM_QUOTE": {
            "emoji": "💬", "label": "Warm quote",
            "bg": "#DBEAFE", "fg": "#1E3A8A",
        },
        "CAPTURED_UNCLOSED": {
            "emoji": "🔑", "label": "Captured, unclosed",
            "bg": "#FEF3C7", "fg": "#92400E",
        },
        "DECISION_DEADLINE": {
            "emoji": "⏰", "label": "Decision deadline",
            "bg": "#FED7AA", "fg": "#7C2D12",
        },
        "HIGH_LTV_SIGNAL": {
            "emoji": "💎", "label": "High-LTV signal",
            "bg": "#EDE9FE", "fg": "#5B21B6",
        },
    }

    # Header strip with the attention-grabbing styling
    st.markdown(
        f"<div style='background:linear-gradient(90deg,#FEE2E2 0%,#FED7AA 100%);"
        f"padding:10px 14px;border-radius:8px;border-left:4px solid #DC2626;"
        f"margin-bottom:8px'>"
        f"<div style='font-size:13px;font-weight:800;color:#7F1D1D;"
        f"text-transform:uppercase;letter-spacing:0.06em'>"
        f"🔥 Drop everything — {len(hot_leads_rows)} hot lead{'s' if len(hot_leads_rows) != 1 else ''}</div>"
        f"<div style='font-size:12px;color:#7C2D12;margin-top:2px'>"
        f"Call these before any of your buckets below. Each badge explains why."
        f"</div></div>",
        unsafe_allow_html=True,
    )

    _hot_cids_for_contacts = [r.get("customer_id") for r in hot_leads_rows
                              if r.get("customer_id")]
    _hot_contact_map = _bulk_lookup_contacts(_hot_cids_for_contacts) if _hot_cids_for_contacts else {}

    for r in hot_leads_rows:
        cid = r.get("customer_id")
        reason = r.get("reason") or "CALLBACK"
        meta = REASON_META.get(reason, REASON_META["CALLBACK"])

        # Resolve contact
        phone, email = "", ""
        if cid and int(cid) in _hot_contact_map:
            phone, email = _hot_contact_map[int(cid)]
        phone_disp = fmt_phone(phone) if phone else "no phone"
        phone_html = (
            f"<a href='{escape(tel_href(phone))}' style='color:#0066EE;"
            f"text-decoration:none;font-weight:700'>📞 {escape(phone_disp)}</a>"
            if phone else "<span style='color:#999'>📞 no phone on file</span>"
        )

        # History
        ltv = float(r.get("lifetime_revenue") or 0)
        invoices = int(r.get("lifetime_invoices") or 0)
        history_html = (
            f"Existing customer · lifetime {fmt_money(ltv)} across {invoices} visits"
            if ltv else "New caller — no prior history"
        )

        # Signal badge
        badge_html = (
            f"<span style='background:{meta['bg']};color:{meta['fg']};"
            f"padding:3px 10px;border-radius:10px;font-size:11px;"
            f"font-weight:700;text-transform:uppercase;letter-spacing:0.04em;"
            f"margin-left:8px'>"
            f"{meta['emoji']} {escape(meta['label'])}</span>"
        )

        with st.container(border=True):
            st.markdown(
                f"<div style='font-size:16px;font-weight:700;color:#111'>"
                f"{escape(r.get('customer_name') or 'Unknown')}{badge_html}</div>"
                f"<div style='font-size:13px;color:#444;margin-top:4px;"
                f"padding:6px 10px;background:{meta['bg']};border-radius:6px;"
                f"border-left:3px solid {meta['fg']}'>"
                f"<b>Why hot:</b> {escape(r.get('detail') or '')}</div>"
                f"<div style='font-size:14px;margin:6px 0 4px'>{phone_html}</div>"
                f"<div style='font-size:12px;color:#777'>{history_html}</div>",
                unsafe_allow_html=True,
            )

            # Personalized call script for this signal type. Expanded by
            # default — Fey should be able to scan it while the phone
            # rings without an extra click.
            script_items = HOT_LEAD_SCRIPTS.get(reason, [])
            if script_items:
                first_name = _hot_lead_first_name(r.get("customer_name"))
                with st.expander("📋 Call script", expanded=True):
                    for label, line in script_items:
                        personalized = line.format(name=first_name)
                        st.markdown(
                            f"**{escape(label).upper()}** — {escape(personalized)}"
                        )

            # Outcome buttons. Use the 'missed' outcome set since these
            # span multiple bucket types (callback → missed, warm quote →
            # estimate, etc.) but missed's set covers the actions Fey
            # needs after a hot-lead call.
            cfg = OUTCOME_CONFIG["missed"]
            cols = st.columns(len(cfg))
            for i, (outcome_key, info) in enumerate(cfg.items()):
                cols[i].button(
                    info["label"],
                    key=f"hot_{reason}_{cid}_{outcome_key}",
                    on_click=_on_record_outcome,
                    args=("missed", cid, None, outcome_key),
                    use_container_width=True,
                    help=(
                        "Permanent" if info["expires_days"] is None
                        else f"Suppresses for {info['expires_days']} days"
                    ),
                )

    st.markdown("<div style='margin-bottom:6px'></div>", unsafe_allow_html=True)


# ---------- sections ----------

# Order: missed first (most time-sensitive), then aging estimates (warm $$),
# then memberships, then sleeping.
section_specs = [
    ("missed", "📞 Missed calls", "#F34039", missed_visible),
    ("estimate", "📋 Aging estimates (30d+)", "#8B5CF6", estimates_visible),
    ("membership", "🤝 Membership opportunities", "#0066EE", memberships_visible),
    ("sleeping", "💤 Sleeping customers", "#F2A93B", sleeping_visible),
]

current_filter = st.session_state["call_list_filter"]
visible_kinds = {
    "Missed calls": {"missed"},
    "Memberships": {"membership"},
    "Sleeping": {"sleeping"},
    "Estimates": {"estimate"},
}.get(current_filter, {"missed", "membership", "sleeping", "estimate"})

for kind, label, color, rows in section_specs:
    if kind not in visible_kinds:
        continue
    # Pre-filter by search/hot/untouched
    filtered_rows = [r for r in rows if _row_passes_filter(r, kind, _outreach_for(state, r.get("customer_id")))]

    # Sort hot leads (called us back) to the top of the section so Fey
    # can power through the highest-priority ones first.
    def _row_priority(r):
        cid = r.get("customer_id")
        info = state["outreach"].get(cid, {}) if cid else {}
        return 0 if info.get("called_back_at") else 1
    filtered_rows.sort(key=_row_priority)

    count = len(filtered_rows)
    hot_count_in_section = sum(
        1 for r in filtered_rows
        if (state["outreach"].get(r.get("customer_id"), {}) or {}).get("called_back_at")
    )
    # Show the hot-lead count in the section header when there are any.
    header = f"{label} ({count})"
    if hot_count_in_section:
        header = f"{label} ({count}) · 🔥 {hot_count_in_section} hot"

    with st.expander(header, expanded=(count > 0 and kind == "missed")):
        if count == 0:
            empty_msg = {
                "missed": "🎉 No missed calls to chase. Nice work.",
                "membership": "🎉 Every recent install customer has been handled.",
                "sleeping": "🎉 No sleeping customers in rotation right now.",
            }.get(kind, "🎉 Nothing here — focus on the other sections.")
            empty_state(empty_msg)
        else:
            # The call script now lives per-row (personalized) right next
            # to each customer's data, so no section-level header needed.
            for r in filtered_rows:
                # For missed calls the secondary id is the call_id; for
                # estimates it's the estimate_id (so a customer with two
                # open quotes can be tracked independently).
                secondary_id = r.get("id") if kind in ("missed", "estimate") else None
                render_row(r, kind, call_id=secondary_id)

# ---------- completed today (with undo) ----------
st.divider()
with st.expander(f"✅ Completed today ({len(todays_outcomes)})", expanded=False):
    if not todays_outcomes:
        st.caption("Nothing logged yet today.")
    else:
        for o in todays_outcomes:
            kind = o["kind"]
            outcome = o["outcome"]
            cfg = OUTCOME_CONFIG.get(kind, {}).get(outcome, {})
            label = cfg.get("label", outcome)
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.markdown(
                f"**{escape(o['customer'])}** · _{kind}_ → **{label}**  \n"
                f"<span style='font-size:11px;color:#888'>"
                f"{o['recorded_at']:%I:%M %p}</span>",
                unsafe_allow_html=True,
            )
            c2.caption(o.get("notes") or "")
            c3.button(
                "↩️ Undo",
                key=f"undo_{o['id']}",
                on_click=_on_undo,
                args=(o["id"],),
            )
