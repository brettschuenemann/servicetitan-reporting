"""Shared helpers for the email-sending scripts.

Centralizes parsing of multi-recipient env vars so every script handles
`EMAIL_TO=alice@x.com, bob@x.com` (or semicolon-separated) the same way.
"""
from __future__ import annotations


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
