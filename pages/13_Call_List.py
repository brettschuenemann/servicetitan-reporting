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
    """Pull all three sections + recommendation/outreach state."""
    with db() as conn:
        state = load_recommendation_state(conn)
        memberships_all = load_membership_opps(conn)
        sleeping_all = load_sleeping_customers(conn, limit=SECTION_CAPS["sleeping"] * 4)
        missed_all = load_missed_calls(conn)
    return {
        "state": state,
        "memberships_all": memberships_all,
        "sleeping_all": sleeping_all,
        "missed_all": missed_all,
    }


# Module-level per-customer opener cache. Lives for the lifetime of the
# Streamlit process (cleared on app reboot). Keyed by customer_id so
# clicking an action button doesn't invalidate the openers for the OTHER
# customers — only new arrivals trigger a Claude call.
_OPENER_CACHE: dict[int, tuple[datetime, str]] = {}
_OPENER_TTL_SECONDS = 4 * 3600


def _get_openers_with_cache(customer_inputs: list[dict]) -> dict[int, str]:
    """Per-customer caching for openers. Returns customer_id -> opener.

    Customers with a fresh entry in `_OPENER_CACHE` are returned from
    memory. Anyone without (or expired) is sent to Claude in a single
    batched call so we still amortize the API round-trip.
    """
    now = datetime.now()
    result: dict[int, str] = {}
    to_generate: list[dict] = []

    for c in customer_inputs:
        cid = c.get("customer_id")
        if not cid:
            continue
        hit = _OPENER_CACHE.get(int(cid))
        if hit:
            ts, opener = hit
            if (now - ts).total_seconds() < _OPENER_TTL_SECONDS:
                result[int(cid)] = opener
                continue
        to_generate.append(c)

    if to_generate:
        new_openers = generate_openers(to_generate)
        for cid, opener in new_openers.items():
            _OPENER_CACHE[int(cid)] = (now, opener)
            result[int(cid)] = opener

    return result


@st.cache_data(ttl=86400, show_spinner=False)
def _lookup_contact_cached(customer_id: int) -> tuple[str, str]:
    """Per-customer phone/email lookup, cached for a day."""
    return lookup_contact(get_client(), customer_id)


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
    elif f in ("Missed calls", "Memberships", "Sleeping"):
        # Section filter applied at the section render level — always pass
        # when filtering by kind matches; otherwise this row is hidden.
        target_kind = {"Missed calls": "missed", "Memberships": "membership",
                       "Sleeping": "sleeping"}[f]
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

# Apply suppression
memberships = [r for r in memberships_all
               if dedup_key("membership", r.get("customer_id")) not in suppress["membership"]]
sleeping = [r for r in sleeping_all
            if dedup_key("sleeping", r.get("customer_id")) not in suppress["sleeping"]]
missed = [r for r in missed_all
          if dedup_key("missed", r.get("customer_id"), r.get("id")) not in suppress["missed"]]

# Cap
memberships_visible = memberships[:SECTION_CAPS["membership"]]
sleeping_visible = sleeping[:SECTION_CAPS["sleeping"]]
missed_visible = missed[:SECTION_CAPS["missed"]]

# Today's outcomes
with db() as _conn:
    todays_outcomes = load_todays_outcomes(_conn)

# Hot leads count (customers who called us back since their last rec)
hot_count = sum(
    1 for c in (memberships_visible + sleeping_visible + missed_visible)
    if (outreach_map.get(c.get("customer_id")) or {}).get("called_back_at")
)
to_call_count = len(memberships_visible) + len(sleeping_visible) + len(missed_visible)

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
    st.session_state["call_list_filter"] = st.selectbox(
        "Filter",
        ["All", "Hot leads only", "Untouched only",
         "Missed calls", "Memberships", "Sleeping"],
        index=["All", "Hot leads only", "Untouched only",
               "Missed calls", "Memberships", "Sleeping"].index(
            st.session_state["call_list_filter"]),
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
all_visible = list(memberships_visible) + list(sleeping_visible) + list(missed_visible)

# Phone/email enrichment per customer (cached)
for r in memberships_visible + sleeping_visible:
    cid = r.get("customer_id")
    if cid:
        r["phone"], r["email"] = _lookup_contact_cached(int(cid))
for r in missed_visible:
    cid = r.get("customer_id")
    if cid:
        r["phone"], r["email"] = _lookup_contact_cached(int(cid))
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

# Order: missed first (most time-sensitive), then membership, then sleeping
section_specs = [
    ("missed", "📞 Missed calls", "#F34039", missed_visible),
    ("membership", "🤝 Membership opportunities", "#0066EE", memberships_visible),
    ("sleeping", "💤 Sleeping customers", "#F2A93B", sleeping_visible),
]

current_filter = st.session_state["call_list_filter"]
visible_kinds = {
    "Missed calls": {"missed"},
    "Memberships": {"membership"},
    "Sleeping": {"sleeping"},
}.get(current_filter, {"missed", "membership", "sleeping"})

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
                render_row(r, kind, call_id=r.get("id") if kind == "missed" else None)

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
