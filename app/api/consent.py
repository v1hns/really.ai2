"""
Consent endpoint for really.ai v2.

POST /api/consent

Ported from the consent handling logic in v1 app/api/vapi_webhook.py
(v1 had no standalone consent.py; consent was part of the VAPI webhook).

This endpoint handles out-of-band YES/NO responses (e.g. from a web form,
SMS keyword reply forwarded here, or WhatsApp message parsed by the inbound
handler and forwarded to this route).

Changes vs v1:
  - SQLModel Session replaced with async Beanie calls.
  - Introduction: sends WhatsApp message to both parties with the other's
    name, role, and phone number (R-WA-12) in addition to the VAPI intro call.
  - ConsentRequest.status uses ConsentStatus enum (PENDING/APPROVED/DECLINED)
    instead of v1's boolean consented field.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from app.db.models import (
    ConsentRequest,
    ConsentStatus,
    Match,
    User,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class ConsentResponse(BaseModel):
    match_id: str
    user_phone: str
    consented: bool


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("")
async def handle_consent(body: ConsentResponse):
    """
    Record a user's YES/NO consent decision and, if both parties have consented,
    trigger the introduction flow.
    """
    from beanie import PydanticObjectId  # noqa: PLC0415

    user = await User.find_one(User.phone == body.user_phone)
    if not user:
        log.warning(f"Consent received for unknown phone: {body.user_phone}")
        return {"status": "user_not_found"}

    try:
        match_oid = PydanticObjectId(body.match_id)
    except Exception:
        log.error(f"Invalid match_id in consent request: {body.match_id!r}")
        return {"status": "invalid_match_id"}

    cr = await ConsentRequest.find_one(
        ConsentRequest.match_id == match_oid,
        ConsentRequest.user_id == user.id,
    )
    if not cr:
        log.warning(f"ConsentRequest not found for match {body.match_id}, user {user.id}")
        return {"status": "consent_request_not_found"}

    cr.status = ConsentStatus.APPROVED if body.consented else ConsentStatus.DECLINED
    cr.responded_at = datetime.now(timezone.utc)
    await cr.save()

    if not body.consented:
        log.info(f"{body.user_phone} declined match {body.match_id}")
        return {"status": "declined"}

    # Check if both parties have consented
    match = await Match.get(match_oid)
    if not match:
        log.warning(f"Match {body.match_id} not found.")
        return {"status": "match_not_found"}

    other_id = (
        match.target_id if match.initiator_id != user.id else match.initiator_id
    )
    other_cr = await ConsentRequest.find_one(
        ConsentRequest.match_id == match_oid,
        ConsentRequest.user_id == other_id,
    )

    if not other_cr or other_cr.status != ConsentStatus.APPROVED:
        log.info(f"Waiting on other party for match {body.match_id}")
        return {"status": "waiting_on_other_party"}

    # Both consented — trigger introduction
    other_user = await User.get(other_id)
    if not other_user:
        log.warning(f"Other user {other_id} not found for intro.")
        return {"status": "other_user_not_found"}

    match.introduced = True
    await match.save()

    await _introduce(user, other_user)
    await _introduce(other_user, user)
    log.info(f"Introductions sent: {user.phone} ↔ {other_user.phone}")

    return {"status": "introduced"}


# ---------------------------------------------------------------------------
# Introduction helpers
# ---------------------------------------------------------------------------


async def _introduce(user: User, other: User) -> None:
    """
    Send a WhatsApp introduction message to `user` about `other` (R-WA-12),
    then optionally attempt a VAPI intro call.
    """
    from app.core.config import settings  # noqa: PLC0415

    # WhatsApp message introduction (always attempted if bridge URL is configured)
    intro_msg = (
        f"🎉 Great news, {user.name or 'there'}! "
        f"You and {other.name or 'your match'} both want to connect.\n\n"
        f"Name: {other.name or 'Your match'}\n"
        f"Role: {other.role.value.title() if other.role else 'N/A'}\n"
        f"Phone: {other.phone or 'N/A'}\n\n"
        "Reach out whenever you're ready. Good luck! 🏡"
    )

    try:
        from app.services import whatsapp  # noqa: PLC0415

        await whatsapp.send_message(user.chat_id, intro_msg)
    except Exception as exc:
        log.error(f"WA intro message to {user.chat_id} failed: {exc}")

    # Optional email introduction via Resend (unchanged from v1)
    if settings.RESEND_API_KEY and user.email and other.email:
        try:
            from app.services import email as email_svc  # noqa: PLC0415

            await email_svc.send_introduction(user, other)
        except ImportError:
            log.debug("email.py not available; skipping email intro.")
        except Exception as exc:
            log.error(f"Email intro to {user.email} failed: {exc}")

    # VAPI intro call (optional; VAPI may not be configured)
    if not settings.VAPI_API_KEY or not user.phone:
        return
    try:
        from app.services.vapi import start_intro_call  # noqa: PLC0415

        await start_intro_call(
            phone=user.phone,
            name=user.name or "there",
            other_name=other.name or "your match",
            other_phone=other.phone or "N/A",
        )
    except Exception as exc:
        log.error(f"VAPI intro call to {user.phone} failed: {exc}")
