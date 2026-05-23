"""Streamlit-cached loaders backed by the Postgres cache.

Invoices and memberships come from the database (see `lib/database.py` and
`lib/sync.py`). Jobs and reference data still hit the ServiceTitan API directly.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import streamlit as st
from dotenv import load_dotenv

from .database import db
from .servicetitan import ServiceTitanClient

load_dotenv()

REQUIRED_VARS = ("ST_APP_KEY", "ST_TENANT_ID", "ST_CLIENT_ID", "ST_CLIENT_SECRET")


def _secret(name: str) -> str | None:
    """Pull a config value from st.secrets first, then os.environ."""
    try:
        v = st.secrets.get(name)
        if v:
            return str(v)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass
    return os.environ.get(name)


@st.cache_resource(show_spinner=False)
def get_client() -> ServiceTitanClient:
    missing = [k for k in REQUIRED_VARS if not _secret(k)]
    if missing:
        raise RuntimeError(
            "Missing required credentials: "
            + ", ".join(missing)
            + ". Set them in `.streamlit/secrets.toml` (cloud) or `.env` (local)."
        )
    return ServiceTitanClient(
        app_key=_secret("ST_APP_KEY"),
        tenant_id=_secret("ST_TENANT_ID"),
        client_id=_secret("ST_CLIENT_ID"),
        client_secret=_secret("ST_CLIENT_SECRET"),
        environment=_secret("ST_ENVIRONMENT") or "production",
    )


def _iso(d: date) -> str:
    return d.isoformat() + "T00:00:00Z"


@st.cache_data(ttl=600, show_spinner=False)
def load_jobs(start: date, end: date) -> list[dict]:
    client = get_client()
    return client.get_jobs(
        created_after=_iso(start),
        created_before=_iso(end + timedelta(days=1)),
    )


@st.cache_data(ttl=120, show_spinner=False)
def load_invoices(start: date, end: date) -> list[dict]:
    """Return invoices whose invoiceDate falls in [start, end], from the Postgres cache."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw FROM invoices WHERE invoice_date BETWEEN %s AND %s "
                "ORDER BY invoice_date",
                (start, end),
            )
            rows = cur.fetchall()
    return [r["raw"] for r in rows]  # JSONB comes back already-parsed


@st.cache_data(ttl=3600, show_spinner=False)
def load_business_units() -> dict[int, str]:
    client = get_client()
    return {bu["id"]: bu.get("name", f"BU {bu['id']}") for bu in client.get_business_units()}


@st.cache_data(ttl=3600, show_spinner=False)
def load_technicians() -> dict[int, str]:
    client = get_client()
    return {t["id"]: t.get("name", f"Tech {t['id']}") for t in client.get_technicians()}


@st.cache_data(ttl=120, show_spinner=False)
def load_memberships_with_billing() -> list[dict]:
    """Every membership annotated with `billingAmount`, from the Postgres cache."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT raw, billing_amount FROM memberships")
            rows = cur.fetchall()
    out = []
    for r in rows:
        m = r["raw"]  # JSONB
        m["billingAmount"] = float(r["billing_amount"] or 0)
        out.append(m)
    return out


def membership_revenue_in_range(memberships: list[dict], start: date, end: date) -> float:
    """Sum membership billings whose `from` (activation/renewal) falls in [start, end]."""
    start_s, end_s = start.isoformat(), end.isoformat()
    total = 0.0
    for m in memberships:
        frm = (m.get("from") or "")[:10]
        if frm and start_s <= frm <= end_s:
            total += float(m.get("billingAmount") or 0)
    return total


def memberships_to_monthly_revenue(memberships: list[dict]) -> list[dict]:
    """Flatten memberships into per-row revenue dicts: {date, total} for charting."""
    rows = []
    for m in memberships:
        frm = (m.get("from") or "")[:10]
        amt = float(m.get("billingAmount") or 0)
        if frm and amt:
            rows.append({"date": frm, "total": amt})
    return rows
