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

CREATE TABLE IF NOT EXISTS campaigns (
    id           BIGINT PRIMARY KEY,
    name         TEXT,
    active       BOOLEAN,
    is_default   BOOLEAN,
    category     TEXT,
    source       TEXT,
    medium       TEXT,
    created_on   TIMESTAMPTZ,
    modified_on  TIMESTAMPTZ,
    raw          JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                   BIGINT PRIMARY KEY,
    job_number           TEXT,
    job_status           TEXT,
    job_type_id          BIGINT,
    campaign_id          BIGINT,
    business_unit_id     BIGINT,
    customer_id          BIGINT,
    location_id          BIGINT,
    invoice_id           BIGINT,
    summary              TEXT,
    total                DOUBLE PRECISION,
    completed_on         TIMESTAMPTZ,
    created_on           TIMESTAMPTZ,
    modified_on          TIMESTAMPTZ,
    no_charge            BOOLEAN,
    raw                  JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_completed_on ON jobs(completed_on);
CREATE INDEX IF NOT EXISTS ix_jobs_modified_on  ON jobs(modified_on);
CREATE INDEX IF NOT EXISTS ix_jobs_campaign     ON jobs(campaign_id);
CREATE INDEX IF NOT EXISTS ix_jobs_customer     ON jobs(customer_id);

CREATE TABLE IF NOT EXISTS estimates (
    id                   BIGINT PRIMARY KEY,
    status_name          TEXT,
    status_value         INTEGER,
    name                 TEXT,
    summary              TEXT,
    subtotal             DOUBLE PRECISION,
    tax                  DOUBLE PRECISION,
    created_on           TIMESTAMPTZ,
    modified_on          TIMESTAMPTZ,
    sold_on              TIMESTAMPTZ,
    sold_by_id           BIGINT,
    customer_id          BIGINT,
    location_id          BIGINT,
    business_unit_id     BIGINT,
    business_unit_name   TEXT,
    job_id               BIGINT,
    job_number           TEXT,
    active               BOOLEAN,
    raw                  JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_estimates_status      ON estimates(status_name);
CREATE INDEX IF NOT EXISTS ix_estimates_modified_on ON estimates(modified_on);
CREATE INDEX IF NOT EXISTS ix_estimates_customer    ON estimates(customer_id);

CREATE TABLE IF NOT EXISTS calls (
    id                   BIGINT PRIMARY KEY,
    received_on          TIMESTAMPTZ,
    created_on           TIMESTAMPTZ,
    modified_on          TIMESTAMPTZ,
    direction            TEXT,
    call_type            TEXT,
    duration_seconds     INTEGER,
    from_phone           TEXT,
    to_phone             TEXT,
    agent_id             BIGINT,
    agent_name           TEXT,
    customer_id          BIGINT,
    customer_name        TEXT,
    campaign_id          BIGINT,
    campaign_name        TEXT,
    job_id               BIGINT,
    job_number           TEXT,
    business_unit_id     BIGINT,
    business_unit_name   TEXT,
    recording_url        TEXT,
    voicemail_url        TEXT,
    reason               TEXT,
    raw                  JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_calls_received_on  ON calls(received_on);
CREATE INDEX IF NOT EXISTS ix_calls_modified_on  ON calls(modified_on);
CREATE INDEX IF NOT EXISTS ix_calls_agent        ON calls(agent_id);
CREATE INDEX IF NOT EXISTS ix_calls_customer     ON calls(customer_id);
CREATE INDEX IF NOT EXISTS ix_calls_call_type    ON calls(call_type);

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
