"""
OpenAI-powered conversation manager for really.ai v2.

Ported from v1 app/services/ai.py.
Changes vs v1:
  - Conversation history loaded from MongoDB (Beanie Message model) instead of SQLModel.
  - Transport reference changed from Telegram to WhatsApp in docstrings/personality blurb.
  - All prompts, XML tagging, context window size, and GPT-4o model choice preserved verbatim.
"""
import json
import re
from typing import Optional
from openai import AsyncOpenAI
from app.core.config import settings
from app.db.models import User, Message

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

MODEL = "gpt-4o"

SYSTEM_PROMPT = """You are Really, an AI real estate superconnecter on WhatsApp. Your job is to \
understand what someone needs in real estate, build their profile through natural conversation, \
and connect them with the right people.

## Your personality
- Warm, direct, efficient — get to the point fast
- Ask one question at a time, no more
- Never recap or summarize what the user just said — just move to the next question
- No filler phrases like "Great!", "Perfect!", "Got it!" — just respond and move on
- Keep every message under 3 sentences

## Your goals by user role

**Buyers:** Learn their target location(s), budget range, property type (house/condo/apt/townhouse), \
bedrooms needed, must-haves, nice-to-haves, and timeline to purchase.

**Renters:** Learn target neighborhood, monthly budget, property type, bedrooms, move-in date, \
lease length preference, pet/parking needs.

**Sellers:** Learn property address, asking price or price guidance, property type, key features, \
timeline to sell, and if they already have an agent.

**Landlords:** Learn property address, rental price, property type, availability date, \
and tenant preferences.

**Agents:** Learn their market/area specialization, years of experience, transaction volume, \
and current buyer/seller needs so you can make introductions.

**Investors:** Learn investment strategy (flip/BRRRR/buy-and-hold/commercial), target markets, \
budget, preferred property types, and current portfolio size.

## Profile extraction
After each user message, if you have gathered enough to update the profile, include a JSON block \
at the END of your response (it will be parsed and stripped before sending to the user):

<profile_update>
{
  "name": "string or null",
  "role": "buyer|seller|renter|landlord|agent|investor or null",
  "location": "string or null",
  "budget_min": number or null,
  "budget_max": number or null,
  "property_types": "comma-separated or null",
  "bedrooms": number or null,
  "requirements": "free text summary or null",
  "timeline": "string or null",
  "listing_address": "string or null",
  "listing_price": number or null,
  "listing_description": "string or null",
  "profile_complete": true or false
}
</profile_update>

Only include fields you are updating. Set profile_complete to true when you have enough \
information to attempt matching (at minimum: role, location, and one key requirement).

## Matching & introductions
When you learn a match has been found (system will inform you), craft a warm introduction message \
explaining why these two people should connect and share their first names + what they're looking for.

## Important rules
- Never make up listings or prices
- Never share someone's phone number without explicit consent (the system handles intros)
- If someone says STOP or UNSUBSCRIBE, acknowledge and say they've been removed
- Keep messages under 300 words
- Format lists with emoji bullets for readability
"""


def _build_messages(history: list[Message], new_message: str, system_extra: str = "") -> list[dict]:
    """Build the OpenAI messages list from conversation history and the new incoming message."""
    system = SYSTEM_PROMPT
    if system_extra:
        system += f"\n\n## Additional context\n{system_extra}"

    msgs = [{"role": "system", "content": system}]
    # Last 20 messages from history — v1 context window spec
    for m in history[-20:]:
        # v2 Message model uses `speaker` ("user"/"assistant"); map to OpenAI roles
        openai_role = "user" if m.speaker == "user" else "assistant"
        msgs.append({"role": openai_role, "content": m.content})
    msgs.append({"role": "user", "content": new_message})
    return msgs


def _extract_profile_update(text: str) -> tuple[str, Optional[dict]]:
    """Extract and strip <profile_update>…</profile_update> XML block from AI response."""
    pattern = r"<profile_update>(.*?)</profile_update>"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return text.strip(), None
    clean = (text[: match.start()] + text[match.end() :]).strip()
    try:
        update = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        update = None
    return clean, update


async def get_reply(
    user: User,
    history: list[Message],
    incoming_text: str,
    system_extra: str = "",
) -> tuple[str, Optional[dict]]:
    """
    Generate a reply for the given incoming message.

    Args:
        user: The Beanie User document (used for context; not mutated here).
        history: Ordered list of Message documents (loaded from MongoDB by the caller).
        incoming_text: The raw text of the user's latest message.
        system_extra: Optional additional system context injected after the main prompt.

    Returns:
        Tuple of (reply_text, profile_update_dict | None).
        reply_text has the <profile_update> XML stripped.
    """
    messages = _build_messages(history, incoming_text, system_extra)

    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        messages=messages,
    )

    raw = response.choices[0].message.content or ""
    clean, profile_update = _extract_profile_update(raw)
    return clean, profile_update


async def build_intro_message(user_a: User, user_b: User) -> str:
    """Generate an introduction message to send to user_a about user_b."""
    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=256,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Write a warm 2-sentence introduction connecting these two people. "
                    f"Person A: {user_a.name or 'someone'}, role={user_a.role}, "
                    f"location={user_a.location}, requirements={user_a.requirements}, "
                    f"budget={user_a.budget_min}-{user_a.budget_max}. "
                    f"Person B: {user_b.name or 'someone'}, role={user_b.role}, "
                    f"location={user_b.location}, requirements={user_b.requirements}, "
                    f"listing={user_b.listing_description}. "
                    f"Address it to Person A and mention Person B by first name only."
                ),
            },
        ],
    )
    return (response.choices[0].message.content or "").strip()
