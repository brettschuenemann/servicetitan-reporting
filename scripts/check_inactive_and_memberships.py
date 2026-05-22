"""See whether inactive invoices or memberships could account for the $49.8k gap."""
import os
import sys

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


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


# 1) Does passing active=Any change anything?
print("=== Invoices with active=Any (would include voided/inactive) ===")
try:
    body = client._request(
        f"/accounting/v2/tenant/{client.tenant_id}/invoices",
        {"active": "Any", "pageSize": 500, "page": 1},
    )
    print(f"page 1 totalCount={body.get('totalCount')} hasMore={body.get('hasMore')} returned={len(body.get('data', []))}")
    actives = sum(1 for inv in body["data"] if inv.get("active") is True)
    inactives = sum(1 for inv in body["data"] if inv.get("active") is False)
    print(f"  active=True: {actives}, active=False: {inactives}")
    if inactives:
        for inv in body["data"]:
            if inv.get("active") is False:
                print(f"  INACTIVE id={inv['id']} invoiceDate={inv.get('invoiceDate')} total={inv.get('total')}")
                break
except Exception as exc:
    print(f"ERROR: {exc}")

# 2) Memberships endpoint — see if there's billable membership data we're missing
print("\n=== Memberships ===")
for path in (
    f"/memberships/v2/tenant/{client.tenant_id}/invoice-templates",
    f"/memberships/v2/tenant/{client.tenant_id}/memberships",
    f"/memberships/v2/tenant/{client.tenant_id}/recurring-services",
):
    try:
        body = client._request(path, {"pageSize": 5, "page": 1})
        rows = body.get("data", [])
        print(f"  GET {path}: totalCount={body.get('totalCount')} returned={len(rows)}")
        if rows:
            print(f"    keys: {sorted(rows[0].keys())}")
    except Exception as exc:
        print(f"  GET {path}: ERROR {str(exc)[:200]}")

# 3) Estimates — sometimes counted as committed revenue
print("\n=== Estimates ===")
try:
    body = client._request(
        f"/sales/v2/tenant/{client.tenant_id}/estimates",
        {"createdOnOrAfter": "2025-01-01T00:00:00Z", "createdBefore": "2025-05-23T00:00:00Z", "pageSize": 5, "page": 1},
    )
    rows = body.get("data", [])
    print(f"  totalCount={body.get('totalCount')} hasMore={body.get('hasMore')} returned={len(rows)}")
    if rows:
        print(f"    keys: {sorted(rows[0].keys())}")
        for r in rows[:3]:
            print(f"    id={r.get('id')} subTotal={r.get('subTotal')} status={r.get('status', {}).get('value') if isinstance(r.get('status'), dict) else r.get('status')}")
except Exception as exc:
    print(f"  ERROR: {str(exc)[:200]}")
