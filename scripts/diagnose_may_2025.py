"""Look at line-item serviceDate and the payments-by-date dimension for May 2025."""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from lib.servicetitan import ServiceTitanClient

load_dotenv()
client = ServiceTitanClient(
    app_key=os.environ["ST_APP_KEY"],
    tenant_id=os.environ["ST_TENANT_ID"],
    client_id=os.environ["ST_CLIENT_ID"],
    client_secret=os.environ["ST_CLIENT_SECRET"],
    environment=os.environ.get("ST_ENVIRONMENT", "production"),
    page_size=500,
)


def f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def in_may_2025(s):
    return isinstance(s, str) and s.startswith("2025-05")


invoices = client.get_invoices()

# A) Sum line items by serviceDate in May 2025
item_total = 0.0
item_count = 0
items_outside_invoice_may = 0.0  # items whose serviceDate is in May 2025 but invoiceDate isn't
for inv in invoices:
    for item in (inv.get("items") or []):
        if in_may_2025(item.get("serviceDate")):
            amt = f(item.get("total"))
            item_total += amt
            item_count += 1
            if not in_may_2025(inv.get("invoiceDate")):
                items_outside_invoice_may += amt

print(f"Line items with serviceDate in May 2025: {item_count} items, total ${item_total:,.2f}")
print(f"  ...of which sit on invoices NOT dated in May 2025: ${items_outside_invoice_may:,.2f}")

# B) Total of invoices that have at least one line item with serviceDate in May 2025 (full invoice value)
inv_touching_may = []
for inv in invoices:
    for item in (inv.get("items") or []):
        if in_may_2025(item.get("serviceDate")):
            inv_touching_may.append(inv)
            break
inv_touching_total = sum(f(i.get("total")) for i in inv_touching_may)
print(f"\nInvoices touching May 2025 (any line-item serviceDate in May): {len(inv_touching_may)}, sum total ${inv_touching_total:,.2f}")

# C) Payments endpoint — try filtering by the 'date' field (which is actually populated)
print("\n=== Payments where the 'date' field is in May 2025 ===")
# The payments endpoint has both 'paidOnOrAfter' and 'startsOnOrAfter' etc.
# Try a couple variants.
for after, before, label in (
    ("paidOnOrAfter", "paidBefore", "paidOn"),
    ("startsOnOrAfter", "startsBefore", "starts"),
):
    try:
        body = client._request(
            f"/accounting/v2/tenant/{client.tenant_id}/payments",
            {after: "2025-05-01T00:00:00Z", before: "2025-06-01T00:00:00Z", "pageSize": 500, "page": 1},
        )
        rows = body.get("data", [])
        # See what the 'date' field actually is on these rows
        may_dates = sum(1 for r in rows if in_may_2025(r.get("date")))
        print(f"  filter={label}: page 1 returned={len(rows)}, of which 'date' starts with 2025-05: {may_dates}")
        if rows:
            print(f"    first row: id={rows[0].get('id')} date={rows[0].get('date')} paidOn={rows[0].get('paidOn')} total={rows[0].get('total')}")
    except Exception as exc:
        print(f"  filter={label}: ERROR {str(exc)[:200]}")

# D) Sum payments whose 'date' field is in May 2025 (paginate through all)
print("\nPaginating all payments and summing those with date in May 2025…")
page = 1
total_payments_may = 0.0
n_may = 0
while True:
    body = client._request(
        f"/accounting/v2/tenant/{client.tenant_id}/payments",
        {"pageSize": 500, "page": page},
    )
    rows = body.get("data", [])
    if not rows:
        break
    for r in rows:
        if in_may_2025(r.get("date")):
            total_payments_may += f(r.get("total"))
            n_may += 1
    if not body.get("hasMore"):
        break
    page += 1
print(f"  Payments with date in May 2025: {n_may} rows, sum total ${total_payments_may:,.2f}")
