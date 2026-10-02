"""Texts — Fey's SMS inbox + reply UI.

Three sections:
1. 📩 New replies      — inbound messages awaiting Fey's response
2. 💬 Active threads   — all customers with activity in last 14 days
3. ❓ Unmatched         — inbounds we couldn't link to a customer (Fey links manually)

Plus a campaign-stats strip at the top so Fey can see what outreach is in flight.
"""
from __future__ import annotations

import os, sys
from datetime import datetime, timezone
from html import escape

import streamlit as st
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from lib.database import db
from lib.servicetitan import ServiceTitanClient
from lib.sms import send_sms, normalize_phone, dry_run_enabled, match_customer_by_phone, provider_configured
from lib.sms_ai import suggest_reply, INTENT_META
from lib.style import apply_mobile_styles


st.set_page_config(page_title="Texts • Pure Comfort", layout="wide", page_icon="💬")
apply_mobile_styles()


# ── helpers ───────────────────────────────────────────────────────

def _st_client():
    if not all(os.environ.get(k) for k in (
        "ST_APP_KEY","ST_TENANT_ID","ST_CLIENT_ID","ST_CLIENT_SECRET"
    )):
        return None
    return ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"],
        tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"],
        client_secret=os.environ["ST_CLIENT_SECRET"],
    )


def _format_when(ts):
    if not ts:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    if delta.total_seconds() < 60:
        return "just now"
    mins = int(delta.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    return ts.strftime("%b %d")


def _bubble(direction: str, body: str, when: str, channel: str = "") -> str:
    """Render a single message bubble."""
    if direction == "outbound":
        bg, fg, align = "#0066EE", "white", "right"
        side_margin = "margin-left:60px;margin-right:0"
    else:
        bg, fg, align = "#E5E7EB", "#111827", "left"
        side_margin = "margin-right:60px;margin-left:0"
    meta = f"<div style='font-size:11px;color:#6B7280;text-align:{align};margin-top:2px'>{escape(when)}"
    if channel and channel != "manual":
        meta += f" · {escape(channel)}"
    meta += "</div>"
    return (
        f"<div style='margin:6px 0;{side_margin}'>"
        f"<div style='background:{bg};color:{fg};padding:8px 12px;"
        f"border-radius:14px;display:inline-block;max-width:80%;white-space:pre-wrap;"
        f"font-size:14px;line-height:1.4'>{escape(body)}</div>"
        f"{meta}"
        f"</div>"
    )


# ── data loading ──────────────────────────────────────────────────

# Loaders cache aggressively; invalidation is explicit. live_inbox()
# polls a one-row cursor each tick and clears these only when a message
# arrived, was sent, or an unmatched row was linked — so ticks and
# reruns cost one cheap query, not a full reload.
@st.cache_data(ttl=300, show_spinner=False)
def load_threads(limit: int = 50) -> list[dict]:
    """Group messages by customer; return most recent N threads."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ranked AS (
                  SELECT
                    COALESCE(customer_id::text, from_phone) AS key,
                    customer_id, from_phone, to_phone,
                    direction, body, channel, sent_at, status,
                    ROW_NUMBER() OVER (
                      PARTITION BY COALESCE(customer_id::text,
                        CASE WHEN direction = 'inbound'
                             THEN from_phone ELSE to_phone END)
                      ORDER BY sent_at DESC
                    ) AS rn
                  FROM sms_messages
                  WHERE sent_at >= NOW() - INTERVAL '14 days'
                ),
                latest AS (
                  SELECT * FROM ranked WHERE rn = 1
                ),
                cust AS (
                  SELECT customer_id, MIN(customer_name) AS name
                  FROM invoices
                  WHERE customer_name IS NOT NULL AND customer_id IS NOT NULL
                  GROUP BY customer_id
                )
                SELECT
                  l.customer_id,
                  l.from_phone,
                  l.to_phone,
                  l.direction,
                  l.body,
                  l.channel,
                  l.sent_at,
                  l.status,
                  c.name AS customer_name,
                  (SELECT COUNT(*) FROM sms_messages s
                   WHERE COALESCE(s.customer_id::text,
                     CASE WHEN s.direction='inbound' THEN s.from_phone
                          ELSE s.to_phone END)
                     = COALESCE(l.customer_id::text,
                       CASE WHEN l.direction='inbound' THEN l.from_phone
                            ELSE l.to_phone END)) AS thread_size,
                  -- "needs reply" = latest message is inbound and Fey hasn't responded yet
                  (l.direction = 'inbound') AS needs_reply
                FROM latest l
                LEFT JOIN cust c ON c.customer_id = l.customer_id
                ORDER BY l.sent_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


@st.cache_data(ttl=300, show_spinner=False)
def load_thread_messages(customer_id: int | None, phone: str | None) -> list[dict]:
    """Pull the full message log for one thread."""
    with db() as conn:
        with conn.cursor() as cur:
            if customer_id:
                cur.execute(
                    """
                    SELECT direction, body, channel, sent_at, status, sent_by
                    FROM sms_messages
                    WHERE customer_id = %s
                    ORDER BY sent_at ASC LIMIT 100
                    """,
                    (customer_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT direction, body, channel, sent_at, status, sent_by
                    FROM sms_messages
                    WHERE (from_phone = %s OR to_phone = %s)
                      AND customer_id IS NULL
                    ORDER BY sent_at ASC LIMIT 100
                    """,
                    (phone, phone),
                )
            return [dict(r) for r in cur.fetchall()]


