"""
VAPI webhook endpoint for really.ai v2.

POST /api/vapi/webhook

Ported from v1 app/api/vapi_webhook.py.
Changes vs v1:
  - SQLModel Session replaced with async Beanie calls throughout.
  - After profile save in _handle_intake, calls embeddings.embed_and_save(user)
    before find_matches() (Phase 3 stub — wrapped in try/except).
  - find_matches import wrapped in try/except (Phase 3 will implement it).
  - ConsentRequest.status uses ConsentStatus enum (PENDING/APPROVED/DECLINED)
    instead of v1's boolean consented field.

All branching logic, call routing, and consent state machine preserved verbatim from v1.
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.db.models import (
    ConsentRequest,
    ConsentStatus,
    ConversationState,
    Match,
    User,
    UserRole,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Main webhook handler
# ---------------------------------------------------------------------------


@router.post("/webhook")
async def vapi_webhook(request: Request):
    payload = await request.json()
    msg = payload.get("message", {})
    if msg.get("type") == "end-of-call-report":
        call_type = (msg.get("call", {}).get("metadata") or {}).get("call_type", "intake")
        if call_type == "intake":
            await _handle_intake(msg)
        elif call_type == "consent":
            await _handle_consent(msg)
        # "intro" call type: no-op (just logging), same as v1
        elif call_type == "intro":
            log.info("Intro call completed — no further action required.")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Intake handler
# ---------------------------------------------------------------------------


async def _handle_intake(msg: dict) -> None:
    phone = msg.get("call", {}).get("customer", {}).get("number", "")
    if not phone:
        log.warning("Intake webhook received with no phone number.")
        return

    analysis = msg.get("analysis", {})
    structured = analysis.get("structuredData", {})
    summary = analysis.get("summary", "")
    transcript = msg.get("transcript", "")

    _export_json(phone, structured, summary, transcript)

    user = await User.find_one(User.phone == phone)
    if not user:
        log.warning(f"No user found for phone {phone}")
        return

    _apply_structured(user, structured, summary)
    user.conversation_state = ConversationState.ACTIVE
    user.updated_at = datetime.now(timezone.utc)
    await user.save()

    # Phase 3: generate embedding before matching (R-VEC-04)
    await _try_embed(user)

    # Phase 3: find matches
    try:
        from app.services import matching  # noqa: PLC0415

        matches = await matching.find_matches(user)
        for match in matches:
            # Initiator (new user via intake) is pre-approved
            await ConsentRequest(
                match_id=match.id,
                user_id=user.id,
                status=ConsentStatus.APPROVED,
                responded_at=datetime.now(timezone.utc),
            ).insert()

            # Pending consent for the matched target
            await ConsentRequest(
                match_id=match.id,
                user_id=match.target_id,
                status=ConsentStatus.PENDING,
            ).insert()

            target = await User.get(match.target_id)
            if target:
                await _call_for_consent(target, user, str(match.id))
                log.info(
                    f"Match: {user.phone} ↔ {target.phone} "
                    f"score={match.vector_score:.3f}"
                )
    except ImportError:
        log.warning("matching.py not yet available (Phase 3); skipping match step.")
    except Exception as exc:
        log.error(f"Matching error in vapi_webhook for {phone}: {exc}")


# ---------------------------------------------------------------------------
# Consent handler
# ---------------------------------------------------------------------------


async def _handle_consent(msg: dict) -> None:
    metadata = msg.get("call", {}).get("metadata") or {}
    phone = msg.get("call", {}).get("customer", {}).get("number", "")
    match_id_str = metadata.get("match_id", "")
    consented = (msg.get("analysis", {}).get("structuredData") or {}).get("consented", False)

    if not phone or not match_id_str:
        log.warning("Consent webhook missing phone or match_id.")
        return

    user = await User.find_one(User.phone == phone)
    if not user:
        log.warning(f"No user found for phone {phone} in consent webhook.")
        return

    from beanie import PydanticObjectId  # noqa: PLC0415

    try:
        match_oid = PydanticObjectId(match_id_str)
    except Exception:
        log.error(f"Invalid match_id in consent webhook: {match_id_str!r}")
        return

    cr = await ConsentRequest.find_one(
        ConsentRequest.match_id == match_oid,
        ConsentRequest.user_id == user.id,
    )
    if not cr:
        log.warning(f"ConsentRequest not found for match {match_id_str}, user {user.id}")
        return

    cr.status = ConsentStatus.APPROVED if consented else ConsentStatus.DECLINED
    cr.responded_at = datetime.now(timezone.utc)
    await cr.save()

    if not consented:
        log.info(f"{phone} declined match {match_id_str}")
        return

    # Check if both parties have consented
    match = await Match.get(match_oid)
    if not match:
        log.warning(f"Match {match_id_str} not found.")
        return

    other_id = match.target_id if match.initiator_id != user.id else match.initiator_id
    other_cr = await ConsentRequest.find_one(
        ConsentRequest.match_id == match_oid,
        ConsentRequest.user_id == other_id,
    )

    if not other_cr or other_cr.status != ConsentStatus.APPROVED:
        log.info(f"Waiting on other party for match {match_id_str}")
        return

    # Both consented — trigger introductions
    other_user = await User.get(other_id)
    if not other_user:
        log.warning(f"Other user {other_id} not found for intro.")
        return

    match.introduced = True
    await match.save()

    await _call_intro(user, other_user)
    await _call_intro(other_user, user)
    log.info(f"Intro calls triggered: {user.phone} ↔ {other_user.phone}")


# ---------------------------------------------------------------------------
# Helper: consent call dispatch (WA preferred, VAPI fallback — R-WA-11)
# ---------------------------------------------------------------------------


async def _call_for_consent(to_user: User, new_user: User, match_id: str) -> None:
    from app.core.config import settings  # noqa: PLC0415

    if to_user.whatsapp_active:
        from app.services import whatsapp  # noqa: PLC0415

        consent_msg = (
            f"Hey {to_user.name or 'there'}! Really found a match for you — "
            f"{new_user.name or 'someone'} is a "
            f"{new_user.role.value if new_user.role else 'professional'} "
            f"in {new_user.location or 'your market'}. "
            f"Would you like to connect? Reply YES or NO."
        )
        await whatsapp.send_message(to_user.chat_id, consent_msg)
        try:
            await whatsapp.initiate_call(to_user.chat_id, match_id=match_id)
        except Exception as exc:
            log.error(f"WA consent call to {to_user.chat_id} failed: {exc}")
        return

    # VAPI fallback
    if not settings.VAPI_API_KEY or not to_user.phone:
        log.info(
            f"[VAPI not configured / no phone] Would call {to_user.chat_id} "
            f"for consent on match {match_id}"
        )
        return

    try:
        from app.services.vapi import start_consent_call  # noqa: PLC0415

        await start_consent_call(
            phone=to_user.phone,
            name=to_user.name or "there",
            match_name=new_user.name or "",
            match_role=new_user.role.value if new_user.role else "professional",
            match_location=new_user.location or "your market",
            match_summary=(
                new_user.requirements
                or f"looking in {new_user.location or 'your market'}"
            ),
            match_id=match_id,
        )
    except Exception as exc:
        log.error(f"Consent call failed to {to_user.phone}: {exc}")


# ---------------------------------------------------------------------------
# Helper: intro call dispatch
# ---------------------------------------------------------------------------


async def _call_intro(user: User, other: User) -> None:
    from app.core.config import settings  # noqa: PLC0415

    # Always send WhatsApp intro message (R-WA-12)
    if user.whatsapp_active or user.chat_id:
        from app.services import whatsapp  # noqa: PLC0415

        intro_msg = (
            f"🎉 Great news, {user.name or 'there'}! "
            f"You and {other.name or 'your match'} both want to connect.\n\n"
            f"Name: {other.name or 'Your match'}\n"
            f"Role: {other.role.value.title() if other.role else 'N/A'}\n"
            f"Phone: {other.phone or 'N/A'}\n\n"
            "Reach out whenever you're ready. Good luck! 🏡"
        )
        try:
            await whatsapp.send_message(user.chat_id, intro_msg)
        except Exception as exc:
            log.error(f"WA intro message to {user.chat_id} failed: {exc}")

    # Also attempt VAPI intro call (optional, as in v1)
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
        log.error(f"Intro call failed to {user.phone}: {exc}")


# ---------------------------------------------------------------------------
# Helper: apply structured VAPI data to User document (v1 parity)
# ---------------------------------------------------------------------------


def _apply_structured(user: User, structured: dict, summary: str) -> None:
    field_map = {
        "role",
        "location",
        "budget_min",
        "budget_max",
        "property_types",
        "bedrooms",
        "timeline",
        "requirements",
        "listing_address",
        "listing_price",
        "listing_description",
    }
    for key, val in structured.items():
        if key not in field_map or val is None:
            continue
        if key == "role":
            try:
                val = UserRole(val)
            except ValueError:
                log.warning(f"Unknown role from VAPI: {val!r}")
                continue
        if key == "property_types" and isinstance(val, str):
            val = [p.strip() for p in val.split(",") if p.strip()]
        setattr(user, key, val)

    if not user.requirements and summary:
        user.requirements = summary


# ---------------------------------------------------------------------------
# Helper: profile export (v1 parity — persists JSON artifact to disk)
# ---------------------------------------------------------------------------


def _export_json(phone: str, structured: dict, summary: str, transcript: str) -> None:
    import os

    os.makedirs("exports", exist_ok=True)
    safe = phone.replace("+", "").replace(" ", "_")
    path = f"exports/{safe}_{int(datetime.now(timezone.utc).timestamp())}.json"
    try:
        with open(path, "w") as f:
            json.dump(
                {
                    "phone": phone,
                    "structured_profile": structured,
                    "summary": summary,
                    "transcript": transcript,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
                indent=2,
            )
        log.info(f"Profile exported → {path}")
    except Exception as exc:
        log.error(f"Profile export failed: {exc}")


# ---------------------------------------------------------------------------
# Phase 3 stub: embedding generation
# ---------------------------------------------------------------------------


async def _try_embed(user: User) -> None:
    """
    Regenerate and persist the user's profile embedding before matching.

    Wrapped in try/except so Phase 2 does not break when embeddings.py
    does not yet exist (Phase 3 implements it).
    """
    try:
        from app.services import embeddings  # noqa: PLC0415

        await embeddings.embed_and_save(user)
    except ImportError:
        log.debug("embeddings.py not yet available (Phase 3); skipping embedding in webhook.")
    except Exception as exc:
        log.error(f"Embedding error in vapi_webhook for user {user.id}: {exc}")
