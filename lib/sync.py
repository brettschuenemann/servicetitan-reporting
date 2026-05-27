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


def _extract_invoice_item_rows(invoice_id: int, raw: dict) -> list[tuple]:
    """Flatten an invoice's `items` array into rows for invoice_items.

    ServiceTitan's item payload is inconsistent — `type` can live at the top
    level or inside `skuType`; `totalCost` may be null and need to be derived
    from `cost * quantity`. We accept whatever's there and zero-fill the rest.
    """
    out: list[tuple] = []
    for item in (raw.get("items") or []):
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not item_id:
            continue
        qty = _safe_float(item.get("quantity"))
        cost = _safe_float(item.get("cost"))
        total_cost = item.get("totalCost")
        if total_cost is None:
            total_cost = cost * qty
        item_type = item.get("type") or item.get("skuType")
        if isinstance(item_type, dict):
            item_type = item_type.get("name")
        out.append((
            int(item_id),
            int(invoice_id),
            item.get("skuId"),
            item.get("skuName"),
            item.get("description"),
            qty,
            cost,
            _safe_float(total_cost),
            _safe_float(item.get("price")),
            _safe_float(item.get("total")),
            item_type,
            json.dumps(item),
        ))
    return out


def _write_invoice_items(
    conn: psycopg2.extensions.connection,
    invoice_ids: list[int],
    item_rows: list[tuple],
) -> None:
    """Replace all items for the given invoices in one transaction."""
    if not invoice_ids:
        return
    with conn.cursor() as cur:
        # Delete-and-reinsert is the simplest way to handle items added/removed
        # from an invoice between syncs. The set is bounded by the invoice batch.
        cur.execute(
            "DELETE FROM invoice_items WHERE invoice_id = ANY(%s)",
            (invoice_ids,),
        )
        if item_rows:
            execute_values(
                cur,
                """
                INSERT INTO invoice_items (
                    id, invoice_id, sku_id, sku_name, description,
                    quantity, cost, total_cost, price, total, item_type, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    invoice_id=EXCLUDED.invoice_id, sku_id=EXCLUDED.sku_id,
                    sku_name=EXCLUDED.sku_name, description=EXCLUDED.description,
                    quantity=EXCLUDED.quantity, cost=EXCLUDED.cost,
                    total_cost=EXCLUDED.total_cost, price=EXCLUDED.price,
                    total=EXCLUDED.total, item_type=EXCLUDED.item_type,
                    raw=EXCLUDED.raw
                """,
                item_rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
    conn.commit()


def backfill_invoice_items_from_raw(
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
    chunk: int = 1000,
) -> dict:
    """One-shot: read all invoices.raw and (re)write invoice_items.

    Safe to run anytime — uses delete-then-insert per invoice batch so it's
    idempotent. Used to bootstrap the items table from invoices already cached
    before items-sync was wired in.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM invoices")
        total_invoices = cur.fetchone()["n"]
    progress(f"Backfilling invoice_items from {total_invoices:,} cached invoices…")

    processed = 0
    items_written = 0
    offset = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, raw FROM invoices ORDER BY id LIMIT %s OFFSET %s",
                (chunk, offset),
            )
            batch = cur.fetchall()
        if not batch:
            break
        invoice_ids: list[int] = []
        item_rows: list[tuple] = []
        for r in batch:
            invoice_ids.append(int(r["id"]))
            item_rows.extend(_extract_invoice_item_rows(int(r["id"]), r["raw"]))
        _write_invoice_items(conn, invoice_ids, item_rows)
        processed += len(batch)
        items_written += len(item_rows)
        offset += chunk
        progress(f"Backfill: {processed:,}/{total_invoices:,} invoices processed, {items_written:,} items written")

    set_sync_state(conn, "invoice_items", None, items_written)
    progress(f"Backfill complete. {items_written:,} line items written.")
    return {"invoices_scanned": processed, "items_written": items_written}


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

        # Also (re)write line items for the touched invoices.
        invoice_ids = [int(inv["id"]) for inv in invoices]
        item_rows: list[tuple] = []
        for inv in invoices:
            item_rows.extend(_extract_invoice_item_rows(int(inv["id"]), inv))
        _write_invoice_items(conn, invoice_ids, item_rows)
        progress(f"Invoice items synced: {len(item_rows):,} line items for {len(invoice_ids):,} invoices.")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM invoices")
        total_rows = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM invoice_items")
        items_total = cur.fetchone()["n"]
    set_sync_state(conn, "invoices", max_modified, total_rows)
    set_sync_state(conn, "invoice_items", None, items_total)
    progress(f"Invoices synced. {len(invoices)} touched, {total_rows} total in cache.")
    return {"upserted": len(invoices), "total": total_rows, "items_total": items_total}


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


def _parse_duration_to_seconds(d: str | None) -> int | None:
    """HH:MM:SS string → integer seconds. Returns None if unparseable."""
    if not d:
        return None
    parts = d.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def sync_calls(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    state = get_sync_state(conn, "calls")
    since = state["last_modified_on"] if state else None
    progress(f"Fetching calls{' since ' + since if since else ' (full)'}…")
    # Full sync uses createdAfter = epoch; incremental uses modifiedAfter
    if since:
        calls = client.get_calls(modified_after=since)
    else:
        calls = client.get_calls(created_after="2020-01-01T00:00:00Z")
    progress(f"Got {len(calls)} call records. Writing to DB…")

    max_modified = since
    rows = []
    for call in calls:
        lc = call.get("leadCall") or {}
        agent = lc.get("agent") or {}
        customer = lc.get("customer") or {}
        campaign = lc.get("campaign") or {}
        bu = call.get("businessUnit") or {}
        reason = lc.get("reason")
        reason_name = reason.get("name") if isinstance(reason, dict) else reason
        modified_on = lc.get("modifiedOn") or call.get("modifiedOn")
        max_modified = _maxstr(max_modified, modified_on)
        wrapper_id = call.get("id")
        job_id = wrapper_id if (call.get("jobNumber") and wrapper_id) else None
        rows.append((
            lc.get("id") or wrapper_id,
            lc.get("receivedOn"),
            lc.get("createdOn"),
            modified_on,
            lc.get("direction"),
            lc.get("callType"),
            _parse_duration_to_seconds(lc.get("duration")),
            lc.get("from"),
            lc.get("to"),
            agent.get("id"),
            agent.get("name"),
            customer.get("id"),
            customer.get("name"),
            campaign.get("id"),
            campaign.get("name"),
            job_id,
            call.get("jobNumber"),
            bu.get("id"),
            bu.get("name"),
            lc.get("recordingUrl"),
            lc.get("voiceMailUrl"),
            reason_name,
            json.dumps(call),
        ))

    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO calls (
                    id, received_on, created_on, modified_on,
                    direction, call_type, duration_seconds, from_phone, to_phone,
                    agent_id, agent_name, customer_id, customer_name,
                    campaign_id, campaign_name, job_id, job_number,
                    business_unit_id, business_unit_name,
                    recording_url, voicemail_url, reason, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    received_on=EXCLUDED.received_on, created_on=EXCLUDED.created_on,
                    modified_on=EXCLUDED.modified_on,
                    direction=EXCLUDED.direction, call_type=EXCLUDED.call_type,
                    duration_seconds=EXCLUDED.duration_seconds,
                    from_phone=EXCLUDED.from_phone, to_phone=EXCLUDED.to_phone,
                    agent_id=EXCLUDED.agent_id, agent_name=EXCLUDED.agent_name,
                    customer_id=EXCLUDED.customer_id, customer_name=EXCLUDED.customer_name,
                    campaign_id=EXCLUDED.campaign_id, campaign_name=EXCLUDED.campaign_name,
                    job_id=EXCLUDED.job_id, job_number=EXCLUDED.job_number,
                    business_unit_id=EXCLUDED.business_unit_id,
                    business_unit_name=EXCLUDED.business_unit_name,
                    recording_url=EXCLUDED.recording_url, voicemail_url=EXCLUDED.voicemail_url,
                    reason=EXCLUDED.reason, raw=EXCLUDED.raw
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM calls")
        total = cur.fetchone()["n"]
    set_sync_state(conn, "calls", max_modified, total)
    progress(f"Calls synced. {len(calls)} touched, {total} total in cache.")
    return {"upserted": len(calls), "total": total}


def sync_appointment_assignments(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    state = get_sync_state(conn, "appointment_assignments")
    since = state["last_modified_on"] if state else None
    progress(f"Fetching appointment-assignments{' since ' + since if since else ' (full)'}…")
    assigns = client.get_appointment_assignments(modified_after=since)
    progress(f"Got {len(assigns)} assignments. Writing to DB…")

    max_modified = since
    rows = []
    for a in assigns:
        modified_on = a.get("modifiedOn")
        max_modified = _maxstr(max_modified, modified_on)
        rows.append((
            a["id"],
            a.get("technicianId"),
            a.get("technicianName"),
            a.get("jobId"),
            a.get("appointmentId"),
            a.get("assignedOn"),
            a.get("status"),
            bool(a.get("isPaused")),
            bool(a.get("active")),
            a.get("createdOn"),
            modified_on,
            json.dumps(a),
        ))

    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO appointment_assignments (
                    id, technician_id, technician_name, job_id, appointment_id,
                    assigned_on, status, is_paused, active,
                    created_on, modified_on, raw
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    technician_id=EXCLUDED.technician_id,
                    technician_name=EXCLUDED.technician_name,
                    job_id=EXCLUDED.job_id, appointment_id=EXCLUDED.appointment_id,
                    assigned_on=EXCLUDED.assigned_on, status=EXCLUDED.status,
                    is_paused=EXCLUDED.is_paused, active=EXCLUDED.active,
                    created_on=EXCLUDED.created_on, modified_on=EXCLUDED.modified_on,
                    raw=EXCLUDED.raw
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                page_size=500,
            )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM appointment_assignments")
        total = cur.fetchone()["n"]
    set_sync_state(conn, "appointment_assignments", max_modified, total)
    progress(f"Appointment-assignments synced. {len(assigns)} touched, {total} total.")
    return {"upserted": len(assigns), "total": total}


def sync_for_email(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    """Best-effort incremental sync used right before sending an email
    and by the hourly background cron.

    Pulls everything modified since the last sync (handled internally by
    each entity's sync_state). Failures are logged but don't raise — we
    always want the email to go out, even if ST is temporarily unhappy.
    Email runs against whatever data was successfully fetched.

    Last step pre-warms `customer_contacts` for Call List candidates so
    the page never has to hit ST for phone/email on cold loads.
    """
    results: dict[str, dict] = {}
    steps = [
        ("invoices", sync_invoices),
        ("estimates", sync_estimates),
        ("jobs", sync_jobs),
        ("campaigns", sync_campaigns),
        ("calls", sync_calls),
        ("appointment_assignments", sync_appointment_assignments),
        ("memberships", sync_memberships),
        ("call_list_contacts", sync_call_list_contacts),
        ("call_list_openers", sync_call_list_openers),
    ]
    for name, fn in steps:
        try:
            results[name] = fn(client, conn, progress)
        except Exception as exc:
            progress(f"sync {name} failed (non-fatal): {exc}")
            results[name] = {"error": str(exc)}
    return results


def sync_call_list_contacts(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    """Pre-fetch phone/email for everyone who might appear on the Call List.

    The Call List page reads contacts from `customer_contacts`. If we keep
    that table warm, cold page loads stay near-instant instead of paying
    300ms-per-customer × 40 customers for ST contact lookups.

    Candidate universe:
      - install / sales-tagged invoices in last 180 days (membership opps)
      - inbound calls in last 30 days (missed-call follow-ups)
      - open estimates (estimate follow-ups)
      - customers with paid invoices 6-24 months ago (sleeping)

    Refresh policy: any contact >7 days old gets re-fetched. Contacts on
    ServiceTitan change rarely (homeowners don't swap phone numbers), so
    a weekly refresh is plenty.
    """
    from concurrent.futures import ThreadPoolExecutor

    progress("Identifying Call List contact candidates…")
    with conn.cursor() as cur:
        cur.execute(
            """
            -- Union of customers from each of the four call-list source
            -- queries. LIMIT inside each subquery keeps cron time bounded
            -- if any single source explodes.
            WITH candidates AS (
              SELECT DISTINCT customer_id FROM invoices
              WHERE invoice_date >= CURRENT_DATE - INTERVAL '180 day'
                AND customer_id IS NOT NULL

              UNION
              SELECT DISTINCT customer_id FROM calls
              WHERE received_on >= NOW() - INTERVAL '30 day'
                AND customer_id IS NOT NULL

              UNION
              SELECT DISTINCT customer_id FROM estimates
              WHERE status_name = 'Open' AND active = TRUE
                AND customer_id IS NOT NULL

              UNION
              SELECT DISTINCT customer_id FROM invoices
              WHERE invoice_date BETWEEN CURRENT_DATE - INTERVAL '24 month'
                                     AND CURRENT_DATE - INTERVAL '6 month'
                AND customer_id IS NOT NULL
            )
            SELECT c.customer_id
            FROM candidates c
            LEFT JOIN customer_contacts cc
              ON cc.customer_id = c.customer_id
             AND cc.fetched_at > NOW() - INTERVAL '7 day'
            WHERE cc.customer_id IS NULL
            """
        )
        to_fetch = [int(r["customer_id"]) for r in cur.fetchall()]

    if not to_fetch:
        progress("Contacts already fresh — nothing to fetch.")
        return {"fetched": 0, "skipped_fresh": "all"}

    progress(f"Fetching contacts for {len(to_fetch)} customers (parallel)…")

    # Lazy import to avoid circular deps
    from scripts.send_csr_daily_email import fetch_customer_contacts_full

    with ThreadPoolExecutor(max_workers=12) as ex:
        fetched = list(ex.map(
            lambda c: (c, fetch_customer_contacts_full(client, c)),
            to_fetch,
        ))

    # Persist primary phone + email (used for display on Call List / Todo)
    progress(f"Persisting primary contacts for {len(fetched)} customers…")
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO customer_contacts (customer_id, phone, email) VALUES %s "
            "ON CONFLICT (customer_id) DO UPDATE SET "
            "  phone = EXCLUDED.phone, "
            "  email = EXCLUDED.email, "
            "  fetched_at = NOW()",
            [(cid, full["primary_phone"], full["primary_email"])
             for cid, full in fetched],
        )

        # Persist EVERY phone (used by conversion-analytics reverse lookup)
        phone_rows = []
        for cid, full in fetched:
            for p in full["all_phones"]:
                phone_rows.append(
                    (cid, p["normalized"], p["raw"], p["kind"])
                )
        if phone_rows:
            execute_values(
                cur,
                "INSERT INTO customer_phones "
                "  (customer_id, normalized_phone, raw_phone, kind) VALUES %s "
                "ON CONFLICT (customer_id, normalized_phone) DO UPDATE SET "
                "  raw_phone  = EXCLUDED.raw_phone, "
                "  kind       = EXCLUDED.kind, "
                "  fetched_at = NOW()",
                phone_rows,
            )
            progress(f"Persisted {len(phone_rows)} phone numbers across "
                     f"{len(fetched)} customers.")
    conn.commit()
    return {
        "fetched": len(fetched),
        "candidates": len(to_fetch),
        "phone_rows": len(phone_rows) if 'phone_rows' in locals() else 0,
    }


def sync_call_list_openers(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    """Pre-generate openers for every Call List candidate not already cached.

    The page reads openers from `csr_openers`; without this step, the first
    page load that includes a never-before-seen customer fires a Claude
    call (3-5s). Pre-warming here means cold loads stay near-instant.

    Cost discipline:
      - We only generate for (kind, customer_id, secondary_id) tuples
        NOT already in csr_openers. After the initial backfill, daily
        incremental cost is ~5-15 new openers × $0.003 = ~$0.05/day.
      - Buffer of 2× SECTION_CAPS per section so the "next up" rows that
        will bubble in as Fey marks the current ones are also ready.

    Returns {generated: N, candidates: N, skipped_cached: N}.
    """
    # Lazy imports — avoids circular dependencies and keeps lib/ light
    from datetime import date
    from scripts.send_csr_daily_email import (
        load_membership_opps, load_sleeping_customers,
        load_missed_calls, load_open_estimates,
        SECTION_CAPS, dedup_key, load_recommendation_state,
        to_central,
    )
    from lib.call_openers import generate_openers

    progress("Loading Call List candidate rows…")
    state = load_recommendation_state(conn)
    suppress = state["suppress"]

    memberships = [r for r in load_membership_opps(conn)
                   if dedup_key("membership", r.get("customer_id"))
                      not in suppress["membership"]][:SECTION_CAPS["membership"] * 2]
    sleeping = [r for r in load_sleeping_customers(conn, limit=SECTION_CAPS["sleeping"] * 4)
                if dedup_key("sleeping", r.get("customer_id"))
                   not in suppress["sleeping"]][:SECTION_CAPS["sleeping"] * 2]
    missed = [r for r in load_missed_calls(conn)
              if dedup_key("missed", r.get("customer_id"), r.get("id"))
                 not in suppress["missed"]][:SECTION_CAPS["missed"] * 2]
    estimates = [r for r in load_open_estimates(conn, min_age_days=30)
                 if dedup_key("estimate", r.get("customer_id"), r.get("id"))
                    not in suppress["estimate"]][:SECTION_CAPS["estimate"] * 2]

    today_d = date.today()
    inputs: list[dict] = []
    keys: list[tuple[str, int, int]] = []

    # Same opener-input shape the page builds — keep these in sync.
    for r in memberships:
        cid = r.get("customer_id")
        if not cid: continue
        install_date = r.get("install_date")
        inputs.append({
            "customer_id": cid,
            "customer_name": r.get("customer_name"),
            "kind": "membership",
            "equipment": r.get("equipment"),
            "install_summary": r.get("install_summary"),
            "install_days_ago": (today_d - install_date).days if install_date else None,
            "install_value": float(r.get("install_value") or 0),
            "lifetime_revenue": float(r.get("lifetime_revenue") or 0),
            "lifetime_invoices": int(r.get("lifetime_invoices") or 0),
            "first_visit_year": r["first_visit"].year if r.get("first_visit") else None,
        })
        keys.append(("membership", int(cid), 0))

    for r in sleeping:
        cid = r.get("customer_id")
        if not cid: continue
        last_visit = r.get("last_visit")
        inputs.append({
            "customer_id": cid,
            "customer_name": r.get("customer_name"),
            "kind": "sleeping",
            "last_visit_days_ago": (today_d - last_visit).days if last_visit else None,
            "last_summary": r.get("last_summary"),
            "last_items": r.get("last_items"),
            "loyal_revenue": float(r.get("loyal_revenue") or 0),
            "loyal_invoices": int(r.get("loyal_invoices") or 0),
        })
        keys.append(("sleeping", int(cid), 0))

    for r in missed:
        cid = r.get("customer_id")
        if not cid: continue
        received = r.get("received_on")
        last_visit = r.get("last_visit")
        inputs.append({
            "customer_id": cid,
            "customer_name": r.get("customer_name") or "Unknown",
            "kind": "missed",
            "call_type": r.get("call_type"),
            "call_when": to_central(received).strftime("%a %I:%M %p") if received else "earlier",
            "lifetime_revenue": float(r.get("lifetime_revenue") or 0),
            "lifetime_invoices": int(r.get("lifetime_invoices") or 0),
            "last_visit_days_ago": (today_d - last_visit).days if last_visit else None,
            "last_invoice_summary": r.get("last_invoice_summary"),
        })
        keys.append(("missed", int(cid), 0))

    for r in estimates:
        cid = r.get("customer_id")
        if not cid: continue
        inputs.append({
            "customer_id": cid,
            "customer_name": r.get("customer_name"),
            "kind": "estimate",
            "estimate_name": r.get("estimate_name"),
            "summary": r.get("summary"),
            "subtotal": float(r.get("subtotal") or 0),
            "age_days": int(r.get("age_days") or 0),
            "originating_tech": r.get("originating_tech"),
            "business_unit_name": r.get("business_unit_name"),
            "lifetime_revenue": float(r.get("lifetime_revenue") or 0),
            "lifetime_invoices": int(r.get("lifetime_invoices") or 0),
            "estimate_id": int(r.get("id") or 0),
        })
        keys.append(("estimate", int(cid), int(r.get("id") or 0)))

    if not keys:
        return {"generated": 0, "candidates": 0, "skipped_cached": 0}

    # Skip composite keys that already have a row
    with conn.cursor() as cur:
        placeholders = ",".join(["(%s,%s,%s)"] * len(keys))
        params = [v for t in keys for v in t]
        cur.execute(
            f"SELECT kind, customer_id, secondary_id FROM csr_openers "
            f"WHERE (kind, customer_id, secondary_id) IN ({placeholders})",
            params,
        )
        cached = {(r["kind"], int(r["customer_id"]), int(r["secondary_id"]))
                  for r in cur.fetchall()}

    to_generate = [(inp, k) for inp, k in zip(inputs, keys) if k not in cached]
    if not to_generate:
        progress(f"All {len(keys)} openers already cached — nothing to do.")
        return {"generated": 0, "candidates": len(keys), "skipped_cached": len(keys)}

    # Sanity cap so a misconfiguration can't burn through Claude tokens.
    # 200 new openers per run = ~$0.60; way above the realistic incremental
    # rate (5-15/day), well below a runaway cost.
    HARD_CAP = 200
    if len(to_generate) > HARD_CAP:
        progress(f"WARNING capping at {HARD_CAP} (would have generated {len(to_generate)})")
        to_generate = to_generate[:HARD_CAP]

    progress(f"Generating {len(to_generate)} new openers via Claude…")
    new_openers = generate_openers([inp for inp, _ in to_generate])

    # Dedupe by composite key — multiple inputs can share a key
    # (e.g. two missed calls from the same customer both map to
    # ("missed", cid, 0)). Without this, ON CONFLICT raises
    # CardinalityViolation since the same key appears twice in one INSERT.
    rows: list[tuple] = []
    seen: set[tuple] = set()
    for inp, key in to_generate:
        if key in seen:
            continue
        opener = new_openers.get(key[1])  # generate_openers returns dict[cid]
        if opener:
            rows.append((key[0], key[1], key[2], opener))
            seen.add(key)

    if rows:
        progress(f"Persisting {len(rows)} openers…")
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO csr_openers "
                "  (kind, customer_id, secondary_id, opener) VALUES %s "
                "ON CONFLICT (kind, customer_id, secondary_id) DO UPDATE SET "
                "  opener = EXCLUDED.opener, generated_at = NOW()",
                rows,
            )
        conn.commit()

    return {
        "generated": len(rows),
        "candidates": len(keys),
        "skipped_cached": len(cached),
    }


def sync_all(
    client: ServiceTitanClient,
    conn: psycopg2.extensions.connection,
    progress: ProgressCallback = _noop,
) -> dict:
    inv_stats = sync_invoices(client, conn, progress)

    # First-time bootstrap: if invoices exist but no items have been written yet,
    # backfill them from the cached raw payloads. Cheap to check; no-op once done.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM invoice_items")
        items_count = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM invoices")
        invoices_count = cur.fetchone()["n"]
    if invoices_count and items_count == 0:
        backfill_invoice_items_from_raw(conn, progress)

    mem_stats = sync_memberships(client, conn, progress)
    est_stats = sync_estimates(client, conn, progress)
    job_stats = sync_jobs(client, conn, progress)
    cmp_stats = sync_campaigns(client, conn, progress)
    call_stats = sync_calls(client, conn, progress)
    appt_stats = sync_appointment_assignments(client, conn, progress)
    return {
        "invoices": inv_stats,
        "memberships": mem_stats,
        "estimates": est_stats,
        "jobs": job_stats,
        "campaigns": cmp_stats,
        "calls": call_stats,
        "appointment_assignments": appt_stats,
    }