@st.cache_data(ttl=300, show_spinner=False)
def load_unmatched(limit: int = 20) -> list[dict]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.from_phone, u.created_at,
                       m.body, m.id AS message_id
                FROM sms_unmatched u
                JOIN sms_messages m ON m.id = u.message_id
                WHERE u.resolved_at IS NULL
                ORDER BY u.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


def _inbox_cursor() -> tuple:
    """Newest message id + open unmatched count, one cheap round trip.
    Any send, inbound, or link moves it — the inbox only pays for a
    full reload when this changes."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT COALESCE(MAX(id), 0) FROM sms_messages) AS m, "
                "(SELECT COUNT(*) FROM sms_unmatched WHERE resolved_at IS NULL) AS u"
            )
            row = cur.fetchone()
    return (row["m"], row["u"])


@st.cache_data(ttl=300, show_spinner=False)
def _cached_suggestion(thread_signature: tuple) -> dict:
    """Cache AI suggestions by thread signature so we don't re-call Claude
    on every page render. thread_signature is a tuple of (direction, body)
    pairs — changes when a new message arrives."""
    msgs = [{"direction": d, "body": b} for d, b in thread_signature]
    return suggest_reply(msgs)


@st.cache_data(ttl=120, show_spinner=False)
def load_active_campaigns() -> list[dict]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, kind, recipient_count, sent_count, reply_count,
                       started_at, completed_at, dry_run
                FROM sms_campaigns
                WHERE started_at >= NOW() - INTERVAL '30 days'
                ORDER BY started_at DESC
                LIMIT 10
                """
            )
            return [dict(r) for r in cur.fetchall()]


# ── render ────────────────────────────────────────────────────────

st.title("💬 Texts")

# Status banner
if dry_run_enabled():
    st.warning("**DRY-RUN MODE** — `SMS_DRY_RUN=1`. Messages are logged but NOT sent. "
               "Unset the env var to send for real.")
elif not provider_configured():
    st.info("No SMS provider configured — page is read-only until Telnyx "
            "(`TELNYX_API_KEY` + `SMS_FROM_NUMBER`) or Twilio credentials are set.")

