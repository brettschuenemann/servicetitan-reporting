"""Shared helpers for the email-sending scripts.

Centralizes parsing of multi-recipient env vars + idempotency guards
for retry crons. `was_sent_recently` lets a retry cron skip when the
primary already succeeded; `record_send` logs each successful send.
"""
from __future__ import annotations

import os


def parse_recipients(value: str | None) -> list[str]:
    """Split a comma- or semicolon-separated address list into clean entries.

    Trims whitespace, drops empties, and deduplicates case-insensitively
    while preserving the first-seen casing. Safe to call on missing env vars
    (None returns []).
    """
    if not value:
        return []
    raw = [a.strip() for a in value.replace(";", ",").split(",")]
    seen: set[str] = set()
    out: list[str] = []
    for addr in raw:
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def header(addrs: list[str]) -> str:
    """Join recipients for a To/Cc header (comma-space-separated)."""
    return ", ".join(addrs)


def exclude(addrs: list[str], remove_set: list[str]) -> list[str]:
    """Return addrs minus any entry present in remove_set (case-insensitive)."""
    removed = {a.lower() for a in remove_set}
    return [a for a in addrs if a.lower() not in removed]


def trigger_source() -> str:
    """How this script was invoked. Used to gate idempotency dedup."""
    return os.environ.get("GITHUB_EVENT_NAME") or "cli"


def was_sent_recently(conn, kind: str, hours: int = 6) -> bool:
    """True if a successful send of `kind` was logged in the last N hours."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM email_sends "
            "WHERE kind = %s AND sent_at >= NOW() - %s * INTERVAL '1 hour') AS hit",
            (kind, hours),
        )
        return bool(cur.fetchone()["hit"])


def record_send(conn, kind: str, recipients: list[str]) -> None:
    """Log a successful send so retry crons skip the duplicate."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO email_sends (kind, recipients, triggered_by) "
            "VALUES (%s, %s, %s)",
            (kind, ", ".join(recipients), trigger_source()),
        )
        conn.commit()


def should_skip_for_retry(conn, kind: str, hours: int = 6) -> bool:
    """Convenience: skip the send if scheduled AND already sent recently.
    Manual workflow_dispatch / CLI runs always send."""
    if trigger_source() != "schedule":
        return False
    return was_sent_recently(conn, kind, hours=hours)
