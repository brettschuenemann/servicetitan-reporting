"""Resend setup helper — prints the exact DNS records to add for domain
verification, and a checklist of what to do where.

Usage:
    python3 scripts/resend_setup_helper.py
    python3 scripts/resend_setup_helper.py --domain purecomfort.com

The actual DNS values come from Resend's dashboard after you click
"Add domain". This script doesn't fetch them — it prints the template
so you know what to expect and where to put them.
"""
from __future__ import annotations

import argparse
import sys


GUIDE = """
═══════════════════════════════════════════════════════════════════
 Resend setup — full walkthrough
═══════════════════════════════════════════════════════════════════

WHY: Gmail SMTP keeps failing with WebLoginRequired. Resend is purpose-
built for transactional email and has free 3,000/mo + 100/day limits
(well above Pure Comfort's volume). After this, your 4 daily email
crons stop dying randomly.

──────────────────────────────────────────────
STEP 1 — Sign up (3 min)
──────────────────────────────────────────────
1. Go to https://resend.com/signup
2. Sign in with Google or email — no credit card
3. You're in the dashboard

──────────────────────────────────────────────
STEP 2 — Add + verify the sending domain (10 min)
──────────────────────────────────────────────
Resend will send from a verified domain. Without verification you can
only send FROM onboarding@resend.dev (works but looks unprofessional).

In dashboard:  Settings → Domains → "Add Domain"
  → Enter: {domain}
  → Pick region: us-east-1 (closest to Chicago)

Resend now shows you 3 DNS records. Copy them ALL.

──────────────────────────────────────────────
STEP 3 — Add DNS records at your registrar (5 min)
──────────────────────────────────────────────
Log into your domain registrar (GoDaddy / Cloudflare / Namecheap /
wherever {domain} is registered).

Add each record from Resend's dashboard. They'll look like:

  Type    Host                           Value
  ────    ──────────                     ─────
  MX      send.{domain}                  feedback-smtp.us-east-1.amazonses.com   (priority 10)
  TXT     send.{domain}                  "v=spf1 include:amazonses.com ~all"
  TXT     resend._domainkey.{domain}     <long DKIM string from Resend>

Save. DNS propagation usually takes 1–10 minutes.

Back in Resend dashboard, click "Verify DNS Records" until it goes
green. If it doesn't verify in 30 min, double-check the records
exactly match what Resend showed.

──────────────────────────────────────────────
STEP 4 — Generate an API key (1 min)
──────────────────────────────────────────────
In dashboard:  API Keys → "Create API Key"
  → Name: "Pure Comfort reporting"
  → Permission: "Sending access"  (not full access)
  → Copy the key (starts with `re_`)

──────────────────────────────────────────────
STEP 5 — Configure the app (2 min)
──────────────────────────────────────────────
LOCAL (.env file):
  RESEND_API_KEY=re_<your-key-here>
  EMAIL_FROM=reports@{domain}     # any prefix on the verified domain

GITHUB ACTIONS (repo Settings → Secrets and variables → Actions):
  Add new repository secret:
    Name:  RESEND_API_KEY
    Value: re_<your-key-here>

  Optionally add:
    Name:  EMAIL_FROM
    Value: reports@{domain}

──────────────────────────────────────────────
STEP 6 — Test (30 sec)
──────────────────────────────────────────────
Trigger any of the email scripts manually:

  python3 scripts/send_csr_progress_report.py    # safest — has dry-run logic
  python3 scripts/send_followups_email.py

Look for "Sent via resend." in the output. Check the inbox.

──────────────────────────────────────────────
STEP 7 — Leave Gmail SMTP secrets in place
──────────────────────────────────────────────
The lib/email_send.py helper automatically picks Resend if
RESEND_API_KEY is set, else falls back to Gmail SMTP.

DON'T delete SMTP_USER / SMTP_PASSWORD from GitHub Actions secrets —
they serve as a fallback if you ever lose access to Resend.

═══════════════════════════════════════════════════════════════════
You're done. Next email cron run uses Resend. No more WebLoginRequired.
═══════════════════════════════════════════════════════════════════
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="purecomfort.com",
                        help="Sending domain (default: purecomfort.com)")
    args = parser.parse_args()
    print(GUIDE.format(domain=args.domain))
    return 0


if __name__ == "__main__":
    sys.exit(main())
