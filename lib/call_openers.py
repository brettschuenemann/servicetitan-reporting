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
import re

import anthropic


# Brand → product types Pure Comfort actually installs. Used to extract a
# clean "<brand> <product>" phrase from messy install notes so we hand
# Claude something it'll happily use verbatim instead of hedging to "the
# new system." Expand as you spot more brands in your own data.
_BRAND_PATTERNS = [
    r"AO\s*Smith",
    r"A\.\s*O\.\s*Smith",
    r"Bradford\s*White",
    r"Rheem",
    r"Rinnai",
    r"Navien",
    r"Noritz",
    r"Trane",
    r"Carrier",
    r"Lennox",
    r"Goodman",
    r"Amana",
    r"Daikin",
    r"Mitsubishi",
    r"Bryant",
    r"York",
    r"American\s*Standard",
    r"Bosch",
    r"Heil",
    r"Coleman",
    r"Ruud",
]

_PRODUCT_PATTERNS = [
    r"water\s*heater",
    r"tankless",
    r"heat\s*pump",
    r"furnace",
    r"air\s*handler",
    r"air\s*conditioner",
    r"condenser",
    r"AC\s*unit",
    r"mini[-\s]?split",
    r"boiler",
    r"sewer\s*line",
    r"drain\s*line",
    r"sump\s*pump",
    r"sewer\s*pump",
    r"ejector\s*pump",
    r"water\s*line",
    r"gas\s*line",
    r"toilet",
    r"vanity",
    r"shower\s*pan",
]


# Patterns for detecting business names so the greeting helper can fall back
# to "Hi there" instead of awkwardly using a non-person first name.
_BUSINESS_RE = re.compile(
    r"\b("
    r"LLC|Inc\.?|Corp\.?|Co\.?|Company|Companies|Group|Holdings|Trust|Estate|"
    r"Foundation|Association|HOA|Properties|Partners|Partnership|"
    r"Restaurant|Cafe|Studios?|Apartments?|Condos?|Condominiums?|"
    r"Theatre|Theater|Church|Synagogue|Temple|Mosque|"
    r"School|University|College|Hospital|Clinic|Center|Centre"
    r")\b",
    re.IGNORECASE,
)


def _is_business(name: str) -> bool:
    return bool(name) and bool(_BUSINESS_RE.search(name))


def _first_names(name: str) -> list[str]:
    """Pull first name(s) from messy formats:
      'Smith, John'            → ['John']
      'Smith, John & Jane'     → ['John', 'Jane']
      'John & Jane Smith'      → ['John', 'Jane']
      'John Smith'             → ['John']
    Returns [] for businesses or empty input.
    """
    if not name or _is_business(name):
        return []
    # Strip honorifics
    name = re.sub(r"\b(Mr|Mrs|Ms|Dr|Rev|Fr|Sr|Jr)\.?\s+", "", name, flags=re.IGNORECASE)
    if "," in name:
        # "Last, First [& Other]"
        first_part = name.split(",", 1)[1].strip()
    else:
        # "First [& Other] Last" — drop the last word as the surname
        words = name.split()
        first_part = " ".join(words[:-1]) if len(words) > 1 else name
    parts = re.split(r"\s*(?:&|\band\b|\+|/)\s*", first_part)
    out = []
    for p in parts:
        toks = p.strip().split()
        if toks:
            out.append(toks[0])
    return out[:2]  # cap at two for greeting purposes


def _greeting(name: str) -> str:
    if _is_business(name):
        return "Hi there"
    firsts = _first_names(name)
    if not firsts:
        return "Hi there"
    if len(firsts) == 1:
        return f"Hi {firsts[0]}"
    return f"Hey {firsts[0]} and {firsts[1]}"


