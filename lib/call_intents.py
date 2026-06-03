"""AI intent classifier for call transcripts.

Mirrors the intent vocabulary in lib/sms_ai.py so analytics stack
across channels. Different prompt than SMS though — call transcripts
are longer, less context-bounded, and the customer is doing most of
the talking.

Intent buckets:
  schedule_new      — new service request (most valuable)
  reschedule        — moving an existing appointment
  cancel            — canceling
  update_existing   — adding info to a job already booked
  question          — pricing / hours / service area
  accept_quote      — saying yes to an open estimate
  declining         — explicit no thanks
  emergency         — urgent / no heat / no AC / leak
  billing           — billing / invoice question / payment
  warranty          — warranty issue
  thanks_done       — wrap-up call
  unclear           — couldn't categorize
"""
from __future__ import annotations

import json
import os
from typing import Optional

import anthropic


_MODEL = "claude-sonnet-4-5"
_MAX_TOKENS = 200


INTENTS = [
    "schedule_new", "reschedule", "cancel", "update_existing",
    "question", "accept_quote", "declining", "emergency",
    "billing", "warranty", "thanks_done", "unclear",
]


_SYSTEM_PROMPT = """You classify the intent of an inbound phone call to Pure Comfort (HVAC + plumbing in Chicagoland) based on the call transcript.

You receive a transcript of a customer calling Pure Comfort. Output STRICT JSON with one field:

{"intent": "<one of: schedule_new | reschedule | cancel | update_existing | question | accept_quote | declining | emergency | billing | warranty | thanks_done | unclear>"}

Decision rules:
- schedule_new = customer reporting a problem and wanting service for the first time on this issue
- reschedule = explicitly moving an existing appointment to a new time
- cancel = explicitly canceling an appointment
- update_existing = calling to add info to a job already on the books
- question = asking for info (cost, hours, service area, status)
- accept_quote = saying yes to a previously sent estimate
- declining = explicitly saying no thanks to something
- emergency = urgent breakdown — no heat in winter, no AC in summer, active water leak, gas smell
- billing = invoice or payment dispute or question
- warranty = problem with previous work
- thanks_done = just confirming/closing — no new action needed
- unclear = transcript is too short, garbled, or doesn't fit any category

Output JSON ONLY — no preamble, no markdown fences."""


def classify_intent(transcript: str, model: Optional[str] = None) -> str:
    """Return one of INTENTS, or 'unclear' on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not transcript or len(transcript) < 30:
        return "unclear"
    try:
        client = anthropic.Anthropic(api_key=api_key)
        # Truncate very long transcripts — first 4k chars is plenty for intent
        truncated = transcript[:4000]
        resp = client.messages.create(
            model=model or os.environ.get("CALL_INTENT_MODEL") or _MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": truncated}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.rstrip().endswith("```"):
                text = text.rsplit("```", 1)[0]
        data = json.loads(text.strip())
        intent = str(data.get("intent", "unclear"))
        return intent if intent in INTENTS else "unclear"
    except Exception as exc:
        print(f"[call_intents] classify failed: {exc}")
        return "unclear"


# Intent → display emoji + color for UI/analytics
INTENT_DISPLAY = {
    "schedule_new":    ("📅", "#0066EE", "Schedule new"),
    "reschedule":      ("🔄", "#F59E0B", "Reschedule"),
    "cancel":          ("❌", "#DC2626", "Cancel"),
    "update_existing": ("✏️", "#7C3AED", "Update job"),
    "question":        ("❓", "#6B7280", "Question"),
    "accept_quote":    ("✅", "#10B981", "Accept quote"),
    "declining":       ("👋", "#9CA3AF", "Decline"),
    "emergency":       ("🚨", "#DC2626", "EMERGENCY"),
    "billing":         ("💵", "#F59E0B", "Billing"),
    "warranty":        ("🛠️", "#F59E0B", "Warranty"),
    "thanks_done":     ("🙏", "#10B981", "Wrap-up"),
    "unclear":         ("🤔", "#6B7280", "Unclear"),
}