# ── Campaigns strip ──────────────────────────────────────────────
campaigns = load_active_campaigns()
if campaigns:
    with st.expander(f"📊 Recent campaigns ({len(campaigns)})", expanded=False):
        for c in campaigns:
            badge = "🧪 DRY" if c["dry_run"] else "🟢 LIVE"
            done = "" if not c.get("completed_at") else f" · done {c['completed_at']:%b %d}"
            st.markdown(
                f"**{badge} {escape(c['name'])}** — {escape(c['kind'])} · "
                f"{c['sent_count']}/{c['recipient_count']} sent · "
                f"{c['reply_count']} replies · started {c['started_at']:%b %d %H:%M}{done}"
            )

# ── ✏️ Compose — send a text to any number ────────────────────────
# Its own fragment: typing in here reruns just this block, never the
# campaigns strip or the inbox. All sends route through send_sms:
# opt-out check, dry-run flag, ST-note push, and thread logging apply.

@st.cache_data(ttl=600, show_spinner=False)
def _compose_match(norm: str) -> tuple:
    """Phone → (customer_id, display name). Cached so the preview costs
    one lookup per number, not one per keystroke-commit."""
    with db() as conn:
        cid = match_customer_by_phone(conn, norm)
        name = None
        if cid:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MIN(customer_name) AS n FROM invoices WHERE customer_id = %s",
                    (cid,),
                )
                row = cur.fetchone()
            name = (row or {}).get("n")
    return cid, name