def _days_phrase(days: int | None) -> str:
    """Natural-language 'how long ago' phrasing."""
    if days is None or days < 0:
        return "recently"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days <= 4:
        return "a few days ago"
    if days <= 10:
        return "about a week ago"
    if days <= 17:
        return "a week and a half ago"
    if days <= 24:
        return "a couple weeks ago"
    if days <= 38:
        return "about a month ago"
    if days <= 55:
        return "about six weeks ago"
    if days <= 75:
        return "a couple months ago"
    if days <= 130:
        return "a few months ago"
    months = days // 30
    return f"about {months} months ago"


# Known brands we expect to follow with a product type. If we extracted
# JUST the brand with no product type, append "unit" so "the new AO Smith"
# becomes "the new AO Smith unit" rather than the awkward bare brand.
_KNOWN_BRANDS_LOWER = {
    "ao smith", "a.o. smith", "bradford white", "rheem", "rinnai", "navien",
    "noritz", "trane", "carrier", "lennox", "goodman", "amana", "daikin",
    "mitsubishi", "bryant", "york", "american standard", "bosch", "heil",
    "coleman", "ruud",
}


def _equipment_phrase(eq: str) -> str:
    """Format the extracted equipment label for use in an opener template."""
    eq = eq.strip()
    if eq.lower() in _KNOWN_BRANDS_LOWER:
        return f"the new {eq} unit"
    return f"the new {eq}"


# Three template variants per kind so Fey doesn't see the same phrasing on
# every row. Pick deterministically by hashing customer_id so the same lead
# gets the same opener across email re-renders within a suppression window.
_MEMBERSHIP_TEMPLATES = [
    "{greet}, it's Fey at Pure Comfort — wanted to check in on {eq} we installed {when}. How's it running for you?",
    "{greet}, it's Fey from Pure Comfort. Just following up on {eq} we put in {when} — everything working smoothly?",
    "{greet}, Fey here at Pure Comfort — wanted to make sure {eq} we installed {when} is running well. How's it been?",
]


def _template_opener(c: dict) -> str | None:
    """Generate an opener via Python template when data is clean enough.
    Returns None to defer to Claude (relationship-based, edge cases, etc.)."""
    kind = c.get("kind")
    if kind != "membership":
        # Sleeping & missed are better off with Claude — they lean on
        # relationship context, recency, or call-type phrasing that
        # benefits from the model's variability.
        return None

    eq = c.get("equipment_extracted")
    if not eq:
        return None  # nothing concrete to anchor the opener — let Claude try

    name = c.get("customer_name") or ""
    days = c.get("install_days_ago")
    cid = c.get("customer_id") or 0

    template = _MEMBERSHIP_TEMPLATES[cid % len(_MEMBERSHIP_TEMPLATES)]
    return template.format(
        greet=_greeting(name),
        eq=_equipment_phrase(eq),
        when=_days_phrase(days),
    )


def _extract_equipment(text: str | None) -> str | None:
    """Pull a short '<brand> <product>' or '<product>' phrase from messy
    install notes. Returns None if nothing recognizable is found.

    Conservative on purpose — better to return None and let Claude fall
    back to a generic opener than to fabricate equipment names.
    """
    if not text:
        return None
    txt = text.strip()
    brand_re = "|".join(_BRAND_PATTERNS)
    product_re = "|".join(_PRODUCT_PATTERNS)

    def _title_brand(s: str) -> str:
        """Title-case a brand only if it came in all-lowercase. Preserve
        mixed-case brands like 'AO Smith' / 'Trane' verbatim."""
        if s == s.lower():
            return s.title()
        return s

    # Best match: "<brand> ... <product>" within 60 chars of each other
    m = re.search(
        rf"\b({brand_re})\b[\w\s\-/.]{{0,60}}?\b({product_re})\b",
        txt, flags=re.IGNORECASE,
    )
    if m:
        # Brand keeps its natural casing; product type stays lowercase
        # so we render "Bradford White water heater", not "Bradford White
        # Water Heater" (the latter reads like a product SKU, not prose).
        return f"{_title_brand(m.group(1))} {m.group(2).lower()}"

    # Second best: standalone brand mention
    m = re.search(rf"\b({brand_re})\b", txt, flags=re.IGNORECASE)
    if m:
        return _title_brand(m.group(1))

    # Third best: standalone product type
    m = re.search(rf"\b({product_re})\b", txt, flags=re.IGNORECASE)
    if m:
        return m.group(1).lower()

    return None


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
Reference a CONCRETE detail from the customer's history — not a generic placeholder like "your new equipment" or "your last service." Read ALL the context fields and pull the most distinctive specific detail.

