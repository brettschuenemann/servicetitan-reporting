"""Postgres cache for ServiceTitan data — schema and connection helpers.

Uses psycopg2 with RealDictCursor so all rows are dict-like. The schema is
applied lazily on each new connection (cheap thanks to IF NOT EXISTS).

DATABASE_URL is read from st.secrets first, then os.environ. Recommended provider:
Neon (neon.tech) — serverless Postgres, free tier is plenty for this workload.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

import psycopg2
import psycopg2.extras
import streamlit as st

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id                   BIGINT PRIMARY KEY,
    invoice_date         DATE,
    due_date             DATE,
    created_on           TIMESTAMPTZ,
    modified_on          TIMESTAMPTZ,
    total                DOUBLE PRECISION,
    sub_total            DOUBLE PRECISION,
    sales_tax            DOUBLE PRECISION,
    balance              DOUBLE PRECISION,
    discount_total       DOUBLE PRECISION,
    customer_id          BIGINT,
    customer_name        TEXT,
    business_unit_id     BIGINT,
    business_unit_name   TEXT,
    job_id               BIGINT,
    job_number           TEXT,
    reference_number     TEXT,
    summary              TEXT,
    active               BOOLEAN,
    raw                  JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invoices_invoice_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS ix_invoices_modified_on  ON invoices(modified_on);

CREATE TABLE IF NOT EXISTS memberships (
    id                    BIGINT PRIMARY KEY,
    from_date             DATE,
    to_date               DATE,
    created_on            TIMESTAMPTZ,
    modified_on           TIMESTAMPTZ,
    status                TEXT,
    active                BOOLEAN,
    billing_template_id   BIGINT,
    billing_amount        DOUBLE PRECISION,
    billing_frequency     TEXT,
    customer_id           BIGINT,
    business_unit_id      BIGINT,
    membership_type_id    BIGINT,
    raw                   JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memberships_from_date   ON memberships(from_date);
CREATE INDEX IF NOT EXISTS ix_memberships_modified_on ON memberships(modified_on);

CREATE TABLE IF NOT EXISTS membership_templates (
    id     BIGINT PRIMARY KEY,
    total  DOUBLE PRECISION,
    raw    JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    entity            TEXT PRIMARY KEY,
    last_modified_on  TEXT,
    last_sync_at      TEXT,
    row_count         INTEGER
);
"""


def _database_url() -> str:
    try:
        url = st.secrets.get("DATABASE_URL")
        if url:
            return str(url)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to .env (local) or "
            ".streamlit/secrets.toml (cloud). Get a free one from neon.tech."
        )
    return url


def get_connection() -> psycopg2.extensions.connection:
    """Open a new Postgres connection with the schema applied. Caller owns the lifetime."""
    conn = psycopg2.connect(_database_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def db() -> Iterator[psycopg2.extensions.connection]:
    """Context manager that opens, yields, and closes a connection."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def get_sync_state(conn, entity: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity, last_modified_on, last_sync_at, row_count "
            "FROM sync_state WHERE entity = %s",
            (entity,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def set_sync_state(conn, entity: str, last_modified_on: Optional[str], row_count: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (entity, last_modified_on, last_sync_at, row_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity) DO UPDATE SET
                last_modified_on = EXCLUDED.last_modified_on,
                last_sync_at     = EXCLUDED.last_sync_at,
                row_count        = EXCLUDED.row_count
            """,
            (entity, last_modified_on, now, row_count),
        )
    conn.commit()


def db_populated() -> bool:
    """Cheap check: does the invoices table have any rows?"""
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM invoices")
                row = cur.fetchone()
                return bool(row and row["n"] > 0)
    except Exception:
        return False
