"""See if any 2024 memberships are available through different filter combinations."""
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
)
path = f"/memberships/v2/tenant/{client.tenant_id}/memberships"


def probe(label, params):
    try:
        body = client._request(path, {**params, "pageSize": 500, "page": 1})
        rows = body.get("data", [])
        years = sorted({(r.get("from") or "")[:4] for r in rows if r.get("from")})
        print(f"  {label}: page1={len(rows)} hasMore={body.get('hasMore')}  from-years on page1: {years}")
    except Exception as exc:
        print(f"  {label}: ERROR {str(exc)[:160]}")


print("Probing /memberships filters:")
probe("active=True (default)", {})
probe("active=Any", {"active": "Any"})
probe("active=False", {"active": "False"})
probe("fromOnOrBefore=2024-12-31", {"fromOnOrBefore": "2024-12-31T00:00:00Z"})
probe("createdBefore=2025-01-01", {"createdBefore": "2025-01-01T00:00:00Z"})
probe("modifiedBefore=2025-01-01", {"modifiedBefore": "2025-01-01T00:00:00Z"})
probe("statusFilter=Canceled", {"status": "Canceled"})
probe("statusFilter=Expired", {"status": "Expired"})

# Full paginate active=Any to count all from-years
print("\nFull paginate with active=Any to see all from-years:")
all_rows = list(client._paginate(path, {"active": "Any"}))
print(f"  Total: {len(all_rows)} memberships")
from collections import Counter
yrs = Counter((r.get("from") or "")[:4] for r in all_rows)
for yr, n in sorted(yrs.items()):
    print(f"    {yr or 'NULL'}: {n}")