- Membership customers: Pure Comfort does both HVAC and PLUMBING installs. Many "installs" are water heaters, sewer line replacements, drain work, etc. — not just HVAC. EXTRACT THE SPECIFIC ITEM/SCOPE FROM THE DATA — this is the most important rule. Do not say "the install" or "the work we did" — be concrete.

  Worked examples (input → opener):

  Input: install notes: "Service to drain and remove AO Smith cyclone 50 gallon 100k btu unit. To install new AO Smith cyclone unit with same specs..."
  → "Hi Bob, it's Fey at Pure Comfort — just checking in on the new AO Smith water heater we put in last week. How's it running?"

  Input: install notes: "Service to hand dig approx 2-3 ft down 18 foot section of sewer line in back yard. Replace all original 4" cast iron with new schedule 40 pvc..."
  → "Hi Jim, it's Fey at Pure Comfort — wanted to check in on the sewer line replacement we did a few days ago. Everything draining properly?"

  Input: equipment: "Trane XR15 3-ton heat pump"
  → "Hi Sarah, it's Fey at Pure Comfort — how's the new Trane heat pump treating you?"

  Input: install notes: "Bradford white 50 gal NG water heater install with new drain down valve and t-5 expansion tank"
  → "Hey Colin, it's Fey at Pure Comfort — checking in on the new Bradford White water heater we put in. How's it running?"

  Rules of thumb for extraction:
  * Brand name + equipment type (AO Smith water heater, Bradford White water heater, Trane heat pump, Carrier furnace, Mitsubishi mini-split)
  * Scope-of-work phrase (sewer line replacement, drain line repair, water heater swap, AC install)
  * Lift brand + product type VERBATIM from the notes — even if the notes are technical, the customer will recognize their own equipment
  * Only fall back to "your new system" when BOTH equipment and install notes are completely empty

- Sleeping customers: SKIP any line-item or summary text that looks like a ServiceTitan migration placeholder ("Imported Default Service", "Imported Default Invoice Item", "Default" anything). That's garbage data — never reference it.
  * When `last service summary:` is meaningful, reference the specific work done.
  * When the only available data is migration garbage or empty, lean on the relationship: their visit count, dollar history, and recency ("you've been a great customer for years", "noticed it's been about X months since we were out", "saw you've trusted us with a lot of work over the years").
  * Make them feel valued without inventing details. The loyal-period stats are real.

