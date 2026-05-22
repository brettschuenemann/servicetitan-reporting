"""Probe the memberships endpoints to see if there's hidden May 2025 revenue."""
import json
import os
import sys
from collections import Counter
from datetime import date

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


# 1) Are any of the existing /accounting/v2/invoices already tagged as membership invoices?
print("=== Already-loaded invoices with membershipId set ===")
invoices = client.get_invoices()
mem_invoices = [i for i in invoices if i.get("membershipId")]
print(f"  invoices with membershipId: {len(mem_invoices)}/{len(invoices)}")
may_mem = [i for i in mem_invoices if (i.get("invoiceDate") or "").startswith("2025-05")]
print(f"  of which invoiceDate in May 2025: {len(may_mem)}, total ${sum(f(i.get('total')) for i in may_mem):,.2f}")
print(f"  (so these would already be in our $65,895.29 — they don't add new revenue)")

# 2) List all available membership endpoints by trying common ones
print("\n=== Probing membership endpoints ===")
for path in (
    "memberships",
    "recurring-services",
    "recurring-service-events",
    "invoices",
    "membership-types",
    "recurring-service-types",
    "membership-status-changes",
):
    full = f"/memberships/v2/tenant/{client.tenant_id}/{path}"
    try:
        body = client._request(full, {"pageSize": 5, "page": 1})
        rows = body.get("data", [])
        print(f"  GET /{path}: returned={len(rows)}, hasMore={body.get('hasMore')}, totalCount={body.get('totalCount')}")
        if rows:
            sample = rows[0]
            money_keys = [k for k in sample.keys() if any(t in k.lower() for t in ("price", "amount", "total", "revenue", "billing"))]
            print(f"    money-ish keys: {money_keys}")
            print(f"    full first row keys: {sorted(sample.keys())[:20]}…")
    except Exception as exc:
        msg = str(exc)
        if "404" in msg:
            print(f"  GET /{path}: 404 not available")
        else:
            print(f"  GET /{path}: ERROR {msg[:200]}")

# 3) Look at the memberships endpoint in detail — pull a representative sample
print("\n=== Membership records: detailed look ===")
path = f"/memberships/v2/tenant/{client.tenant_id}/memberships"
all_mems = list(client._paginate(path))
print(f"  Total memberships: {len(all_mems)}")
if all_mems:
    sample = all_mems[0]
    print(f"  Sample membership:")
    print(json.dumps({k: v for k, v in sample.items() if v not in (None, "", [])}, indent=2, default=str)[:1500])

    # Active in May 2025? Look at from/to dates and status
    def covers_may_2025(m):
        frm = (m.get("from") or "")[:10]
        to = (m.get("to") or "")[:10] or "9999-12-31"
        return frm <= "2025-05-31" and "2025-05-01" <= to

    active_may = [m for m in all_mems if covers_may_2025(m)]
    print(f"\n  Memberships covering May 2025: {len(active_may)}")
    statuses = Counter(m.get("status") for m in active_may)
    print(f"  Status breakdown: {dict(statuses)}")

    # initialDeferredRevenue — can we sum?
    idr_total = sum(f(m.get("initialDeferredRevenue")) for m in all_mems)
    idr_total_active = sum(f(m.get("initialDeferredRevenue")) for m in active_may)
    print(f"\n  sum(initialDeferredRevenue), all memberships: ${idr_total:,.2f}")
    print(f"  sum(initialDeferredRevenue), May 2025-active:  ${idr_total_active:,.2f}")

    # billingFrequency distribution
    freq = Counter(m.get("billingFrequency") for m in active_may)
    print(f"  billingFrequency: {dict(freq)}")

    # Group by membershipTypeId to see what types are active
    types = Counter(m.get("membershipTypeId") for m in active_may)
    print(f"  membershipTypeId counts (top 5): {types.most_common(5)}")

# 4) Recurring services have an invoice template — see if there's a billing amount
print("\n=== Recurring services (potentially the billing schedule) ===")
path = f"/memberships/v2/tenant/{client.tenant_id}/recurring-services"
rs = list(client._paginate(path))
print(f"  Total recurring services: {len(rs)}")
if rs:
    print(f"  Sample:")
    sample = rs[0]
    print(json.dumps({k: v for k, v in sample.items() if v not in (None, "", [])}, indent=2, default=str)[:1500])
