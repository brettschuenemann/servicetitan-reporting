"""Find invoices for the Vanderzwan customer and dump all date fields."""
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
    page_size=500,
)

# Pull a wide window so we catch all related invoices.
invoices = client.get_invoices(
    created_after="2026-01-01T00:00:00Z",
    created_before="2026-06-01T00:00:00Z",
)
print(f"Loaded {len(invoices)} invoices in window")

matches = [
    inv for inv in invoices
    if "vanderzwan" in (inv.get("customer", {}).get("name") or "").lower()
]
print(f"Matched {len(matches)} for 'vanderzwan'\n")

for inv in matches:
    print(f"id={inv['id']}  ref={inv.get('referenceNumber')}  customer={inv['customer'].get('name')}")
    print(f"  createdOn:   {inv.get('createdOn')}")
    print(f"  invoiceDate: {inv.get('invoiceDate')}")
    print(f"  modifiedOn:  {inv.get('modifiedOn')}")
    print(f"  dueDate:     {inv.get('dueDate')}")
    print(f"  paidOn:      {inv.get('paidOn')}")
    print(f"  depositedOn: {inv.get('depositedOn')}")
    print(f"  total:       {inv.get('total')}")
    print()
