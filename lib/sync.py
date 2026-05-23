"""Sync ServiceTitan data into the Postgres cache.

Full sync on first run; incremental sync afterward via `modifiedOnOrAfter`.
"""
from __future__ import annotations

import json
from typing import Callable

import psycopg2
from psycopg2.extras import execute_values

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


def _maxstr(a, b):
    if not a:
        return b
    if not b:
        return a
    return max(a, b)


def _date_only(s):
    if not s:
        return None
    return s[:10]


def sync_invoices(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    state = get_sync_state(conn, "invoices")
    since = state["last_modified_on"] if state else None
    progress(f"Fetching invoices{' since ' + since if since else ' (full)'}…")

    invoices = (
        client.get_invoices(modified_after=since) if since else client.get_invoices()
    )
    progress(f"Got {len(invoices)} invoice records. Writing to DB…")

    max_modified = since
    rows = []
    for inv in invoices:
        customer = inv.get("customer") or {}
        bu = inv.get("businessUnit") or {}
        job = inv.get("job") or {}
        modified_on = inv.get("modifiedOn")
        max_modified = _maxstr(max_modified, modified_on)
        rows.append((
            inv["id"],
            _date_only(inv.get("invoiceDate")),
            _date_only(inv.get("dueDate")),
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
            bool(inv.get("active")),
            json.dumps(inv),
        ))

    if rows:
        with conn.cursor() as cur:
            # Batched UPSERT — ~500 rows per round-trip instead of one per row.
            execute_values(
                cur,
                """
                INSERT INTO invoices (
                    id, invoice_date, due_date, created_on, modified_on,
                    total, sub_total, sales_tax, balance, discount_total,
                    customer_id, customer_name, business_unit_id, business_unit_name,
                    job_id, job_number, reference_number, summary, active, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    invoice_date=EXCLUDED.invoice_date, due_date=EXCLUDED.due_date,
                    created_on=EXCLUDED.created_on, modified_on=EXCLUDED.modified_on,
                    total=EXCLUDED.total, sub_total=EXCLUDED.sub_total,
                    sales_tax=EXCLUDED.sales_tax, balance=EXCLUDED.balance,
                    discount_total=EXCLUDED.discount_total,
                    customer_id=EXCLUDED.customer_id, customer_name=EXCLUDED.customer_name,
                    business_unit_id=EXCLUDED.business_unit_id,
                    business_unit_name=EXCLUDED.business_unit_name,
                    job_id=EXCLUDED.job_id, job_number=EXCLUDED.job_number,
                    reference_number=EXCLUDED.reference_number, summary=EXCLUDED.summary,
                    active=EXCLUDED.active, raw=EXCLUDED.raw
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
        conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM invoices")
        total_rows = cur.fetchone()["n"]
    set_sync_state(conn, "invoices", max_modified, total_rows)
    progress(f"Invoices synced. {len(invoices)} touched, {total_rows} total in cache.")
    return {"upserted": len(invoices), "total": total_rows}


def sync_memberships(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    # Memberships endpoint doesn't reliably honor modifiedOn filters, so refresh the
    # membership list in full on every sync. Templates are cached forever.
    progress("Fetching memberships (full refresh)…")
    memberships = client.get_memberships()
    progress(f"Got {len(memberships)} memberships. Writing to DB…")

    mem_rows = [
        (
            m["id"],
            _date_only(m.get("from")),
            _date_only(m.get("to")),
            m.get("createdOn"),
            m.get("modifiedOn"),
            m.get("status"),
            bool(m.get("active")),
            m.get("billingTemplateId"),
            None,  # billing_amount filled in after templates sync
            m.get("billingFrequency"),
            m.get("customerId"),
            m.get("businessUnitId"),
            m.get("membershipTypeId"),
            json.dumps(m),
        )
        for m in memberships
    ]
    if mem_rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO memberships (
                    id, from_date, to_date, created_on, modified_on,
                    status, active, billing_template_id, billing_amount, billing_frequency,
                    customer_id, business_unit_id, membership_type_id, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    from_date=EXCLUDED.from_date, to_date=EXCLUDED.to_date,
                    created_on=EXCLUDED.created_on, modified_on=EXCLUDED.modified_on,
                    status=EXCLUDED.status, active=EXCLUDED.active,
                    billing_template_id=EXCLUDED.billing_template_id,
                    billing_frequency=EXCLUDED.billing_frequency,
                    customer_id=EXCLUDED.customer_id, business_unit_id=EXCLUDED.business_unit_id,
                    membership_type_id=EXCLUDED.membership_type_id, raw=EXCLUDED.raw
                """,
                mem_rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
        conn.commit()

    template_ids = {m["billingTemplateId"] for m in memberships if m.get("billingTemplateId")}
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM membership_templates")
        known = {row["id"] for row in cur.fetchall()}
    needed = sorted(template_ids - known)
    if needed:
        progress(f"Fetching {len(needed)} new billing templates…")
        with conn.cursor() as cur:
            for i, tid in enumerate(needed, 1):
                try:
                    body = client.get_membership_invoice_template(tid)
                    cur.execute(
                        "INSERT INTO membership_templates (id, total, raw) "
                        "VALUES (%s, %s, %s::jsonb) "
                        "ON CONFLICT (id) DO UPDATE SET total=EXCLUDED.total, raw=EXCLUDED.raw",
                        (tid, _safe_float(body.get("total")), json.dumps(body)),
                    )
                except Exception:
                    cur.execute(
                        "INSERT INTO membership_templates (id, total, raw) "
                        "VALUES (%s, %s, %s::jsonb) "
                        "ON CONFLICT (id) DO UPDATE SET total=EXCLUDED.total, raw=EXCLUDED.raw",
                        (tid, 0.0, "{}"),
                    )
                if i % 100 == 0:
                    progress(f"Templates: {i}/{len(needed)}")
                    conn.commit()
        conn.commit()
    else:
        progress("All billing templates already cached.")

    # Propagate template totals into memberships.billing_amount for fast joins
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE memberships m
               SET billing_amount = t.total
              FROM membership_templates t
             WHERE t.id = m.billing_template_id
            """
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM memberships")
        total = cur.fetchone()["n"]
    set_sync_state(conn, "memberships", None, total)
    progress(f"Memberships synced. {total} total in cache, {len(needed)} new templates.")
    return {"memberships": total, "new_templates": len(needed)}


def sync_estimates(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    state = get_sync_state(conn, "estimates")
    since = state["last_modified_on"] if state else None
    progress(f"Fetching estimates{' since ' + since if since else ' (full)'}…")
    estimates = (
        client.get_estimates(modified_after=since) if since else client.get_estimates()
    )
    progress(f"Got {len(estimates)} estimates. Writing to DB…")

    max_modified = since
    rows = []
    for e in estimates:
        status = e.get("status") or {}
        sold_by = e.get("soldBy") or {}
        modified_on = e.get("modifiedOn")
        max_modified = _maxstr(max_modified, modified_on)
        rows.append((
            e["id"],
            status.get("name") if isinstance(status, dict) else status,
            status.get("value") if isinstance(status, dict) else None,
            e.get("name"),
            e.get("summary"),
            _safe_float(e.get("subtotal")),
            _safe_float(e.get("tax")),
            e.get("createdOn"),
            modified_on,
            e.get("soldOn"),
            sold_by.get("id") if isinstance(sold_by, dict) else sold_by,
            e.get("customerId"),
            e.get("locationId"),
            e.get("businessUnitId"),
            e.get("businessUnitName"),
            e.get("jobId"),
            e.get("jobNumber"),
            bool(e.get("active")),
            json.dumps(e),
        ))

    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO estimates (
                    id, status_name, status_value, name, summary, subtotal, tax,
                    created_on, modified_on, sold_on, sold_by_id,
                    customer_id, location_id, business_unit_id, business_unit_name,
                    job_id, job_number, active, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    status_name=EXCLUDED.status_name, status_value=EXCLUDED.status_value,
                    name=EXCLUDED.name, summary=EXCLUDED.summary,
                    subtotal=EXCLUDED.subtotal, tax=EXCLUDED.tax,
                    created_on=EXCLUDED.created_on, modified_on=EXCLUDED.modified_on,
                    sold_on=EXCLUDED.sold_on, sold_by_id=EXCLUDED.sold_by_id,
                    customer_id=EXCLUDED.customer_id, location_id=EXCLUDED.location_id,
                    business_unit_id=EXCLUDED.business_unit_id,
                    business_unit_name=EXCLUDED.business_unit_name,
                    job_id=EXCLUDED.job_id, job_number=EXCLUDED.job_number,
                    active=EXCLUDED.active, raw=EXCLUDED.raw
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM estimates")
        total_rows = cur.fetchone()["n"]
    set_sync_state(conn, "estimates", max_modified, total_rows)
    progress(f"Estimates synced. {len(estimates)} touched, {total_rows} total in cache.")
    return {"upserted": len(estimates), "total": total_rows}


def sync_campaigns(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    progress("Fetching campaigns…")
    campaigns = client.get_campaigns()
    rows = []
    for cmp in campaigns:
        cat = cmp.get("category")
        if isinstance(cat, dict):
            cat = cat.get("name")
        rows.append((
            cmp["id"],
            cmp.get("name"),
            bool(cmp.get("active")),
            bool(cmp.get("isDefaultCampaign")),
            cat,
            cmp.get("source"),
            cmp.get("medium"),
            cmp.get("createdOn"),
            cmp.get("modifiedOn"),
            json.dumps(cmp),
        ))
    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO campaigns (
                    id, name, active, is_default, category, source, medium,
                    created_on, modified_on, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    name=EXCLUDED.name, active=EXCLUDED.active, is_default=EXCLUDED.is_default,
                    category=EXCLUDED.category, source=EXCLUDED.source, medium=EXCLUDED.medium,
                    created_on=EXCLUDED.created_on, modified_on=EXCLUDED.modified_on,
                    raw=EXCLUDED.raw
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
        conn.commit()
    set_sync_state(conn, "campaigns", None, len(campaigns))
    progress(f"Campaigns synced. {len(campaigns)} total.")
    return {"campaigns": len(campaigns)}


def sync_jobs(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    state = get_sync_state(conn, "jobs")
    since = state["last_modified_on"] if state else None
    progress(f"Fetching jobs{' since ' + since if since else ' (full)'}…")
    jobs = client.get_all_jobs(modified_after=since) if since else client.get_all_jobs()
    progress(f"Got {len(jobs)} jobs. Writing to DB…")

    max_modified = since
    rows = []
    for j in jobs:
        modified_on = j.get("modifiedOn")
        max_modified = _maxstr(max_modified, modified_on)
        rows.append((
            j["id"],
            j.get("jobNumber"),
            j.get("jobStatus"),
            j.get("jobTypeId"),
            j.get("campaignId"),
            j.get("businessUnitId"),
            j.get("customerId"),
            j.get("locationId"),
            j.get("invoiceId"),
            j.get("summary"),
            _safe_float(j.get("total")),
            j.get("completedOn"),
            j.get("createdOn"),
            modified_on,
            bool(j.get("noCharge")),
            json.dumps(j),
        ))

    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO jobs (
                    id, job_number, job_status, job_type_id, campaign_id,
                    business_unit_id, customer_id, location_id, invoice_id,
                    summary, total, completed_on, created_on, modified_on,
                    no_charge, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    job_number=EXCLUDED.job_number, job_status=EXCLUDED.job_status,
                    job_type_id=EXCLUDED.job_type_id, campaign_id=EXCLUDED.campaign_id,
                    business_unit_id=EXCLUDED.business_unit_id,
                    customer_id=EXCLUDED.customer_id, location_id=EXCLUDED.location_id,
                    invoice_id=EXCLUDED.invoice_id, summary=EXCLUDED.summary,
                    total=EXCLUDED.total, completed_on=EXCLUDED.completed_on,
                    created_on=EXCLUDED.created_on, modified_on=EXCLUDED.modified_on,
                    no_charge=EXCLUDED.no_charge, raw=EXCLUDED.raw
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM jobs")
        total_rows = cur.fetchone()["n"]
    set_sync_state(conn, "jobs", max_modified, total_rows)
    progress(f"Jobs synced. {len(jobs)} touched, {total_rows} total in cache.")
    return {"upserted": len(jobs), "total": total_rows}


def sync_all(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    inv_stats = sync_invoices(client, conn, progress)
    mem_stats = sync_memberships(client, conn, progress)
    est_stats = sync_estimates(client, conn, progress)
    job_stats = sync_jobs(client, conn, progress)
    cmp_stats = sync_campaigns(client, conn, progress)
    return {
        "invoices": inv_stats,
        "memberships": mem_stats,
        "estimates": est_stats,
        "jobs": job_stats,
        "campaigns": cmp_stats,
    }
