"""Daily email digest of unsold estimates from the rolling last 7 days.

Pulls the freshest estimates from ServiceTitan, finds the ones still Open,
enriches them with phone numbers via the customers/contacts endpoint, and
emails an HTML table to the recipient. Designed for GitHub Actions cron;
also runnable manually with `python scripts/send_followups_email.py`.

Required environment variables:
  ST_APP_KEY, ST_TENANT_ID, ST_CLIENT_ID, ST_CLIENT_SECRET,
  DATABASE_URL, SMTP_USER, SMTP_PASSWORD, EMAIL_TO,
  EMAIL_FROM (optional; defaults to SMTP_USER)
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from datetime import date
from email.message import EmailMessage
from html import escape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from lib.database import db  # noqa: E402
from lib.email_utils import header as fmt_recipients, parse_recipients  # noqa: E402
from lib.servicetitan import ServiceTitanClient  # noqa: E402
from lib.sync import sync_estimates  # noqa: E402

REQUIRED = (
    "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO",
    "ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET",
    "DATABASE_URL",
)
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    sys.exit(f"Missing env vars: {', '.join(missing)}")


def _fmt_phone(p: str | None) -> str:
    if not p:
        return "—"
    digits = "".join(c for c in p if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return p


def main() -> int:
    client = ServiceTitanClient(
        app_key=os.environ["ST_APP_KEY"], tenant_id=os.environ["ST_TENANT_ID"],
        client_id=os.environ["ST_CLIENT_ID"], client_secret=os.environ["ST_CLIENT_SECRET"],
    )

    print("Syncing latest estimates…")
    with db() as conn:
        sync_estimates(client, conn, progress=lambda m: print(f"  · {m}"))

        with conn.cursor() as cur:
            cur.execute(
                """
                WITH cust_name AS (
                  SELECT customer_id, MIN(customer_name) AS name FROM invoices
                  WHERE customer_name IS NOT NULL GROUP BY customer_id
                )
                SELECT
                  e.id, e.subtotal, e.created_on, e.summary,
                  e.business_unit_name, e.job_number, e.customer_id, e.name AS estimate_name,
                  COALESCE(cn.name, 'Customer ' || e.customer_id::text) AS customer_name,
                  EXTRACT(DAY FROM (NOW() - e.created_on))::int AS age_days
                FROM estimates e
                LEFT JOIN cust_name cn ON cn.customer_id = e.customer_id
                WHERE e.status_name = 'Open' AND e.active = TRUE
                  AND e.created_on >= NOW() - INTERVAL '7 days'
                ORDER BY e.subtotal DESC NULLS LAST
                """
            )
            rows = [dict(r) for r in cur.fetchall()]

    print(f"Found {len(rows)} unsold estimates created in the last 7 days.")

    # Enrich with phone numbers — one API call per unique customer
    phone_cache: dict[int, str | None] = {}
    for r in rows:
        cid = r["customer_id"]
        if cid in phone_cache:
            r["phone"] = phone_cache[cid]
            continue
        try:
            contacts = client.get_customer_contacts(cid)
        except Exception as exc:
            print(f"  · contact lookup failed for {cid}: {exc}")
            contacts = []
        # Prefer MobilePhone, then Phone
        ranked = sorted(
            (c for c in contacts if c.get("value") and c.get("type") in ("MobilePhone", "Phone")),
            key=lambda c: 0 if c["type"] == "MobilePhone" else 1,
        )
        phone = ranked[0]["value"] if ranked else None
        phone_cache[cid] = phone
        r["phone"] = phone

    total_value = sum(float(r["subtotal"] or 0) for r in rows)
    today_str = date.today().strftime("%A, %B %d, %Y")
    subject = f"Open estimates — last 7 days — {today_str}"

    # Plain-text body for clients that don't render HTML
    if rows:
        text_lines = [
            f"{r['customer_name'][:32]:32s}  {_fmt_phone(r['phone']):16s}  "
            f"${float(r['subtotal'] or 0):>10,.2f}  {r['age_days']}d  "
            f"{(r['business_unit_name'] or '')[:20]}  {(r['summary'] or '')[:60]}"
            for r in rows
        ]
        text = (
            f"Open estimates created in the last 7 days — {len(rows)} estimates, ${total_value:,.2f} pipeline\n"
            f"As of {today_str}.\n\n"
            + "\n".join(text_lines)
            + "\n"
        )
    else:
        text = f"No open estimates created in the last 7 days. As of {today_str}.\n"

    # HTML body
    if rows:
        html_rows = "".join(
            "<tr>"
            f"<td>{escape(r['customer_name'])}</td>"
            f"<td style='white-space:nowrap'>{escape(_fmt_phone(r['phone']))}</td>"
            f"<td style='text-align:right;white-space:nowrap'>${float(r['subtotal'] or 0):,.2f}</td>"
            f"<td style='text-align:right'>{r['age_days']}d</td>"
            f"<td>{escape(r['business_unit_name'] or '')}</td>"
            f"<td>{escape((r['summary'] or '')[:120])}</td>"
            f"<td>{escape(r['job_number'] or '')}</td>"
            "</tr>"
            for r in rows
        )
        html = f"""<!doctype html>
<html><body style='font-family:Arial,Helvetica,sans-serif;color:#111'>
  <h2 style='margin:0 0 8px'>Open estimates from the last 7 days</h2>
  <p style='margin:0 0 16px;color:#555'>
    <b>{len(rows)}</b> estimates &middot; pipeline <b>${total_value:,.2f}</b> &middot; as of {today_str}.
    Sorted by value, largest first.
  </p>
  <table cellpadding='6' cellspacing='0' style='border-collapse:collapse;border:1px solid #ddd;font-size:14px'>
    <thead style='background:#f5f5f5'>
      <tr>
        <th align='left'>Customer</th>
        <th align='left'>Phone</th>
        <th align='right'>Value</th>
        <th align='right'>Age</th>
        <th align='left'>Business unit</th>
        <th align='left'>Summary</th>
        <th align='left'>Job #</th>
      </tr>
    </thead>
    <tbody>{html_rows}</tbody>
  </table>
  <p style='color:#888;font-size:12px;margin-top:16px'>
    Generated automatically from ServiceTitan via the reporting dashboard.
  </p>
</body></html>"""
    else:
        html = (
            "<!doctype html><html><body style='font-family:Arial,Helvetica,sans-serif'>"
            f"<p>No open estimates created in the last 7 days. As of {today_str}.</p>"
            "</body></html>"
        )

    recipients = parse_recipients(os.environ["EMAIL_TO"])
    if not recipients:
        sys.exit("EMAIL_TO has no valid addresses.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = fmt_recipients(recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    print(f"Connecting to Gmail SMTP and sending to {fmt_recipients(recipients)}…")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg, to_addrs=recipients)
    print("Sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
