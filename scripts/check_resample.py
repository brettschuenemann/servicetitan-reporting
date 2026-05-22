"""Reproduce the home dashboard's resample on the Vanderzwan invoice in isolation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from lib.servicetitan import ServiceTitanClient
from lib.reporting import invoices_to_dataframe

load_dotenv()
client = ServiceTitanClient(
    app_key=os.environ["ST_APP_KEY"],
    tenant_id=os.environ["ST_TENANT_ID"],
    client_id=os.environ["ST_CLIENT_ID"],
    client_secret=os.environ["ST_CLIENT_SECRET"],
    environment=os.environ.get("ST_ENVIRONMENT", "production"),
)

invoices = client.get_invoices(
    created_after="2026-01-01T00:00:00Z",
    created_before="2026-06-01T00:00:00Z",
)
df = invoices_to_dataframe(invoices)
vz = df[df["customer"].apply(lambda c: isinstance(c, dict) and "Vanderzwan, Nick" == c.get("name"))]
print(f"{len(vz)} Vanderzwan Nick invoices in window")
print(vz[["id", "referenceNumber", "createdOn", "invoiceDate", "total"]].to_string())
print()

# Resample by invoiceDate exactly as the home chart does
monthly = df.dropna(subset=["invoiceDate"]).set_index("invoiceDate").resample("MS")["total"].sum().reset_index()
monthly["month"] = monthly["invoiceDate"].dt.strftime("%b %Y")
print("Top months by revenue:")
print(monthly.sort_values("total", ascending=False).head(15).to_string(index=False))
