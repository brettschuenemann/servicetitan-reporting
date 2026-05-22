"""Sync ServiceTitan data into the local SQLite cache.

Full sync on first run; incremental sync afterward via `modifiedOnOrAfter`.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable, Iterator

from .database import get_sync_state, set_sync_state
from .servicetitan import ServiceTitanClient

ProgressCallback = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _maxstr(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return max(a, b)


def sync_invoices(
    client: ServiceTitanClient, conn: sqlite3.Connection, progress: ProgressCallback = _noop
) -> dict:
    state = get_sync_state(conn, "invoices")
    since = state["last_modified_on"] if state else None
    progress(f"Fetching invoices{' since ' + since if since else ' (full)'}…")

    invoices = (
        client.get_invoices(modified_after=since) if since else client.get_invoices()
    )
    progress(f"Got {len(invoices)} invoice records. Writing to DB…")

    max_modified = since
    cur = conn.cursor()
    for inv in invoices:
        customer = inv.get("customer") or {}
        bu = inv.get("businessUnit") or {}
        job = inv.get("job") or {}
        modified_on = inv.get("modifiedOn")
        max_modified = _maxstr(max_modified, modified_on)
        cur.execute(
            """
            INSERT INTO invoices (
                id, invoice_date, due_date, created_on, modified_on,
                total, sub_total, sales_tax, balance, discount_total,
                customer_id, customer_name, business_unit_id, business_unit_name,
                job_id, job_number, reference_number, summary, active, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                invoice_date=excluded.invoice_date, due_date=excluded.due_date,
                created_on=excluded.created_on, modified_on=excluded.modified_on,
                total=excluded.total, sub_total=excluded.sub_total,
                sales_tax=excluded.sales_tax, balance=excluded.balance,
                discount_total=excluded.discount_total,
                customer_id=excluded.customer_id, customer_name=excluded.customer_name,
                business_unit_id=excluded.business_unit_id, business_unit_name=excluded.business_unit_name,
                job_id=excluded.job_id, job_number=excluded.job_number,
                reference_number=excluded.reference_number, summary=excluded.summary,
                active=excluded.active, raw=excluded.raw
            """,
            (
                inv["id"],
                (inv.get("invoiceDate") or "")[:10] or None,
                (inv.get("dueDate") or "")[:10] or None,
                inv.get("createdOn"),
                modified_on,
                _safe_float(inv.get("total")),
                _safe_float(inv.get("subTotal")),
                _safe_float(inv.get("salesTax")),
                _safe_float(inv.get("balance")),
                _safe_float(inv.get("discountTotal")),
                customer.get("id"),
                customer.get("name"),
                bu.get("id"),
                bu.get("name"),
                job.get("id"),
                job.get("number"),
                inv.get("referenceNumber"),
                inv.get("summary"),
                1 if inv.get("active") else 0,
                json.dumps(inv),
            ),
        )
    conn.commit()
    total_rows = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    set_sync_state(conn, "invoices", max_modified, total_rows)
    progress(f"Invoices synced. {len(invoices)} touched, {total_rows} total in cache.")
    return {"upserted": len(invoices), "total": total_rows}


def sync_memberships(
    client: ServiceTitanClient, conn: sqlite3.Connection, progress: ProgressCallback = _noop
) -> dict:
    # Memberships endpoint doesn't reliably honor modifiedOn filters across all tenants,
    # so refresh the membership list in full on every sync. The expensive piece is
    # template lookups, which we cache permanently below.
    progress("Fetching memberships (full refresh)…")
    memberships = client.get_memberships()
    progress(f"Got {len(memberships)} memberships. Writing to DB…")

    cur = conn.cursor()
    for m in memberships:
        cur.execute(
            """
            INSERT INTO memberships (
                id, from_date, to_date, created_on, modified_on,
                status, active, billing_template_id, billing_amount, billing_frequency,
                customer_id, business_unit_id, membership_type_id, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                from_date=excluded.from_date, to_date=excluded.to_date,
                created_on=excluded.created_on, modified_on=excluded.modified_on,
                status=excluded.status, active=excluded.active,
                billing_template_id=excluded.billing_template_id,
                billing_frequency=excluded.billing_frequency,
                customer_id=excluded.customer_id, business_unit_id=excluded.business_unit_id,
                membership_type_id=excluded.membership_type_id, raw=excluded.raw
            """,
            (
                m["id"],
                (m.get("from") or "")[:10] or None,
                (m.get("to") or "")[:10] or None,
                m.get("createdOn"),
                m.get("modifiedOn"),
                m.get("status"),
                1 if m.get("active") else 0,
                m.get("billingTemplateId"),
                None,  # billing_amount filled in by sync_membership_templates
                m.get("billingFrequency"),
                m.get("customerId"),
                m.get("businessUnitId"),
                m.get("membershipTypeId"),
                json.dumps(m),
            ),
        )
    conn.commit()

    # Fetch each unfetched billing template; templates rarely change so we cache forever.
    template_ids = {
        m["billingTemplateId"] for m in memberships if m.get("billingTemplateId")
    }
    known = {row["id"] for row in conn.execute("SELECT id FROM membership_templates")}
    needed = sorted(template_ids - known)
    if needed:
        progress(f"Fetching {len(needed)} new billing templates…")
        for i, tid in enumerate(needed, 1):
            try:
                body = client.get_membership_invoice_template(tid)
                cur.execute(
                    "INSERT OR REPLACE INTO membership_templates (id, total, raw) VALUES (?, ?, ?)",
                    (tid, _safe_float(body.get("total")), json.dumps(body)),
                )
            except Exception:
                cur.execute(
                    "INSERT OR REPLACE INTO membership_templates (id, total, raw) VALUES (?, ?, ?)",
                    (tid, 0.0, "{}"),
                )
            if i % 100 == 0:
                progress(f"Templates: {i}/{len(needed)}")
                conn.commit()
        conn.commit()
    else:
        progress("All billing templates already cached.")

    # Propagate template totals into memberships.billing_amount for fast joins
    cur.execute(
        """
        UPDATE memberships
           SET billing_amount = (
               SELECT total FROM membership_templates WHERE id = memberships.billing_template_id
           )
        """
    )
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM memberships").fetchone()[0]
    set_sync_state(conn, "memberships", None, total)
    progress(f"Memberships synced. {total} total in cache, {len(needed)} new templates.")
    return {"memberships": total, "new_templates": len(needed)}


def sync_all(
    client: ServiceTitanClient, conn: sqlite3.Connection, progress: ProgressCallback = _noop
) -> dict:
    inv_stats = sync_invoices(client, conn, progress)
    mem_stats = sync_memberships(client, conn, progress)
    return {"invoices": inv_stats, "memberships": mem_stats}
