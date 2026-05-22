"""Drill into 2025 totals by various slicing/inclusion rules to find the $484.3k.
"""
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


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


invoices = client.get_invoices()
print(f"Total invoices in tenant: {len(invoices)}")

# 1) Full-year 2025 totals
y2025 = [inv for inv in invoices if (inv.get("invoiceDate") or "").startswith("2025")]
print(f"\n2025 invoices (all): {len(y2025)}, total ${sum(to_float(i.get('total')) for i in y2025):,.2f}")

# 2) Monthly breakdown for 2025
print("\nMonth-by-month for 2025:")
running = 0.0
running_count = 0
for m in range(1, 13):
    rows = [inv for inv in y2025 if (inv.get("invoiceDate") or "")[5:7] == f"{m:02d}"]
    month_total = sum(to_float(i.get("total")) for i in rows)
    running += month_total
    running_count += len(rows)
    print(f"  {m:02d}/2025: {len(rows):>4} rows  ${month_total:>13,.2f}    running YTD: {running_count:>4} rows  ${running:>13,.2f}")

# 3) Same-day-of-year YTD (Jan 1 → May 22) and other plausible cutoffs
today = date.today()
print(f"\nVarious 2025 YTD cutoffs (looking for ~$484,300):")
for cutoff in (
    "2025-04-30",  # full April (last complete month)
    "2025-05-15",  # mid-May
    "2025-05-22",  # same-day-of-year as today
    "2025-05-31",  # full May (next complete month)
    "2025-06-22",  # +30 days
):
    rows = [inv for inv in y2025 if (inv.get("invoiceDate") or "")[:10] <= cutoff]
    print(f"  Jan 1 → {cutoff}: {len(rows):>4} rows  total ${sum(to_float(i.get('total')) for i in rows):>13,.2f}")

# 4) Look at what fields besides 'total' could push us up by ~$49,800
print("\n2025 YTD (through 2025-05-22): field-by-field sums to find missing $49.8k")
ytd = [inv for inv in y2025 if (inv.get("invoiceDate") or "")[:10] <= "2025-05-22"]
print(f"  rows: {len(ytd)}")
for f in ("total", "subTotal", "salesTax", "balance", "discountTotal"):
    print(f"  sum({f}): ${sum(to_float(i.get(f)) for i in ytd):>13,.2f}")

# 5) Check line items (within each invoice's 'items' array) for tax/discount differences
items_total = 0.0
items_count = 0
items_member_total = 0.0
for inv in ytd:
    for item in (inv.get("items") or []):
        items_total += to_float(item.get("total"))
        items_member_total += to_float(item.get("memberPrice"))
        items_count += 1
print(f"  line items: {items_count}, sum(item.total): ${items_total:,.2f}, sum(item.memberPrice): ${items_member_total:,.2f}")

# 6) Look at modifiedOn — are there 2025-invoiceDate invoices modified RECENTLY (which would push their inclusion)?
print("\nSpot-check modifiedOn for 2025 YTD invoices:")
mod_counter = Counter()
for inv in ytd:
    mo = (inv.get("modifiedOn") or "")[:7]  # YYYY-MM
    mod_counter[mo] += 1
for mo, n in sorted(mod_counter.items()):
    print(f"  modifiedOn {mo}: {n}")
