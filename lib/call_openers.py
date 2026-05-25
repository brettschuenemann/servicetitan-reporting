"""Per-customer personalized call openers via Claude.

Used by the morning CSR email to replace the generic "Hi [name], this is
Fey at Pure Comfort..." opener with a warm, specific one-liner that
references something memorable about the customer (their equipment, last
service, length of relationship, etc.).

One batched API call per email send — handles the entire day's list in
a single Claude request to keep cost and latency low.
"""
from __future__ import annotations

import json
import os

import anthropic


# Sonnet is plenty capable for this task and ~5× cheaper than Opus.
# Override with CALL_OPENER_MODEL if you want to swap.
_DEFAULT_MODEL = "claude-sonnet-4-5"


_SYSTEM_PROMPT = """You write personalized call-opener lines for Fey, a CSR at Pure Comfort — HVAC and full-service plumbing, Chicago area.

For each customer in the list, write ONE opener (1-2 sentences) following ALL the rules below.

GREETING
- Customer names may be formatted "Last, First" — extract the first name.
- If the name contains "& [Other Name]" (a couple or family), greet BOTH by name: "Hi Nancy and Barney" or use the friendlier "Hey Nancy and Barney, it's Fey..." Either is fine — just don't drop the second person.
- If the name is a business (contains "LLC", "Inc.", "Company", "Theatre", "Restaurant", etc.) or has no clean first name, open with "Hi there" or "Hey there".

SPECIFICITY (mandatory)
Reference a CONCRETE detail from the customer's history — not a generic placeholder like "your new equipment" or "your last service."

- Membership customers: name the specific equipment from the `equipment:` field whenever it's recorded. Lift the brand and equipment type out of the description verbatim if helpful ("your new Trane heat pump", "the Carrier furnace and air handler we just put in", "the Mitsubishi mini-split"). Only fall back to "your new system" when the equipment field is empty or genuinely uninformative.

- Sleeping customers: when `last service notes:` are present, reference the SPECIFIC work done ("the capacitor swap", "spring maintenance on both units", "the inducer motor replacement", "topped off the refrigerant"). Don't just say "your last service" — name the work. Skip this only if notes are missing.

- Missed callers: if they're an existing customer (lifetime stats present), warmly mention the existing relationship ("good to hear from you again", "saw you've been with us a few years"). For new callers, reference the specific time you saw their call.

TONE
- Friendly human in 2026: contractions ("it's", "wanted to", "how's"), warm but not gushing.
- No corporate jargon, no "I hope this email finds you well" energy.

CLOSE
- End with a natural check-in question: "how's it running?", "how's everything been?", "wanted to make sure everything's running smooth", "what can we help with today?".

WHAT NOT TO INCLUDE
- Do NOT mention membership, plans, offers, discounts, or any sale — that comes later in the call.
- Do NOT invent details (equipment, prior conversations, technician names) that aren't in the data.
- Stay under 320 characters per opener.

Return a strict JSON object mapping customer_id (as a string) to opener text. No prose, no markdown fences — just JSON.

Example output:
{"1003": "Hi Jennifer, it's Fey at Pure Comfort...", "2001": "Hey Michael and Susan..."}"""


def _format_customer(c: dict) -> str:
    """Render one customer's context for the prompt — kind-specific."""
    kind = c.get("kind", "")
    name = c.get("customer_name") or "Customer"
    cid = c.get("customer_id")
    lines = [f"customer_id={cid} kind={kind} name=\"{name}\""]

    if kind == "membership":
        eq = (c.get("equipment") or "").strip() or "(equipment not recorded)"
        days = c.get("install_days_ago")
        val = c.get("install_value", 0)
        ltv = c.get("lifetime_revenue", 0)
        visits = c.get("lifetime_invoices", 0)
        first_year = c.get("first_visit_year")
        lines.append(f"  equipment: {eq[:160]}")
        if days is not None:
            lines.append(f"  install: {days} days ago, ${val:,.0f}")
        if first_year and visits > 1:
            lines.append(f"  customer since {first_year} — {visits} prior visits, ${ltv:,.0f} lifetime")
        elif first_year:
            lines.append(f"  customer since {first_year} (first install)")

    elif kind == "sleeping":
        days = c.get("last_visit_days_ago")
        summary = (c.get("last_summary") or "").strip() or "(no last-service notes)"
        rev = c.get("loyal_revenue", 0)
        visits = c.get("loyal_invoices", 0)
        if days is not None:
            lines.append(f"  last visit: {days} days ago")
        lines.append(f"  last service notes: {summary[:160]}")
        lines.append(f"  loyal-period: ${rev:,.0f} across {visits} visits")

    elif kind == "missed":
        call_type = c.get("call_type", "missed call")
        when = c.get("call_when", "earlier")
        ltv = c.get("lifetime_revenue", 0)
        visits = c.get("lifetime_invoices", 0)
        last_visit = c.get("last_visit_days_ago")
        lines.append(f"  missed call: {call_type} at {when}")
        if visits:
            lines.append(f"  existing customer — ${ltv:,.0f} across {visits} visits")
            if last_visit is not None:
                lines.append(f"  last visit {last_visit} days ago")
        else:
            lines.append("  new caller — no prior history with us")

    return "\n".join(lines)


def generate_openers(customers: list[dict], model: str | None = None) -> dict[int, str]:
    """Batch-generate openers. Returns customer_id -> opener text.

    Customers without customer_id are skipped (unmatched missed callers).
    On any failure, returns empty dict so the caller can fall back to the
    generic call script.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {}

    eligible = [c for c in customers if c.get("customer_id")]
    if not eligible:
        return {}

    model = model or os.environ.get("CALL_OPENER_MODEL") or _DEFAULT_MODEL
    body = (
        "Generate one opener per customer:\n\n"
        + "\n\n".join(_format_customer(c) for c in eligible)
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        # ~80-100 tokens per opener × up to 50 customers = need headroom.
        # 8192 is plenty without being wasteful.
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": body}],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "").strip()
        # Strip any markdown fences if Claude wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            if raw.rstrip().endswith("```"):
                raw = raw.rsplit("```", 1)[0]
        data = json.loads(raw.strip())
        out: dict[int, str] = {}
        for k, v in data.items():
            try:
                out[int(k)] = str(v).strip()
            except (ValueError, TypeError):
                continue
        return out
    except Exception as exc:
        print(f"[call_openers] generation failed: {exc}")
        return {}
