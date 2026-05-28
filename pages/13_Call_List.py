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
    """Pull all four sections + recommendation/outreach state."""
    with db() as conn:
        state = load_recommendation_state(conn)
        memberships_all = load_membership_opps(conn)
        sleeping_all = load_sleeping_customers(conn, limit=SECTION_CAPS["sleeping"] * 4)
        missed_all = load_missed_calls(conn)
        # 30d+ aging estimates — Jake handles the fresh ones, these are Fey's.
        # Pull 4× the cap so suppressed/in-cooldown ones still leave a healthy
        # bench when she clears the visible list.
        estimates_all = load_open_estimates(conn, min_age_days=30)
    return {
        "state": state,
        "memberships_all": memberships_all,
        "sleeping_all": sleeping_all,
        "missed_all": missed_all,
        "estimates_all": estimates_all,
    }


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



# ---------- sections ----------

# Order: missed first (most time-sensitive), then memberships (highest
# conversion rate + recurring revenue), then aging estimates, then sleeping.
section_specs = [
    ("missed", "📞 Missed calls", "#F34039", missed_visible),
    ("membership", "🤝 Membership opportunities", "#0066EE", memberships_visible),
    ("estimate", "📋 Aging estimates (30d+)", "#8B5CF6", estimates_visible),
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