- Missed callers: reference the specific time you saw their call. If they're an existing customer (lifetime stats present), warmly mention the existing relationship ("good to hear from you again", "saw you've been with us a few years"). If the `last invoice was about:` field has meaningful text (not migration garbage), you can reference their last interaction.

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
        eq = (c.get("equipment") or "").strip()
        summary = (c.get("install_summary") or "").strip()
        # Pre-extract a clean equipment phrase from either source so Claude
        # gets a label it'll happily use verbatim (otherwise it tends to
        # hedge to "the new system" rather than pull from free-text notes).
        extracted = _extract_equipment(eq) or _extract_equipment(summary)
        days = c.get("install_days_ago")
        val = c.get("install_value", 0)
        ltv = c.get("lifetime_revenue", 0)
        visits = c.get("lifetime_invoices", 0)
        first_year = c.get("first_visit_year")
        if extracted:
            lines.append(f"  EQUIPMENT (use this exact phrase in the opener): {extracted}")
        elif eq:
            lines.append(f"  equipment: {eq[:200]}")
        elif summary:
            lines.append(f"  install notes (for context): {summary[:200]}")
        else:
            lines.append("  equipment: (not recorded — fall back to 'your new system')")
        if days is not None:
            lines.append(f"  install: {days} days ago, ${val:,.0f}")
        if first_year and visits > 1:
            lines.append(f"  customer since {first_year} — {visits} prior visits, ${ltv:,.0f} lifetime")
        elif first_year:
            lines.append(f"  customer since {first_year} (first install)")

    elif kind == "sleeping":
        days = c.get("last_visit_days_ago")
        summary = (c.get("last_summary") or "").strip()
        items = (c.get("last_items") or "").strip()
        rev = c.get("loyal_revenue", 0)
        visits = c.get("loyal_invoices", 0)
        if days is not None:
            lines.append(f"  last visit: {days} days ago")
        if summary:
            lines.append(f"  last service summary: {summary[:200]}")
        if items:
            lines.append(f"  last service line items: {items[:240]}")
        if not summary and not items:
            lines.append("  (no last-service notes recorded)")
        lines.append(f"  loyal-period: ${rev:,.0f} across {visits} visits")

    elif kind == "missed":
        call_type = c.get("call_type", "missed call")
        when = c.get("call_when", "earlier")
        ltv = c.get("lifetime_revenue", 0)
        visits = c.get("lifetime_invoices", 0)
        last_visit = c.get("last_visit_days_ago")
        last_summary = (c.get("last_invoice_summary") or "").strip()
        lines.append(f"  missed call: {call_type} at {when}")
        if visits:
            lines.append(f"  existing customer — ${ltv:,.0f} across {visits} visits")
            if last_visit is not None:
                lines.append(f"  last visit {last_visit} days ago")
            if last_summary:
                lines.append(f"  last invoice was about: {last_summary[:200]}")
        else:
            lines.append("  new caller — no prior history with us")

    return "\n".join(lines)


def generate_openers(customers: list[dict], model: str | None = None) -> dict[int, str]:
    """Hybrid opener generator (templates + LLM).

    Step 1 — pre-extract clean equipment phrases from raw fields and try
    the Python template layer per customer. This wins for membership rows
    where we recognized the brand/product: deterministic, free, and
    guarantees the equipment is named (which Sonnet/Opus stubbornly
    refuse to do when reading messy install-notes text).

    Step 2 — for customers the templates don't handle (sleeping, missed,
    or memberships with no recognized equipment), batch into a single
    Claude call. Templates already-handled get the deterministic opener;
    Claude handles the relationship-based / edge cases.

    Returns customer_id -> opener text. Customers without customer_id are
    skipped. On any Claude failure, the template results still come back.
    """
    out: dict[int, str] = {}
    for_llm: list[dict] = []

    for c in customers:
        if not c.get("customer_id"):
            continue
        # Pre-extract from whichever source is populated, stash on the
        # dict so both the template path and the LLM prompt can use it.
        if "equipment_extracted" not in c:
            c["equipment_extracted"] = (
                _extract_equipment(c.get("equipment"))
                or _extract_equipment(c.get("install_summary"))
            )
        templated = _template_opener(c)
        if templated:
            out[c["customer_id"]] = templated
        else:
            for_llm.append(c)

    if not for_llm:
        return out

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return out  # templates only — still better than nothing

    model = model or os.environ.get("CALL_OPENER_MODEL") or _DEFAULT_MODEL
    body = (
        "Generate one opener per customer:\n\n"
        + "\n\n".join(_format_customer(c) for c in for_llm)
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": body}],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            if raw.rstrip().endswith("```"):
                raw = raw.rsplit("```", 1)[0]
        data = json.loads(raw.strip())
        for k, v in data.items():
            try:
                out[int(k)] = str(v).strip()
            except (ValueError, TypeError):
                continue
    except Exception as exc:
        print(f"[call_openers] LLM batch failed: {exc}")
        # Template results already in `out` — return them anyway.

    return out
