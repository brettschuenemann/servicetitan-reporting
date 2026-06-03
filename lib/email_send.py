"""Email send abstraction — Resend primary, Gmail SMTP fallback.

Pure Comfort's daily emails (CSR list, weekly summary, followups, progress
report) all funneled through Gmail SMTP, which kept failing due to
Google's consumer-account security policies. This wraps Resend's API as
the primary path and keeps Gmail SMTP as a fallback so nothing breaks
during the migration.

Selection logic:
  - If RESEND_API_KEY is set → use Resend
  - Else if SMTP_USER + SMTP_PASSWORD set → use Gmail SMTP (legacy)
  - Else → raise RuntimeError

Usage:
    from lib.email_send import send_email
    send_email(
        to=["fey@purecomfort.com", "brett@purecomfort.com"],
        subject="Daily CSR list — 2026-06-03",
        text="plain text version...",
        html="<p>HTML version...</p>",
        from_email="csr-reports@purecomfort.com",  # optional
    )

Resend setup (one-time):
    1. Sign up at resend.com (3,000 emails/mo free)
    2. Add + verify your sending domain (purecomfort.com)
       — Adds 3 DNS records (SPF, DKIM, MX). Takes ~10 min.
    3. Generate an API key from the dashboard
    4. Set RESEND_API_KEY in .env + GitHub Actions secrets
    5. Set EMAIL_FROM to your verified sender (e.g. reports@purecomfort.com)

Until the domain is verified, send-from is locked to onboarding@resend.dev
which still works for testing.
"""
from __future__ import annotations

import os
import ssl
from typing import Iterable, Optional


def send_email(
    *,
    to: list[str] | str,
    subject: str,
    text: str,
    html: Optional[str] = None,
    from_email: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> dict:
    """Send an email. Returns provider response dict.

    `to` accepts a list of addresses OR a single comma-separated string.
    """
    if isinstance(to, str):
        to_list = [a.strip() for a in to.split(",") if a.strip()]
    else:
        to_list = list(to)
    if not to_list:
        raise ValueError("send_email: 'to' must contain at least one address")

    from_email = (
        from_email
        or os.environ.get("EMAIL_FROM")
        or os.environ.get("SMTP_USER")
        or "onboarding@resend.dev"
    )

    if os.environ.get("RESEND_API_KEY"):
        return _send_via_resend(
            to_list, subject, text, html, from_email, reply_to,
        )

    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        return _send_via_gmail(
            to_list, subject, text, html, from_email, reply_to,
        )

    raise RuntimeError(
        "No email provider configured. Set RESEND_API_KEY "
        "(preferred) or SMTP_USER + SMTP_PASSWORD."
    )


# ── Resend ────────────────────────────────────────────────────────

def _send_via_resend(
    to: list[str],
    subject: str,
    text: str,
    html: Optional[str],
    from_email: str,
    reply_to: Optional[str],
) -> dict:
    try:
        import resend  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "resend package not installed — add `resend` to requirements.txt"
        ) from exc

    resend.api_key = os.environ["RESEND_API_KEY"]

    payload: dict = {
        "from": from_email,
        "to": to,
        "subject": subject,
    }
    if text:
        payload["text"] = text
    if html:
        payload["html"] = html
    if reply_to:
        payload["reply_to"] = [reply_to]

    response = resend.Emails.send(payload)  # raises on failure
    return {"provider": "resend", "id": response.get("id"), "raw": response}


# ── Gmail SMTP (fallback / legacy) ────────────────────────────────

def _send_via_gmail(
    to: list[str],
    subject: str,
    text: str,
    html: Optional[str],
    from_email: str,
    reply_to: Optional[str],
) -> dict:
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(
        "smtp.gmail.com", 465, context=ssl.create_default_context()
    ) as smtp:
        smtp.login(
            os.environ["SMTP_USER"],
            os.environ["SMTP_PASSWORD"],
        )
        smtp.send_message(msg, to_addrs=to)
    return {"provider": "gmail_smtp", "id": None}
