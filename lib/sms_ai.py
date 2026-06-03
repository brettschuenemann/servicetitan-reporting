"""AI-suggested reply drafts for inbound SMS.

Given a thread (the last N messages), classify intent and draft an
honest, on-brand reply for Fey to either send-as-is or edit.

The model is told to use ONLY facts that are obvious from the thread —
no inventing equipment brands, prices, technician names, etc. If the
right reply requires info Fey hasn't given (specific time slot,
diagnostic price, etc.), the draft says "I'll check on that and get
back to you in 10 min" instead of guessing.

Intent buckets:
  schedule_new      — customer wants service for the first time
  reschedule        — wants to move an existing appointment
  cancel            — wants to cancel
  update_existing   — adding info to a job already on the calendar
  question          — asking something (price, hours, service area)
  accept_quote      — saying yes to an open estimate
  declining         — politely saying no thanks
  emergency         — urgent / no heat / no AC / leak
  thanks_done       — wrapping up, no further action needed
  unclear           — Fey should read it herself
"""
from __future__ import annotations

import json
import os
from typing import Optional

import anthropic


_MODEL = "claude-sonnet-4-5"
_MAX_TOKENS = 600


INTENTS = [
    "schedule_new", "reschedule", "cancel", "update_existing",
    "question", "accept_quote", "declining", "emergency",
    "thanks_done", "unclear",
]


_SYSTEM_PROMPT = """You are drafting a one-line text-message reply for Fey, a CSR at Pure Comfort (HVAC + plumbing in Chicagoland), to send to a customer.

You will receive the last few messages in an SMS thread between Pure Comfort and one customer. Your output is a strict JSON object with two fields:

{
  "intent": "<one of: schedule_new | reschedule | cancel | update_existing | question | accept_quote | declining | emergency | thanks_done | unclear>",
  "suggested_reply": "<the message Fey should send, ≤ 320 chars>"
}

Hard rules for the reply text:
- Conversational, warm, but efficient. NOT salesy.
- Use the customer's name ONLY if it's clear from the thread.
- NEVER invent specifics you can't see in the thread:
    * No prices unless they were already quoted
    * No technician names
    * No specific appointment times — say "I'll check the schedule and text right back"
    * No equipment brands or service-area claims
- If the customer's request needs Fey to do something offline first (look up the schedule, talk to a tech, check pricing), say something like "Let me check on that and text right back in a few minutes" rather than guessing.
- For "emergency" intent, prioritize speed and reassurance: "I'm reaching a tech now — what's the address?"
- For "declining" intent, be gracious and brief: "No problem, thanks for letting us know. We're here if anything changes."
- Always sound human. No corporate boilerplate.

Output STRICT JSON only — no markdown fences, no commentary. If the message is unclear or unsafe to auto-suggest, return intent "unclear" with suggested_reply "" (empty string)."""


def suggest_reply(messages: list[dict], model: Optional[str] = None) -> dict:
    """Given thread messages (chronological, list of {direction, body}),
    return {"intent": str, "suggested_reply": str}.

    Returns {"intent": "unclear", "suggested_reply": ""} on any error
    so callers can fail safe (just hide the suggestion).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not messages:
        return {"intent": "unclear", "suggested_reply": ""}

    # Render the thread in the form the LLM expects
    rendered = []
    for m in messages[-10:]:  # last 10 only
        speaker = "Customer" if m.get("direction") == "inbound" else "Pure Comfort"
        body = (m.get("body") or "").strip()
        if body:
            rendered.append(f"{speaker}: {body}")
    if not rendered:
        return {"intent": "unclear", "suggested_reply": ""}

    user_text = "Thread (oldest → newest):\n\n" + "\n\n".join(rendered)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model or os.environ.get("SMS_AI_MODEL") or _MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_text}],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "").strip()
        # Strip markdown fences if model added them despite instructions
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            if raw.rstrip().endswith("```"):
                raw = raw.rsplit("```", 1)[0]
        data = json.loads(raw.strip())
    except Exception as exc:
        print(f"[sms_ai] suggest_reply failed: {exc}")
        return {"intent": "unclear", "suggested_reply": ""}

    intent = str(data.get("intent", "unclear"))
    if intent not in INTENTS:
        intent = "unclear"
    reply = str(data.get("suggested_reply", "")).strip()
    if len(reply) > 480:
        reply = reply[:480]

    return {"intent": intent, "suggested_reply": reply}


# Intent → emoji + color hint for the UI
INTENT_META = {
    "schedule_new":    ("📅", "#0066EE", "Schedule new service"),
    "reschedule":      ("🔄", "#F59E0B", "Reschedule"),
    "cancel":          ("❌", "#DC2626", "Cancel appointment"),
    "update_existing": ("✏️", "#7C3AED", "Update job info"),
    "question":        ("❓", "#6B7280", "Question"),
    "accept_quote":    ("✅", "#10B981", "Accepting quote"),
    "declining":       ("👋", "#9CA3AF", "Polite decline"),
    "emergency":       ("🚨", "#DC2626", "EMERGENCY"),
    "thanks_done":     ("🙏", "#10B981", "Wrap-up"),
    "unclear":         ("🤔", "#6B7280", "Needs Fey's read"),
}
