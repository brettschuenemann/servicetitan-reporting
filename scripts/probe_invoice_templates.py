"""Fetch a specific membership invoice template by ID to see if it carries a price."""
import json
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

# Try a few candidate endpoints. We know billingTemplateId=296431035 and invoiceTemplateId=296435251.
template_id_billing = 296431035
template_id_invoice = 296435251

paths_to_try = [
    f"/memberships/v2/tenant/{client.tenant_id}/invoice-templates/{template_id_billing}",
    f"/memberships/v2/tenant/{client.tenant_id}/invoice-templates/{template_id_invoice}",
    f"/memberships/v2/tenant/{client.tenant_id}/billing-templates/{template_id_billing}",
    f"/memberships/v2/tenant/{client.tenant_id}/billing-templates",  # list
    f"/memberships/v2/tenant/{client.tenant_id}/recurring-service-types",
    f"/pricebook/v2/tenant/{client.tenant_id}/memberships",
    f"/pricebook/v2/tenant/{client.tenant_id}/services",
]

for path in paths_to_try:
    try:
        # Use direct request — listing endpoints don't take an ID, so adjust params
        if "/{}".format("") in path:
            pass
        params = {"pageSize": 3, "page": 1} if not any(c.isdigit() and "/" + c in path[-15:] for c in path[-15:]) else None
        # Simplification: only add params when path is a listing (no trailing numeric ID segment)
        last_seg = path.rsplit("/", 1)[-1]
        params = None if last_seg.isdigit() else {"pageSize": 3, "page": 1}
        body = client._request(path, params)
        print(f"OK   {path}")
        # Print top-level keys / a row
        if isinstance(body, dict):
            if "data" in body:
                rows = body["data"]
                print(f"     listing: returned {len(rows)}, hasMore={body.get('hasMore')}")
                if rows:
                    money_keys = [k for k in rows[0].keys() if any(t in k.lower() for t in ("price", "amount", "total", "revenue", "billing"))]
                    print(f"     money-ish keys: {money_keys}")
                    print(json.dumps(rows[0], indent=2, default=str)[:1500])
            else:
                money_keys = [k for k in body.keys() if any(t in k.lower() for t in ("price", "amount", "total", "revenue", "billing"))]
                print(f"     single record. money-ish keys: {money_keys}")
                print(json.dumps(body, indent=2, default=str)[:1500])
    except Exception as exc:
        msg = str(exc)
        short = msg[:120].replace("\n", " ")
        print(f"FAIL {path}  →  {short}")
    print()
