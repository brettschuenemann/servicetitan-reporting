"""SQLite cache for ServiceTitan data — schema and connection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "servicetitan.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id                   INTEGER PRIMARY KEY,
    invoice_date         TEXT,
    due_date             TEXT,
    created_on           TEXT,
    modified_on          TEXT,
    total                REAL,
    sub_total            REAL,
    sales_tax            REAL,
    balance              REAL,
    discount_total       REAL,
    customer_id          INTEGER,
    customer_name        TEXT,
    business_unit_id     INTEGER,
    business_unit_name   TEXT,
    job_id               INTEGER,
    job_number           TEXT,
    reference_number     TEXT,
    summary              TEXT,
    active               INTEGER,
    raw                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invoices_invoice_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS ix_invoices_modified_on  ON invoices(modified_on);

CREATE TABLE IF NOT EXISTS memberships (
    id                    INTEGER PRIMARY KEY,
    from_date             TEXT,
    to_date               TEXT,
    created_on            TEXT,
    modified_on           TEXT,
    status                TEXT,
    active                INTEGER,
    billing_template_id   INTEGER,
    billing_amount        REAL,
    billing_frequency     TEXT,
    customer_id           INTEGER,
    business_unit_id      INTEGER,
    membership_type_id    INTEGER,
    raw                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memberships_from_date   ON memberships(from_date);
CREATE INDEX IF NOT EXISTS ix_memberships_modified_on ON memberships(modified_on);

CREATE TABLE IF NOT EXISTS membership_templates (
    id     INTEGER PRIMARY KEY,
    total  REAL,
    raw    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    entity            TEXT PRIMARY KEY,
    last_modified_on  TEXT,
    last_sync_at      TEXT,
    row_count         INTEGER
);
"""


def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection with the schema ensured. Caller owns the lifetime."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_sync_state(conn: sqlite3.Connection, entity: str) -> dict | None:
    row = conn.execute(
        "SELECT entity, last_modified_on, last_sync_at, row_count FROM sync_state WHERE entity = ?",
        (entity,),
    ).fetchone()
    return dict(row) if row else None


def set_sync_state(
    conn: sqlite3.Connection, entity: str, last_modified_on: str | None, row_count: int
) -> None:
    from datetime import datetime, timezone

    conn.execute(
        """
        INSERT INTO sync_state (entity, last_modified_on, last_sync_at, row_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(entity) DO UPDATE SET
            last_modified_on = excluded.last_modified_on,
            last_sync_at     = excluded.last_sync_at,
            row_count        = excluded.row_count
        """,
        (
            entity,
            last_modified_on,
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            row_count,
        ),
    )
    conn.commit()


def db_exists_and_populated() -> bool:
    if not DB_PATH.exists():
        return False
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        return cnt > 0
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