@st.fragment
def compose_box() -> None:
    with st.expander("✏️ New text message", expanded=False):
        # Result of the previous run's send: st.rerun() wipes in-run
        # messages, so the handler stashes the outcome and we show it here.
        _flash = st.session_state.pop("compose_flash", None)
        if _flash:
            _fn = {"success": st.success, "info": st.info, "error": st.error}
            _fn.get(_flash[0], st.info)(_flash[1])
        # Widget keys can't be written after the widget instantiates, so the
        # send handler sets a flag and the clear happens here, next run.
        if st.session_state.pop("compose_clear", False):
            st.session_state["compose_body"] = ""

        comp_phone = st.text_input(
            "To (any format)", key="compose_phone",
            placeholder="(847) 555-1234",
        )
        comp_norm = normalize_phone(comp_phone) if comp_phone.strip() else None
        comp_cid = None
        if comp_phone.strip() and not comp_norm:
            st.error("Needs a valid 10-digit US number.")
        elif comp_norm:
            comp_cid, _name = _compose_match(comp_norm)
            if comp_cid:
                st.caption(f"✓ {comp_norm} — matches **{_name or f'customer {comp_cid}'}** "
                           f"(thread + ST note will attach to their record)")
            else:
                st.caption(f"{comp_norm} — no customer match; lands in the "
                           "unmatched bucket until linked")

        comp_body = st.text_area(
            "Message", key="compose_body", height=90,
            placeholder="Type the message…",
        )
        _chars = len(comp_body or "")
        _segs = 1 if _chars <= 160 else -(-_chars // 153)
        st.caption(f"{_chars} chars · {_segs} SMS segment(s)")

        if st.button("📤 Send text", key="compose_send", type="primary",
                     disabled=not (comp_norm and (comp_body or "").strip())):
            try:
                with db() as _s_conn:
                    _res = send_sms(
                        _s_conn,
                        to_phone=comp_norm,
                        body=comp_body.strip(),
                        channel="manual",
                        customer_id=comp_cid,
                        sent_by="fey",
                        post_to_st=bool(comp_cid),
                        st_client=_st_client(),
                    )
                _status = _res.get("status")
                if _status == "opted_out":
                    _flash_out = ("error", "That number is on the opt-out list — NOT sent.")
                elif _status == "dry_run":
                    _flash_out = ("info", f"Dry-run mode — logged to the thread for {comp_norm} "
                                          "but not actually sent.")
                elif _status == "failed":
                    _flash_out = ("error", "Send failed: "
                                  f"{_res.get('error_message') or _res.get('error_code')}")
                else:
                    _flash_out = ("success", f"Sent to {comp_norm} ({_status}).")
                st.session_state["compose_flash"] = _flash_out
                st.session_state["compose_clear"] = _status not in ("opted_out", "failed")
                # Full-app rerun so the inbox picks the new message up
                # immediately (its cursor check does the cache clearing).
                st.rerun(scope="app")
            except Exception as _exc:
                st.error(f"Send failed: {_exc}")


compose_box()


# The inbox polls itself: this fragment reruns every 15s so inbound
# texts appear while Fey is just looking at the page. Draft replies
# survive reruns via their session_state keys.
@st.fragment(run_every="15s")
def live_inbox() -> None:
    # One cheap query per tick/interaction; the heavy loaders only
    # refetch when something actually changed.
    cursor = _inbox_cursor()
    if st.session_state.get("inbox_cursor") != cursor:
        st.session_state["inbox_cursor"] = cursor
        load_threads.clear()
        load_thread_messages.clear()
        load_unmatched.clear()

    # Outcome of the previous run's send/link — st.rerun() wipes in-run
    # messages, so handlers stash the result here and it shows on top,
    # where it stays visible even if the thread moved or collapsed.
    _flash = st.session_state.pop("inbox_flash", None)
    if _flash:
        _fn = {"success": st.success, "info": st.info, "error": st.error}
        _fn.get(_flash[0], st.info)(_flash[1])

    unmatched = load_unmatched()
    if unmatched:
        st.subheader(f"❓ Unmatched messages ({len(unmatched)})")
        st.caption("Customer texted us but we couldn't link them to an ST record. "
                   "Enter their customer_id to link.")
        for u in unmatched:
            with st.container(border=True):
                cols = st.columns([3, 2, 2])
                cols[0].markdown(
                    f"**📞 {escape(u['from_phone'])}** · {_format_when(u['created_at'])}<br>"
                    f"<span style='color:#444'>{escape((u['body'] or '')[:200])}</span>",
                    unsafe_allow_html=True,
                )
                cid_input = cols[1].text_input(
                    "ST customer_id",
                    key=f"link_cid_{u['id']}",
                    placeholder="e.g. 151125572",
                    label_visibility="collapsed",
                )
                if cols[2].button("🔗 Link", key=f"link_btn_{u['id']}", use_container_width=True):
                    try:
                        cid = int(cid_input.strip())
                        with db() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE sms_messages SET customer_id = %s WHERE id = %s",
                                    (cid, u["message_id"]),
                                )
                                cur.execute(
                                    """UPDATE sms_unmatched
                                       SET resolved_at = NOW(), linked_customer_id = %s
                                       WHERE id = %s""",
                                    (cid, u["id"]),
                                )
                            conn.commit()
                        st.session_state["inbox_flash"] = (
                            "success", f"Linked to customer {cid}")
                        st.rerun()
                    except ValueError:
                        st.error("Customer ID must be a number")
                    except Exception as exc:
                        st.error(f"Link failed: {exc}")

    # ── Threads ───────────────────────────────────────────────────────
    threads = load_threads()
    needs_reply = [t for t in threads if t["needs_reply"]]
    others = [t for t in threads if not t["needs_reply"]]

    def render_thread(t: dict) -> None:
        key_phone = t.get("from_phone") if t["direction"] == "inbound" else t.get("to_phone")
        title_name = t.get("customer_name") or f"📞 {t.get('from_phone') or t.get('to_phone')}"
        summary = (t["body"] or "")[:80].replace("\n", " ")
        label = f"{'📩 ' if t['needs_reply'] else '💬 '}{title_name} — {summary}"

        with st.expander(label, expanded=t["needs_reply"]):
            st.caption(f"Latest: {t['direction']} · {_format_when(t['sent_at'])} · "
                       f"{t['thread_size']} messages")

            # Render full conversation
            msgs = load_thread_messages(t.get("customer_id"), key_phone)
            thread_html = "".join(
                _bubble(m["direction"], m["body"] or "",
                        _format_when(m["sent_at"]), m.get("channel") or "")
                for m in msgs
            )
            st.markdown(f"<div>{thread_html}</div>", unsafe_allow_html=True)

            # AI suggested reply — only when latest is inbound (needs reply)
            # and Anthropic key is available
            prefill_key = f"prefill_{t.get('customer_id') or key_phone}"
            if t["needs_reply"] and os.environ.get("ANTHROPIC_API_KEY"):
                suggestion = _cached_suggestion(
                    tuple((m["direction"], m["body"] or "") for m in msgs)
                )
                intent = suggestion.get("intent", "unclear")
                reply_text_ai = suggestion.get("suggested_reply", "")
                if reply_text_ai:
                    emoji, color, label = INTENT_META.get(
                        intent, INTENT_META["unclear"]
                    )
                    st.markdown(
                        f"<div style='background:#F9FAFB;border-left:3px solid {color};"
                        f"padding:8px 12px;margin:8px 0;border-radius:4px'>"
                        f"<div style='font-size:11px;font-weight:700;color:{color};"
                        f"text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px'>"
                        f"🤖 Suggested reply · {emoji} {escape(label)}</div>"
                        f"<div style='font-size:14px;color:#111827;line-height:1.45'>"
                        f"{escape(reply_text_ai)}</div></div>",
                        unsafe_allow_html=True,
                    )
                    if st.button("✨ Use this suggestion",
                                 key=f"use_sugg_{prefill_key}",
                                 use_container_width=False):
                        # Write directly into the textarea's own session-state
                        # slot — Streamlit ignores a text_area's `value` param
                        # once its key exists, so a separate prefill key never
                        # reached the widget (Send stayed disabled).
                        st.session_state[f"reply_{t.get('customer_id') or key_phone}"] = reply_text_ai
                        st.rerun()
                elif intent == "unclear":
                    st.caption("🤔 AI couldn't draft a clean reply — your turn.")

            # Reply input
            reply_key = f"reply_{t.get('customer_id') or key_phone}"
            # Draft-clear flag from a successful send last run (widget
            # keys can't be written after the widget instantiates).
            if st.session_state.pop(f"clear_{reply_key}", False):
                st.session_state[reply_key] = ""
            reply_text = st.text_area(
                "Reply",
                key=reply_key,
                placeholder="Type your reply…",
                height=80,
                label_visibility="collapsed",
            )
            send_col, info_col = st.columns([1, 3])
            if send_col.button("📤 Send", key=f"send_{reply_key}",
                                disabled=not reply_text.strip(),
                                use_container_width=True):
                try:
                    with db() as conn:
                        row = send_sms(
                            conn,
                            to_phone=key_phone,
                            body=reply_text.strip(),
                            channel="manual",
                            customer_id=t.get("customer_id"),
                            sent_by="fey",
                            post_to_st=bool(t.get("customer_id")),
                            st_client=_st_client(),
                        )
                    _status = row.get("status")
                    if _status == "opted_out":
                        _out = ("error", "Recipient opted out — message NOT sent.")
                    elif _status == "dry_run":
                        _out = ("info", f"Dry-run — reply to {title_name} logged "
                                        "but not actually sent.")
                    elif _status == "failed":
                        _out = ("error", "Send failed: "
                                f"{row.get('error_message') or row.get('error_code')}")
                    else:
                        _out = ("success", f"Reply sent to {title_name} ({_status}).")
                    st.session_state["inbox_flash"] = _out
                    st.session_state[f"clear_{reply_key}"] = (
                        _status not in ("opted_out", "failed"))
                    st.rerun()
                except Exception as exc:
                    st.error(f"Send failed: {exc}")


    if needs_reply:
        st.subheader(f"📩 New replies ({len(needs_reply)})")
        for t in needs_reply:
            render_thread(t)

    if others:
        st.subheader(f"💬 Other recent threads ({len(others)})")
        for t in others:
            render_thread(t)

    if not threads and not unmatched:
        st.info("No messages yet. Once Twilio is wired up and the cron starts running, "
                "inbound and outbound SMS will appear here.")


live_inbox()
