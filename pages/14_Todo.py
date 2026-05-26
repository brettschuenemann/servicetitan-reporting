"""Jake's Todo — combined view of weekly strategic items + fresh estimates.

Two stacked sections:
  1. "This week" — checkable action items parsed from the latest AI summary's
     "Do This Week" block. Checks persist in `ai_summary_todos`.
  2. "Details + specific outcomes" — open estimates ≤30 days old, shown as
     interactive cards with filter/search, action buttons (Sold / Working /
     Declined / VM / Try later / Wrong #), and inline notes per estimate.

Admin-only (require_password). Jake's actual interactive use will need a
real Jake login eventually, but for v1 this lives behind the admin gate
so Brett can validate the workflow.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timezone
from html import escape

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.auth import require_password
from lib.csr_outcomes import (
    OUTCOME_CONFIG,
    dedup_key,
    load_notes_bulk,
    record_outcome,
    save_note,
)
from lib.database import db
from lib.style import apply_mobile_styles, empty_state
from scripts.send_csr_daily_email import (
    fmt_money,
    fmt_phone,
    load_open_estimates,
    load_recommendation_state,
    short,
    tel_href,
    to_central,
)

st.set_page_config(page_title="Jake's Todo · Pure Comfort", layout="wide")
apply_mobile_styles()
require_password()


# ---------- AI summary action-item parser ----------

_DO_THIS_WEEK_HEADER_RE = re.compile(
    r"##\s*(?:Do This Week|Action Items|Next Steps|This Week)\s*\n",
    re.IGNORECASE,
)
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$", re.MULTILINE)


def _parse_ai_action_items(summary_md: str) -> list[str]:
    """Extract numbered items from the 'Do This Week' section.

    Returns the raw markdown of each item (may include **bold**) so the
    UI can render them with formatting. Returns [] if the section
    doesn't exist or no items are found.
    """
    m = _DO_THIS_WEEK_HEADER_RE.search(summary_md)
    if not m:
        return []
    # Slice from after the heading to the next ## or end
    rest = summary_md[m.end():]
    next_h = re.search(r"\n##\s+", rest)
    section = rest[:next_h.start()] if next_h else rest
    return [item.strip() for item in _NUMBERED_LINE_RE.findall(section)]


def _md_bold_to_html(text: str) -> str:
    """Turn **bold** into <b>bold</b> after escaping. Keep links clickable."""
    out = escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return out


# ---------- AI todo persistence ----------

@st.cache_data(ttl=60, show_spinner=False)
def _load_latest_summary() -> tuple[int, str, datetime] | None:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, summary_md, generated_at FROM ai_summaries "
            "ORDER BY generated_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        return None
    return int(row["id"]), row["summary_md"], row["generated_at"]


def _sync_ai_todos(summary_id: int, items: list[str]) -> list[dict]:
    """Upsert items into ai_summary_todos for this summary. Returns the
    current checked state for each item (preserves prior checked_at)."""
    if not items:
        return []
    from psycopg2.extras import execute_values
    with db() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO ai_summary_todos (summary_id, item_index, item_text) "
                "VALUES %s ON CONFLICT (summary_id, item_index) DO NOTHING",
                [(summary_id, i, txt) for i, txt in enumerate(items)],
            )
            conn.commit()
            cur.execute(
                "SELECT id, item_index, item_text, checked_at FROM ai_summary_todos "
                "WHERE summary_id = %s ORDER BY item_index",
                (summary_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def _toggle_ai_todo(todo_id: int, check: bool) -> None:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ai_summary_todos SET checked_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) if check else None, todo_id),
        )
    conn.commit()


# ---------- estimate status detection ----------

def _latest_active_outcomes(conn) -> dict[str, dict]:
    """Most recent non-undone outcome per dedup_key, filtered to estimates."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
              SELECT id, kind, dedup_key, outcome, recorded_at, expires_at,
                     ROW_NUMBER() OVER (PARTITION BY dedup_key
                                        ORDER BY recorded_at DESC) AS rn
              FROM csr_customer_outcomes
              WHERE kind = 'estimate'
            )
            SELECT dedup_key, outcome, recorded_at, expires_at
            FROM ranked WHERE rn = 1 AND outcome <> 'undone'
            """
        )
        return {r["dedup_key"]: dict(r) for r in cur.fetchall()}


def _bucket_for_estimate(est: dict, latest_outcomes: dict[str, dict]) -> str:
    """Return 'todo' | 'working' | 'done_today' | 'done_old'.

    Mapping:
      no outcome / expired             → todo
      sold/declined/wrong_number today → done_today (show as confirmation)
      sold/declined/wrong_number old   → done_old (filter out — already settled)
      working                          → working
      voicemail/try_later (in cooldown)→ working (active follow-up state)
    """
    key = dedup_key("estimate", est.get("customer_id"), est.get("id"))
    info = latest_outcomes.get(key)
    if not info:
        return "todo"
    expires = info.get("expires_at")
    if expires and expires < datetime.now(timezone.utc):
        return "todo"
    outcome = info["outcome"]
    if outcome in ("sold", "declined", "wrong_number"):
        # Done today vs older
        today_chicago = datetime.now(timezone.utc).date()  # close enough for v1
        if info["recorded_at"].date() == today_chicago:
            return "done_today"
        return "done_old"
    if outcome in ("working", "voicemail", "try_later"):
        return "working"
    return "todo"


# ---------- data loading ----------

@st.cache_data(ttl=120, show_spinner="Loading Jake's queue…")
def _load_todo_data() -> dict:
    with db() as conn:
        estimates = load_open_estimates(conn, min_age_days=0, max_age_days=30)
        latest_outcomes = _latest_active_outcomes(conn)
        # Suppression for non-estimate kinds isn't relevant here
    return {"estimates": estimates, "latest_outcomes": latest_outcomes}


# ---------- callbacks ----------

def _queue_toast(message: str, icon: str = "✅") -> None:
    st.session_state["todo_pending_toast"] = (message, icon)


def _on_record_outcome(customer_id: int | None, estimate_id: int, outcome: str):
    try:
        with db() as conn:
            record_outcome(conn, "estimate", customer_id, estimate_id, outcome)
        _load_todo_data.clear()
        label = OUTCOME_CONFIG["estimate"][outcome]["label"]
        _queue_toast(f"Marked **{label}**", icon="✅")
    except Exception as exc:
        st.session_state["todo_error"] = str(exc)


def _on_save_note(customer_id: int, note_key: str):
    text = (st.session_state.get(note_key) or "").strip()
    if not text:
        return
    try:
        with db() as conn:
            save_note(conn, customer_id, text)
        st.session_state[note_key] = ""
        _load_todo_data.clear()
        _queue_toast("Note saved.", icon="📝")
    except Exception as exc:
        st.session_state["todo_error"] = str(exc)


# ---------- header ----------

st.title("🎯 Jake's Todo")
st.caption(
    f"Fresh open estimates (≤30 days) + this week's strategic action items. "
    f"Loaded at {datetime.now().strftime('%-I:%M %p')}"
)

if "todo_pending_toast" in st.session_state:
    msg, ic = st.session_state.pop("todo_pending_toast")
    st.toast(msg, icon=ic)

if st.session_state.get("todo_error"):
    st.error(f"Action failed: {st.session_state['todo_error']}")
    del st.session_state["todo_error"]

# ---------- AI summary action items ----------

st.markdown("### 🧭 This week's focus")

_summary = _load_latest_summary()
if not _summary:
    empty_state("No AI summary generated yet. The weekly cron runs Friday evenings.")
else:
    summary_id, summary_md, generated_at = _summary
    items = _parse_ai_action_items(summary_md)
    if not items:
        st.caption("Latest AI summary didn't include a 'Do This Week' section.")
    else:
        st.caption(f"From the AI summary generated {generated_at:%b %d at %-I:%M %p}.")
        todos = _sync_ai_todos(summary_id, items)
        for t in todos:
            checked = t["checked_at"] is not None
            cb_key = f"ai_todo_{t['id']}"
            new_state = st.checkbox(
                t["item_text"],  # plain text; markdown bold renders inline
                value=checked,
                key=cb_key,
            )
            if new_state != checked:
                _toggle_ai_todo(t["id"], new_state)
                st.rerun()

st.divider()

# ---------- estimate workspace ----------

data = _load_todo_data()
estimates = data["estimates"]
latest_outcomes = data["latest_outcomes"]

# Classify each estimate
for e in estimates:
    e["_bucket"] = _bucket_for_estimate(e, latest_outcomes)
    e["_status_label"] = OUTCOME_CONFIG["estimate"].get(
        latest_outcomes.get(
            dedup_key("estimate", e.get("customer_id"), e.get("id")), {}
        ).get("outcome", ""),
        {},
    ).get("label", "")

# Filter out estimates that were marked done *before* today — they're settled
visible = [e for e in estimates if e["_bucket"] != "done_old"]
by_bucket: dict[str, list[dict]] = {"todo": [], "working": [], "done_today": []}
for e in visible:
    by_bucket[e["_bucket"]].append(e)

# Sort each bucket by value DESC then age DESC
for b in by_bucket.values():
    b.sort(key=lambda e: (-(float(e.get("subtotal") or 0)), -int(e.get("age_days") or 0)))

# KPI strip
total_pipeline = sum(
    float(e.get("subtotal") or 0)
    for e in by_bucket["todo"] + by_bucket["working"]
)
k1, k2, k3, k4 = st.columns(4)
k1.metric("📥 To do", len(by_bucket["todo"]))
k2.metric("⚙️ Working", len(by_bucket["working"]))
k3.metric("💰 Pipeline", f"${total_pipeline:,.0f}",
          help="Open value across To do + Working")
k4.metric("✅ Done today", len(by_bucket["done_today"]))

st.divider()

# ---------- detailed list view ----------

st.markdown("### 📝 Details + specific outcomes")
st.caption(
    "Use these cards for everything the kanban can't express — declined vs sold, "
    "leaving a voicemail, scheduling a try-later, attaching a note."
)

# Filter
filter_choice = st.radio(
    "Show",
    ["All open", "To do only", "Working only", "Done today"],
    index=0,
    horizontal=True,
    label_visibility="collapsed",
)
filter_to_bucket = {
    "To do only": {"todo"},
    "Working only": {"working"},
    "Done today": {"done_today"},
    "All open": {"todo", "working", "done_today"},
}
show_buckets = filter_to_bucket[filter_choice]

# Search
search = st.text_input(
    "Search customer", placeholder="Search by customer name…",
    label_visibility="collapsed",
)
q = search.strip().lower()

filtered = [
    e for e in visible
    if e["_bucket"] in show_buckets
    and (not q or q in (e.get("customer_name") or "").lower())
]
filtered.sort(key=lambda e: (-(float(e.get("subtotal") or 0)), -int(e.get("age_days") or 0)))

# Bulk-load notes
all_cids = [e.get("customer_id") for e in filtered if e.get("customer_id")]
with db() as _conn:
    notes_map = load_notes_bulk(_conn, all_cids) if all_cids else {}

if not filtered:
    empty_state("Nothing matches the current filter.", icon="🔍")
else:
    for e in filtered:
        cid = e.get("customer_id")
        est_id = e.get("id")
        with st.container(border=True):
            # Header line
            value = float(e.get("subtotal") or 0)
            age = int(e.get("age_days") or 0)
            tech = (e.get("originating_tech") or "").strip()
            bu = (e.get("business_unit_name") or "").strip()
            badge = ""
            if e["_status_label"]:
                bg = OUTCOME_CONFIG["estimate"].get(
                    latest_outcomes.get(
                        dedup_key("estimate", cid, est_id), {}
                    ).get("outcome", ""), {}
                ).get("color", "#6B7280")
                badge = (
                    f"<span style='background:{bg};color:white;padding:2px 8px;"
                    f"border-radius:10px;font-size:11px;font-weight:600;"
                    f"margin-left:8px'>{escape(e['_status_label'])}</span>"
                )
            st.markdown(
                f"<div style='font-size:16px;font-weight:600;color:#111'>"
                f"{escape(e.get('customer_name') or 'Unknown')}{badge}</div>"
                f"<div style='font-size:13px;color:#333;margin-top:2px'>"
                f"<b>{fmt_money(value)}</b>"
                + (f" · {escape(short(e.get('estimate_name') or '', 60))}" if e.get('estimate_name') else "")
                + f" · <b>{age}d old</b>"
                + (f" · sent {e['created_on']:%b %d}" if e.get('created_on') else "")
                + (f" · tech: {escape(tech)}" if tech else "")
                + (f" · BU: {escape(bu)}" if bu else "")
                + "</div>",
                unsafe_allow_html=True,
            )

            # Action buttons — all 6 outcomes in one row
            cfg = OUTCOME_CONFIG["estimate"]
            cols = st.columns(len(cfg))
            for i, (outcome_key, info) in enumerate(cfg.items()):
                cols[i].button(
                    info["label"],
                    key=f"act_{est_id}_{outcome_key}",
                    on_click=_on_record_outcome,
                    args=(cid, est_id, outcome_key),
                    use_container_width=True,
                    help=(
                        "Permanent" if info["expires_days"] is None
                        else f"Suppresses for {info['expires_days']} days"
                    ),
                )

            # Notes
            if cid:
                existing_notes = notes_map.get(cid, [])
                if existing_notes:
                    most_recent = existing_notes[0]
                    preview = short(most_recent["note"], 140)
                    st.markdown(
                        f"<div style='margin-top:8px;padding:6px 10px;"
                        f"background:#FFFBEB;border-left:3px solid #F59E0B;"
                        f"border-radius:4px;font-size:12px;color:#78350F'>"
                        f"<b>📝 Last note · {most_recent['created_at']:%b %d, %-I:%M %p}</b> "
                        f"— <i>{escape(preview)}</i></div>",
                        unsafe_allow_html=True,
                    )
                with st.expander(
                    f"📝 Notes ({len(existing_notes)})" if existing_notes else "📝 Add a note"
                ):
                    if len(existing_notes) > 1:
                        st.caption("Older notes:")
                        for n in existing_notes[1:4]:
                            st.caption(
                                f"_{n['created_at']:%b %d, %-I:%M %p}_ — "
                                + escape(n["note"][:300])
                            )
                    note_key = f"jake_note_{cid}_{est_id}"
                    st.text_area(
                        "Add a note",
                        key=note_key,
                        placeholder="e.g. Customer wants to compare to 2 other quotes…",
                        height=70,
                        label_visibility="collapsed",
                    )
                    st.button(
                        "Save note",
                        key=f"jake_savenote_{cid}_{est_id}",
                        on_click=_on_save_note,
                        args=(cid, note_key),
                    )
