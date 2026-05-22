"""Sum membership billings for May 2025 (cash-basis and accrual-basis)."""
import os
import sys
import time
from collections import Counter

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


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# Pull all memberships
print("Loading memberships…")
memberships = list(client._paginate(f"/memberships/v2/tenant/{client.tenant_id}/memberships"))
print(f"  {len(memberships)} memberships total")

# Active in May 2025 means: from <= 2025-05-31 AND (to is None OR to >= 2025-05-01)
def covers_may_2025(m):
    frm = (m.get("from") or "")[:10]
    to = (m.get("to") or "")[:10] or "9999-12-31"
    return frm and frm <= "2025-05-31" and to >= "2025-05-01"


active_may = [m for m in memberships if covers_may_2025(m)]
billed_may = [m for m in memberships if (m.get("from") or "").startswith("2025-05")]

print(f"  covering May 2025: {len(active_may)}")
print(f"  newly billed in May 2025 (from date in May): {len(billed_may)}")

# Distinct billing template IDs we need to fetch
billing_ids = sorted({m["billingTemplateId"] for m in memberships if m.get("billingTemplateId")})
print(f"\n  distinct billingTemplateIds: {len(billing_ids)}")

# Fetch each template's total. (Cache as we go.)
template_total: dict[int, float] = {}
print(f"  Fetching template totals…")
for i, tid in enumerate(billing_ids, 1):
    path = f"/memberships/v2/tenant/{client.tenant_id}/invoice-templates/{tid}"
    try:
        body = client._request(path)
        template_total[tid] = f(body.get("total"))
    except Exception as exc:
        template_total[tid] = 0.0
    if i % 50 == 0:
        print(f"    {i}/{len(billing_ids)}…")

# Look at distribution of template totals
totals_dist = Counter(round(v, 2) for v in template_total.values())
print(f"\n  Template-total distribution (top 10 prices):")
for amt, n in totals_dist.most_common(10):
    print(f"    ${amt:>10,.2f}: {n} templates")

# Cash-basis revenue: memberships billed in May (annual fee billed at `from`)
cash_basis = 0.0
billed_amounts = []
for m in billed_may:
    tid = m.get("billingTemplateId")
    amt = template_total.get(tid, 0.0)
    cash_basis += amt
    billed_amounts.append(amt)
print(f"\nCASH-BASIS (memberships billed in May 2025): {len(billed_may)} memberships, ${cash_basis:,.2f}")

# Accrual-basis: 1/12 of annual fee for every membership active in May
accrual = 0.0
for m in active_may:
    tid = m.get("billingTemplateId")
    annual = template_total.get(tid, 0.0)
    if m.get("billingFrequency") == "Annual":
        accrual += annual / 12
    elif m.get("billingFrequency") == "Monthly":
        accrual += annual
    # other frequencies — best-effort
print(f"ACCRUAL-BASIS (recognized in May 2025): {len(active_may)} memberships, ${accrual:,.2f}")

# What about memberships billed in EARLIER months but whose annual term covers May (typical scenario)?
print(f"\nOur YTD gap for 2025 (Jan 1 → May 22) was about $49.8k.")
print(f"Our May-only gap was about $23.3k.")
print(f"Compare these to the cash- and accrual-basis numbers above.")
