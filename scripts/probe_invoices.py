"""Test which date filter parameter name actually works for the invoices endpoint."""
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
    page_size=3,
)
path = f"/accounting/v2/tenant/{client.tenant_id}/invoices"


def probe(label: str, params: dict) -> None:
    print(f"\n--- {label} ---")
    print(f"params: {params}")
    try:
        body = client._request(path, {**params, "pageSize": 3, "page": 1})
        rows = body.get("data", [])
        print(f"returned {len(rows)} rows; first invoiceDates: {[r.get('invoiceDate') for r in rows]}")
    except Exception as exc:
        print(f"ERROR: {exc}")


probe("invoicedOnOrAfter (current name)", {"invoicedOnOrAfter": "2026-04-01T00:00:00Z"})
probe("invoiceDateOnOrAfter", {"invoiceDateOnOrAfter": "2026-04-01T00:00:00Z"})
probe("invoiceDateFrom", {"invoiceDateFrom": "2026-04-01T00:00:00Z"})
probe("createdOnOrAfter (known good name)", {"createdOnOrAfter": "2026-04-01T00:00:00Z"})
probe("modifiedOnOrAfter", {"modifiedOnOrAfter": "2026-04-01T00:00:00Z"})
