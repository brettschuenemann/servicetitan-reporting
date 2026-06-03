"""SMS Admin — outreach preview, opt-outs, campaign history.

Three tabs:
  1. 📤 Outreach preview — pick a pool, see the exact rendered messages,
     authorize the send.
  2. 🚫 Opt-outs — view all opted-out phones; manually add/remove.
  3. 📊 Campaign history — past batches with stats.

This is the SAFETY page. Before sending any real SMS, preview here to
catch personalization issues, opted-out leakage, bad phone parses, etc.
"""
from __future__ import annotations

import os, sys
from datetime import datetime, timezone, timedelta
from html import escape

import streamlit as st
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from lib.database import db
from lib.servicetitan import ServiceTitanClient
from lib.sms import (
    send_sms, normalize_phone, dry_run_enabled, record_opt_out,
)
from lib.style import apply_mobile_styles, page_header
from lib.auth import require_password

# Import the pool selectors and templates from the batch script
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from sms_outreach_batch import (
    select_sleeping, select_reactivation,
    SLEEPING_TEMPLATE, REACTIVATION_TEMPLATE,
    _first_name, _last_visit_phrase,
)


st.set_page_config(page_title="SMS Admin • Pure Comfort", layout="wide", page_icon="📤")
apply_mobile_styles()
require_password()

page_header(
    "SMS admin",
    "Preview outreach batches before sending, manage opt-outs, "
    "review campaign history.",
)

# Status banner
if dry_run_enabled():
    st.warning("**DRY-RUN MODE** — `SMS_DRY_RUN=1`. Any 'send' button below "
               "will log but NOT actually send.")
elif not os.environ.get("TWILIO_ACCOUNT_SID"):
    st.info("Twilio not configured — sends will be logged with status='failed'. "
            "Preview functionality still works.")


tab1, tab2, tab3 = st.tabs([
    "📤 Outreach preview",
    "🚫 Opt-outs",
    "📊 Campaign history",
])


# ───────────────── Tab 1: Outreach preview ─────────────────
with tab1:
    st.subheader("Compose a batch")
    col_a, col_b, col_c = st.columns([1, 1, 1])
    with col_a:
        kind = st.radio("Pool", ["sleeping", "reactivation"], horizontal=True)
    with col_b:
        batch_size = st.number_input(
            "Batch size", min_value=1, max_value=200, value=10, step=5
        )
    with col_c:
        min_ltv = st.number_input(
            "Min LTV (sleeping only)", min_value=0, value=5000, step=500,
            disabled=(kind != "sleeping"),
        )

    template = SLEEPING_TEMPLATE if kind == "sleeping" else REACTIVATION_TEMPLATE
    with st.expander("📝 Template", expanded=False):
        st.code(template, language=None)

    if st.button("🔍 Preview recipients", use_container_width=False):
        with db() as conn:
            if kind == "sleeping":
                recipients = select_sleeping(conn, int(batch_size), float(min_ltv))
            else:
                recipients = select_reactivation(conn, int(batch_size))

        st.session_state["sms_preview"] = {
            "kind": kind,
            "template": template,
            "recipients": recipients,
        }

    preview = st.session_state.get("sms_preview")
    if preview and preview.get("recipients"):
        recipients = preview["recipients"]
        st.divider()
        st.markdown(f"**Preview — {len(recipients)} recipient(s) selected:**")

        for r in recipients:
            phone = normalize_phone(r.get("phone"))
            first = _first_name(r.get("customer_name"))
            lv_phrase = _last_visit_phrase(r.get("last_visit"))
            body = preview["template"].format(
                first_name=first, last_visit_phrase=lv_phrase,
            )
            phone_disp = phone or "❌ unparseable"
            name = r.get("customer_name") or "(no name)"
            ltv = float(r.get("lifetime_revenue") or 0)
            ltv_disp = f"${ltv:,.0f}" if ltv else "—"

            with st.container(border=True):
                cols = st.columns([3, 2])
                cols[0].markdown(
                    f"**{escape(name)}** · LTV {ltv_disp} · "
                    f"📞 {escape(phone_disp)} · cust {r.get('customer_id')}"
                )
                if not phone:
                    cols[1].error("Will be SKIPPED — bad phone")
                cols[0].markdown(
                    f"<div style='background:#EFF6FF;padding:8px 12px;border-radius:6px;"
                    f"font-size:13px;color:#1E40AF;margin-top:4px;border-left:3px solid #0066EE'>"
                    f"{escape(body)}</div>",
                    unsafe_allow_html=True,
                )

        st.divider()
        # Authorization box
        auth_col, send_col = st.columns([2, 1])
        with auth_col:
            confirm_text = st.text_input(
                "Type SEND to authorize",
                placeholder="SEND",
                label_visibility="collapsed",
            )
        with send_col:
            can_send = confirm_text.strip().upper() == "SEND"
            if st.button(
                "📤 Authorize and send batch",
                disabled=not can_send,
                use_container_width=True,
                type="primary",
            ):
                st_client = None
                if all(os.environ.get(k) for k in (
                    "ST_APP_KEY","ST_TENANT_ID","ST_CLIENT_ID","ST_CLIENT_SECRET"
                )):
                    st_client = ServiceTitanClient(
                        app_key=os.environ["ST_APP_KEY"],
                        tenant_id=os.environ["ST_TENANT_ID"],
                        client_id=os.environ["ST_CLIENT_ID"],
                        client_secret=os.environ["ST_CLIENT_SECRET"],
                    )

                campaign_name = f"{preview['kind']}-{datetime.now().strftime('%Y%m%d-%H%M')}-ui"
                sent = 0
                skipped = 0
                with st.spinner("Sending…"):
                    with db() as conn:
                        # Create campaign row
                        with conn.cursor() as cur:
                            cur.execute(
                                """INSERT INTO sms_campaigns
                                   (name, kind, template, started_at, dry_run)
                                   VALUES (%s, %s, %s, NOW(), %s) RETURNING id""",
                                (campaign_name, preview["kind"], preview["template"],
                                 dry_run_enabled()),
                            )
                            campaign_id = cur.fetchone()["id"]
                        conn.commit()

                        for r in recipients:
                            phone = normalize_phone(r.get("phone"))
                            if not phone:
                                skipped += 1
                                continue
                            first = _first_name(r.get("customer_name"))
                            body = preview["template"].format(
                                first_name=first,
                                last_visit_phrase=_last_visit_phrase(r.get("last_visit")),
                            )
                            try:
                                row = send_sms(
                                    conn,
                                    to_phone=phone, body=body,
                                    channel=preview["kind"],
                                    customer_id=r.get("customer_id"),
                                    sent_by=f"campaign:{campaign_name}",
                                    post_to_st=bool(r.get("customer_id")),
                                    st_client=st_client,
                                )
                                if row.get("status") in ("opted_out", "failed"):
                                    skipped += 1
                                else:
                                    sent += 1
                            except Exception:
                                skipped += 1

                        # Update campaign stats
                        with conn.cursor() as cur:
                            cur.execute(
                                """UPDATE sms_campaigns SET
                                   recipient_count=%s, sent_count=%s, completed_at=NOW()
                                   WHERE id=%s""",
                                (len(recipients), sent, campaign_id),
                            )
                        conn.commit()

                st.session_state.pop("sms_preview", None)
                st.success(
                    f"Campaign sent — {sent} sent, {skipped} skipped "
                    f"(campaign_id={campaign_id})."
                )
                st.rerun()
    elif preview:
        st.info("No recipients in pool right now. Try a different filter.")


