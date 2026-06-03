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
from lib.sms import send_sms, normalize_phone, dry_run_enabled, match_customer_by_phone
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

@st.cache_data(ttl=30, show_spinner="Loading messages…")
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


@st.cache_data(ttl=300, show_spinner=False)
def _cached_suggestion(thread_signature: tuple) -> dict:
    """Cache AI suggestions by thread signature so we don't re-call Claude
    on every page render. thread_signature is a tuple of (direction, body)
    pairs — changes when a new message arrives."""
    msgs = [{"direction": d, "body": b} for d, b in thread_signature]
    return suggest_reply(msgs)


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
elif not os.environ.get("TWILIO_ACCOUNT_SID"):
    st.info("Twilio credentials not yet configured — page is read-only until "
            "`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` are set in env.")

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

# ── Unmatched bucket ──────────────────────────────────────────────
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
                    st.success(f"Linked to customer {cid}")
                    load_threads.clear()
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
                    st.session_state[prefill_key] = reply_text_ai
                    st.rerun()
            elif intent == "unclear":
                st.caption("🤔 AI couldn't draft a clean reply — your turn.")

        # Reply input
        reply_key = f"reply_{t.get('customer_id') or key_phone}"
        reply_text = st.text_area(
            "Reply",
            key=reply_key,
            value=st.session_state.pop(prefill_key, ""),
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
                if row.get("status") == "opted_out":
                    st.error("Recipient opted out — message NOT sent.")
                elif row.get("status") == "dry_run":
                    st.info("Dry-run — message logged but not actually sent.")
                else:
                    st.success(f"Sent. Status: {row.get('status')}")
                load_threads.clear()
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
