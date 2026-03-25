"""
VAPI client — outbound AI phone calls for really.ai v2.

Ported from v1 app/services/vapi.py.
Changes vs v1:
  - No SQLModel references (Beanie used elsewhere).
  - All VAPI payloads, prompts, structured-data schemas, and call logic preserved verbatim.
"""
import httpx
from app.core.config import settings

VAPI_BASE = "https://api.vapi.ai"

INTAKE_SYSTEM_PROMPT = """You are Really, an AI real estate superconnecter. You're calling someone \
who just signed up on really.ai. Your job is to learn enough about them to find a great match.

Be warm, direct, and efficient. Ask one question at a time. No recaps. No filler.

Based on their role, collect:
- Buyers/Renters: location, budget range, property type, bedrooms, timeline
- Sellers/Landlords: property address, price, property type, availability
- Agents: market/area, specialization, what clients they need
- Investors: strategy, target markets, budget, property types

When you have enough info (role + location + one key detail), say:
"Perfect — I have everything I need. I'll call you as soon as I find a match. Talk soon!"
Then end the call."""

STRUCTURED_DATA_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {
            "type": "string",
            "enum": ["buyer", "seller", "renter", "landlord", "agent", "investor"],
        },
        "location": {"type": "string"},
        "budget_min": {"type": "number"},
        "budget_max": {"type": "number"},
        "property_types": {"type": "string"},
        "bedrooms": {"type": "number"},
        "timeline": {"type": "string"},
        "requirements": {"type": "string"},
        "listing_address": {"type": "string"},
        "listing_price": {"type": "number"},
        "listing_description": {"type": "string"},
    },
    "required": ["role", "location"],
}

CONSENT_STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "consented": {"type": "boolean"},
    },
    "required": ["consented"],
}


async def start_intake_call(phone: str) -> str:
    """Initiate a VAPI intake call. Returns the VAPI call ID."""
    payload = {
        "assistant": {
            "model": {
                "provider": "openai",
                "model": "gpt-4o",
                "systemPrompt": INTAKE_SYSTEM_PROMPT,
            },
            "voice": {"provider": "playht", "voiceId": "jennifer"},
            "firstMessage": (
                "Hey! This is Really — your AI real estate superconnecter. "
                "I just need two minutes to learn what you're looking for so I can find you "
                "the perfect match. What's your name?"
            ),
            "endCallMessage": (
                "Perfect — I have everything I need. "
                "I'll call you when I find a match. Talk soon!"
            ),
            "metadata": {"call_type": "intake"},
            "analysisPlan": {
                "structuredDataPrompt": (
                    "Extract the user's real estate profile from this conversation."
                ),
                "structuredDataSchema": STRUCTURED_DATA_SCHEMA,
                "summaryPrompt": (
                    "Summarize what this person is looking for in real estate in 1-2 sentences."
                ),
            },
            "serverUrl": f"{settings.PUBLIC_BASE_URL}/api/vapi/webhook",
        },
        "phoneNumberId": settings.VAPI_PHONE_NUMBER_ID,
        "customer": {"number": phone},
    }

    return await _make_call(payload)


async def start_consent_call(
    phone: str,
    name: str,
    match_name: str,
    match_role: str,
    match_location: str,
    match_summary: str,
    match_id: str,
) -> str:
    """Call a matched user to ask if they want to connect. Returns VAPI call ID."""
    system = (
        f"You are Really, an AI real estate superconnecter. You're calling {name} to let them know "
        f"you found a match for them. Be warm and brief — one message, then ask yes or no.\n\n"
        f"The match: {match_name or 'someone'} is a {match_role} in {match_location} — "
        f"{match_summary}.\n\n"
        f"Ask if they'd like to connect. If yes, tell them you'll call them both together shortly. "
        f"If no, say no problem and end the call."
    )

    payload = {
        "assistant": {
            "model": {
                "provider": "openai",
                "model": "gpt-4o",
                "systemPrompt": system,
            },
            "voice": {"provider": "playht", "voiceId": "jennifer"},
            "firstMessage": (
                f"Hey {name}! This is Really — I found a match for you. "
                f"{match_name or 'Someone'} is a {match_role} in {match_location}. "
                f"Would you like to connect with them?"
            ),
            "metadata": {"call_type": "consent", "match_id": str(match_id)},
            "analysisPlan": {
                "structuredDataPrompt": (
                    "Did the user consent to connecting with their match? "
                    "Set consented to true if yes, false if no."
                ),
                "structuredDataSchema": CONSENT_STRUCTURED_SCHEMA,
            },
            "serverUrl": f"{settings.PUBLIC_BASE_URL}/api/vapi/webhook",
        },
        "phoneNumberId": settings.VAPI_PHONE_NUMBER_ID,
        "customer": {"number": phone, "name": name},
    }

    return await _make_call(payload)


async def start_intro_call(phone: str, name: str, other_name: str, other_phone: str) -> str:
    """Call a user to deliver the final intro and give them the other person's number."""
    payload = {
        "assistant": {
            "model": {
                "provider": "openai",
                "model": "gpt-4o",
                "systemPrompt": (
                    "You are Really. You're delivering a successful match introduction. "
                    "Be warm and quick."
                ),
            },
            "voice": {"provider": "playht", "voiceId": "jennifer"},
            "firstMessage": (
                f"Hey {name}! Great news — you and {other_name or 'your match'} both want to connect. "
                f"Their number is {other_phone}. Reach out whenever you're ready. Good luck!"
            ),
            "metadata": {"call_type": "intro"},
            "serverUrl": f"{settings.PUBLIC_BASE_URL}/api/vapi/webhook",
        },
        "phoneNumberId": settings.VAPI_PHONE_NUMBER_ID,
        "customer": {"number": phone, "name": name},
    }

    return await _make_call(payload)


async def _make_call(payload: dict) -> str:
    """POST the call payload to VAPI and return the call ID."""
    import logging

    log = logging.getLogger(__name__)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{VAPI_BASE}/call/phone",
            headers={
                "Authorization": f"Bearer {settings.VAPI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if not r.is_success:
            log.error(f"VAPI error {r.status_code}: {r.text}")
        r.raise_for_status()
        return r.json().get("id", "")


def _role_context(role: str) -> str:
    """Return a role-specific interview focus hint (v1 parity)."""
    ctx = {
        "buyer": "Focus on: target location, budget range, property type, bedrooms, must-haves, timeline.",
        "seller": "Focus on: property address, asking price, property type, key features, timeline to sell.",
        "renter": "Focus on: neighborhood, monthly budget, property type, bedrooms, move-in date.",
        "landlord": "Focus on: property address, monthly rent, property type, availability date.",
        "agent": "Focus on: market specialization, years of experience, what clients they're looking for.",
        "investor": "Focus on: investment strategy, target markets, budget, preferred property types.",
    }
    return ctx.get(role, "")