# ───────────────── Tab 2: Opt-outs ─────────────────
with tab2:
    st.subheader("Opt-out list")
    st.caption("Anyone here is dead to us — every outbound send checks this list "
               "first. STOP keywords from inbounds populate it automatically.")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone, opted_out_at, source, notes
                FROM sms_opt_outs ORDER BY opted_out_at DESC LIMIT 100
            """)
            opt_outs = list(cur.fetchall())

    if opt_outs:
        for o in opt_outs:
            with st.container(border=True):
                cols = st.columns([3, 2, 1])
                cols[0].markdown(
                    f"**📞 {escape(o['phone'])}** · {o['opted_out_at']:%Y-%m-%d %H:%M} · "
                    f"<span style='color:#6B7280;font-size:12px'>{escape(o['source'])}</span>",
                    unsafe_allow_html=True,
                )
                if cols[2].button("🗑 Remove", key=f"rm_{o['phone']}"):
                    with db() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM sms_opt_outs WHERE phone=%s",
                                        (o['phone'],))
                        conn.commit()
                    st.rerun()
    else:
        st.caption("No opt-outs on file.")

    st.divider()
    st.markdown("**Manually add an opt-out:**")
    add_col1, add_col2 = st.columns([2, 1])
    new_phone = add_col1.text_input(
        "Phone (any format)", placeholder="(847) 555-1234",
        label_visibility="collapsed",
    )
    if add_col2.button("➕ Add to opt-outs", disabled=not new_phone.strip()):
        normalized = normalize_phone(new_phone)
        if not normalized:
            st.error("Couldn't parse that phone — needs 10 US digits.")
        else:
            with db() as conn:
                record_opt_out(conn, normalized, source="manual_add")
            st.success(f"Added {normalized} to opt-out list.")
            st.rerun()


# ───────────────── Tab 3: Campaign history ─────────────────
with tab3:
    st.subheader("Past campaigns")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, kind, recipient_count, sent_count, reply_count,
                       started_at, completed_at, dry_run
                FROM sms_campaigns ORDER BY started_at DESC LIMIT 50
            """)
            campaigns = list(cur.fetchall())

    if not campaigns:
        st.caption("No campaigns yet.")
    else:
        for c in campaigns:
            badge = "🧪 DRY" if c["dry_run"] else "🟢 LIVE"
            kind_emoji = {"sleeping": "💤", "reactivation": "♻️"}.get(c["kind"], "📤")
            with st.container(border=True):
                cols = st.columns([3, 2, 2, 2])
                cols[0].markdown(
                    f"**{badge} {kind_emoji} {escape(c['name'])}**<br>"
                    f"<span style='color:#6B7280;font-size:12px'>"
                    f"{c['started_at']:%Y-%m-%d %H:%M}</span>",
                    unsafe_allow_html=True,
                )
                cols[1].metric("Sent", f"{c['sent_count']}/{c['recipient_count']}")
                cols[2].metric("Replies", c["reply_count"])
                if c["completed_at"]:
                    elapsed = (c["completed_at"] - c["started_at"]).total_seconds()
                    cols[3].metric("Duration", f"{int(elapsed)}s")
                else:
                    cols[3].metric("Status", "In progress")
