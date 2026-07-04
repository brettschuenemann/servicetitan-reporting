"""Daily anomaly detector — email Brett if any of (revenue, calls,
memberships signed) is more than 2σ from a 30-day rolling baseline.

Runs each morning. If everything's within normal bounds, exits silently
(no email — we don't need a daily "everything fine" inbox spam). Only
fires when something is genuinely off.

Why 2σ: roughly ~5% of days should fire under normal variability, so
this is sensitive enough to catch real issues without crying wolf every
day. Tunable via the THRESHOLD constant.

Email recipient: EMAIL_TO env var (same as other scripts).
"""
from __future__ import annotations

import os
import statistics
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from lib.database import get_connection
from lib.email_send import send_email


THRESHOLD_SIGMA = 2.0           # alert if |today - mean| > 2σ
BASELINE_DAYS = 30              # rolling window
LOOKBACK_EXCLUDE_RECENT = 1     # baseline excludes the day being checked


# ── metrics ────────────────────────────────────────────────────────

def fetch_daily_revenue(conn, start_date: date, end_date: date) -> dict[date, float]:
    """Daily net revenue by invoice_date. Sums ALL invoices — negative
    invoices are duplicate-billing corrections (e.g. reversed double-billed
    MSPs) and must net against their erroneous positives, matching the
    dashboard methodology that reconciles to the accountant."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT invoice_date AS d, SUM(total)::numeric AS rev
            FROM invoices
            WHERE invoice_date BETWEEN %s AND %s
            GROUP BY invoice_date
            """, (start_date, end_date),
        )
        return {r["d"]: float(r["rev"]) for r in cur.fetchall()}


def fetch_daily_calls(conn, start_date: date, end_date: date) -> dict[date, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT (received_on AT TIME ZONE 'America/Chicago')::date AS d,
                   COUNT(*) AS n
            FROM calls
            WHERE direction = 'Inbound'
              AND (received_on AT TIME ZONE 'America/Chicago')::date BETWEEN %s AND %s
            GROUP BY d
            """, (start_date, end_date),
        )
        return {r["d"]: int(r["n"]) for r in cur.fetchall()}


def fetch_daily_memberships(conn, start_date: date, end_date: date) -> dict[date, int]:
    """New memberships per day (by from_date — when the customer signed up)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT from_date AS d, COUNT(*) AS n
            FROM memberships
            WHERE from_date BETWEEN %s AND %s
            GROUP BY from_date
            """, (start_date, end_date),
        )
        return {r["d"]: int(r["n"]) for r in cur.fetchall()}


# ── analysis ───────────────────────────────────────────────────────

def detect_anomaly(today_val: float, baseline_vals: list[float]) -> dict | None:
    """Return anomaly dict if today is >THRESHOLD_SIGMA from baseline mean,
    else None. Need at least 7 baseline days to compute meaningfully."""
    if len(baseline_vals) < 7:
        return None
    mean = statistics.mean(baseline_vals)
    stdev = statistics.stdev(baseline_vals) if len(baseline_vals) > 1 else 0
    if stdev == 0:
        return None
    z = (today_val - mean) / stdev
    if abs(z) >= THRESHOLD_SIGMA:
        return {
            "today": today_val,
            "mean": mean,
            "stdev": stdev,
            "z": z,
            "direction": "above" if z > 0 else "below",
        }
    return None


# ── main ───────────────────────────────────────────────────────────

def main() -> int:
    today = date.today()
    yesterday = today - timedelta(days=1)  # we evaluate yesterday's data
    baseline_start = yesterday - timedelta(days=BASELINE_DAYS)
    baseline_end   = yesterday - timedelta(days=LOOKBACK_EXCLUDE_RECENT + 1)

    with get_connection() as conn:
        rev_map  = fetch_daily_revenue(conn, baseline_start, yesterday)
        call_map = fetch_daily_calls(conn, baseline_start, yesterday)
        mem_map  = fetch_daily_memberships(conn, baseline_start, yesterday)

    # For each metric: yesterday's value vs the prior baseline window
    metrics_specs = [
        ("Revenue",     "${:,.0f}",  rev_map),
        ("Inbound calls", "{:,.0f}", call_map),
        ("Memberships signed", "{:,.0f}", mem_map),
    ]

    anomalies = []
    for label, fmt, data in metrics_specs:
        today_val = float(data.get(yesterday, 0))
        baseline = [
            float(v) for d, v in data.items()
            if baseline_start <= d <= baseline_end
        ]
        # Days with no rows are 0; include them in baseline
        for offset in range(LOOKBACK_EXCLUDE_RECENT + 1, BASELINE_DAYS + 1):
            d = yesterday - timedelta(days=offset)
            if d not in data:
                baseline.append(0.0)

        result = detect_anomaly(today_val, baseline)
        if result:
            anomalies.append({
                "label": label,
                "fmt": fmt,
                **result,
            })

    if not anomalies:
        print(f"[anomaly_alerts] {yesterday} — all metrics within normal range, "
              "no email sent")
        return 0

    # Compose email
    print(f"[anomaly_alerts] {len(anomalies)} anomalies detected for {yesterday}")
    lines_text = [f"Anomaly alert for {yesterday}\n"]
    lines_html = [f"<h2>📊 Daily anomaly alert — {yesterday}</h2><ul>"]
    for a in anomalies:
        arrow = "⬆️" if a["direction"] == "above" else "⬇️"
        today_disp = a["fmt"].format(a["today"])
        mean_disp = a["fmt"].format(a["mean"])
        delta_pct = ((a["today"] - a["mean"]) / a["mean"] * 100) if a["mean"] else 0
        msg_text = (
            f"{arrow} {a['label']}: {today_disp} "
            f"(baseline avg {mean_disp}, z={a['z']:+.1f}σ, {delta_pct:+.0f}%)"
        )
        msg_html = (
            f"<li><b>{arrow} {a['label']}:</b> {today_disp} "
            f"<span style='color:#6B7280'>(baseline avg {mean_disp}, "
            f"z={a['z']:+.1f}σ, {delta_pct:+.0f}%)</span></li>"
        )
        print(f"  {msg_text}")
        lines_text.append(msg_text)
        lines_html.append(msg_html)

    lines_html.append("</ul>")
    lines_html.append(
        f"<p style='color:#6B7280;font-size:13px'>Baseline = last 30 days "
        f"excluding yesterday. Alert fires when |z| ≥ {THRESHOLD_SIGMA}σ. "
        f"<a href='https://servicetitan.streamlit.app'>Open dashboard</a></p>"
    )

    recipients = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if not recipients:
        print("[anomaly_alerts] EMAIL_TO not set — printing only")
        return 0

    result = send_email(
        to=recipients,
        subject=f"📊 Anomaly: {len(anomalies)} metric(s) off for {yesterday}",
        text="\n".join(lines_text),
        html="\n".join(lines_html),
    )
    print(f"[anomaly_alerts] sent via {result['provider']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
