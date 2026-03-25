"""
Intake endpoint for really.ai v2.

POST /api/intake/submit

Ported from v1 app/api/intake.py.
Changes vs v1:
  - SQLModel Session replaced with async Beanie calls.
  - All VAPI call initiation logic preserved identically.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings
from app.db.models import ConversationState, User
from app.services.vapi import start_intake_call

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class IntakeSubmission(BaseModel):
    phone: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/submit")
async def submit_intake(body: IntakeSubmission):
    """
    Accept a phone number, upsert the User in MongoDB, and trigger a VAPI intake call.
    """
    # Normalise to E.164 (+1XXXXXXXXXX) — v1 logic preserved verbatim
    digits = "".join(c for c in body.phone if c.isdigit())
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    phone = f"+1{digits}"

    # Upsert user
    user = await User.find_one(User.phone == phone)
    if not user:
        user = User(
            chat_id=f"phone_{phone}",
            phone=phone,
            conversation_state=ConversationState.PROFILE_BUILDING,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            last_active=datetime.now(timezone.utc),
        )
        await user.insert()
    else:
        user.conversation_state = ConversationState.PROFILE_BUILDING
        user.updated_at = datetime.now(timezone.utc)
        await user.save()

    # Trigger VAPI intake call
    call_id = ""
    if settings.VAPI_API_KEY and settings.VAPI_PHONE_NUMBER_ID:
        try:
            call_id = await start_intake_call(phone=phone)
            user.vapi_call_id = call_id
            await user.save()
            log.info(f"VAPI call started: {call_id} → {phone}")
        except Exception as exc:
            log.error(f"VAPI call failed for {phone}: {exc}")
            raise
    else:
        log.warning("VAPI not configured — skipping intake call")

    return {"status": "calling", "call_id": call_id}
