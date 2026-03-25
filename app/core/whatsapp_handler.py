"""
WhatsApp message handler for really.ai v2.

Ported from v1 app/core/handler.py.
Changes vs v1:
  - All transport calls replaced: bot.send_message() → HTTP POST to WA bridge via
    app.services.whatsapp.send_message().
  - All DB reads/writes replaced: SQLModel Session → async Beanie calls.
  - find_matches import wrapped in try/except (Phase 3 will implement matching.py).
  - embeddings.embed_and_save called after profile updates (Phase 3 stub).

Internal logic preserved verbatim:
  - Conversation state machine: GREETING → ROLE_SELECTION → PROFILE_BUILDING → ACTIVE.
  - <profile_update> XML parsing and stripping (done in ai.py, applied here).
  - Role detection via profile_update dict.
  - Opt-out / opt-in handling (STOP / START keywords).
  - Context window: last 20 messages from MongoDB.
"""
import logging
from datetime import datetime, timezone

from app.db.models import (
    ConversationState,
    Match,
    Message,
    User,
    UserRole,
)
from app.services import ai, whatsapp

log = logging.getLogger(__name__)

_STOP_WORDS = {"STOP", "UNSUBSCRIBE", "OPTOUT", "OPT OUT", "QUIT", "/STOP"}
_START_WORDS = {"START", "YES", "/START"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def handle_message(user: User, text: str) -> None:
    """
    Process one incoming WhatsApp message for the given user and send a reply.

    The caller (whatsapp_inbound router) has already:
      - Looked up or created the User document.
      - Updated user.last_active.

    This function owns:
      - Opt-out / opt-in gating.
      - Conversation history loading.
      - AI call + profile update application.
      - State machine transitions.
      - WhatsApp reply dispatch.
      - Matching trigger when state reaches ACTIVE.
    """
    # --- Opt-out handling ---------------------------------------------------
    if text.strip().upper() in _STOP_WORDS:
        user.opt_in = False
        await user.save()
        await whatsapp.send_message(
            user.chat_id,
            "You've been unsubscribed from Really. Reply START anytime to opt back in.",
        )
        return

    # --- Opt-back-in --------------------------------------------------------
    if not user.opt_in:
        if text.strip().upper() in _START_WORDS:
            user.opt_in = True
            await user.save()
        else:
            return  # silently drop messages from opted-out users

    # --- First-contact greeting (GREETING state) ----------------------------
    if user.conversation_state == ConversationState.GREETING:
        await _send_welcome(user)
        # Transition to ROLE_SELECTION so the next message goes through AI
        user.conversation_state = ConversationState.ROLE_SELECTION
        user.updated_at = datetime.now(timezone.utc)
        await user.save()
        return

    # --- Load conversation history from MongoDB (last 20) -------------------
    history = (
        await Message.find(Message.user_id == user.id)
        .sort(+Message.created_at)
        .to_list()
    )
    # Keep last 20 for context window (matches v1 spec)
    history = history[-20:]

    # --- Build system context -----------------------------------------------
    system_extra = (
        f"User state: {user.conversation_state.value}. "
        f"User role: {user.role.value if user.role else 'unknown'}. "
        f"Profile so far: location={user.location}, "
        f"budget={user.budget_min}-{user.budget_max}, "
        f"property_types={user.property_types}, "
        f"requirements={user.requirements}."
    )

    # --- Get AI reply -------------------------------------------------------
    reply, profile_update = await ai.get_reply(user, history, text, system_extra)

    # --- Persist conversation turn ------------------------------------------
    await Message(
        user_id=user.id,
        speaker="user",
        content=text,
    ).insert()

    await Message(
        user_id=user.id,
        speaker="assistant",
        content=reply,
    ).insert()

    # --- Apply profile updates ----------------------------------------------
    if profile_update:
        changed = _apply_profile_update(user, profile_update)
        if changed:
            user.updated_at = datetime.now(timezone.utc)
            await user.save()
            # Phase 3: regenerate embedding after profile change
            await _try_embed(user)

    # --- Send reply via WhatsApp bridge ------------------------------------
    await whatsapp.send_message(user.chat_id, reply)

    # --- Trigger matching when profile becomes ACTIVE ----------------------
    if user.conversation_state == ConversationState.ACTIVE:
        await _run_matching(user)


# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------


async def _send_welcome(user: User) -> None:
    """Send the initial greeting to a brand-new WhatsApp user."""
    msg = (
        "👋 Welcome to Really — your AI real estate superconnecter!\n\n"
        "I'll match you with the right buyers, sellers, renters, landlords, agents, "
        "or investors in your market.\n\n"
        "What best describes you right now?\n"
        "• 🏠 Buy a property\n"
        "• 💰 Sell a property\n"
        "• 🔑 Rent a place\n"
        "• 🏢 Rent out my property\n"
        "• 🤝 I'm an agent\n"
        "• 📈 Invest in real estate"
    )
    await whatsapp.send_message(user.chat_id, msg)


# ---------------------------------------------------------------------------
# Profile update application (mirrors v1 _apply_profile_update exactly)
# ---------------------------------------------------------------------------


def _apply_profile_update(user: User, update: dict) -> bool:
    """
    Apply AI-extracted profile fields to the User Beanie document.

    Returns True if any field was changed (so the caller knows to save).
    Also advances conversation_state based on what was updated.
    """
    field_map = {
        "name",
        "role",
        "location",
        "budget_min",
        "budget_max",
        "property_types",
        "bedrooms",
        "requirements",
        "timeline",
        "listing_address",
        "listing_price",
        "listing_description",
    }
    changed = False

    for key, val in update.items():
        if key not in field_map or val is None:
            continue

        if key == "role":
            try:
                val = UserRole(val)
            except ValueError:
                log.warning(f"Unknown role value: {val!r}")
                continue

        if key == "property_types" and isinstance(val, str):
            # v2 stores as list; split comma-separated string from AI
            val = [p.strip() for p in val.split(",") if p.strip()]

        setattr(user, key, val)
        changed = True

    if not changed:
        return False

    # State machine transitions (identical to v1)
    if user.conversation_state == ConversationState.GREETING:
        user.conversation_state = ConversationState.ROLE_SELECTION

    if update.get("role") and user.conversation_state == ConversationState.ROLE_SELECTION:
        user.conversation_state = ConversationState.PROFILE_BUILDING

    if update.get("profile_complete") and user.conversation_state == ConversationState.PROFILE_BUILDING:
        user.conversation_state = ConversationState.ACTIVE

    return True


# ---------------------------------------------------------------------------
# Matching trigger (Phase 3 will provide the real implementation)
# ---------------------------------------------------------------------------


async def _run_matching(user: User) -> None:
    """
    Attempt to find matches for an ACTIVE user.

    Wraps the find_matches import in try/except so Phase 2 does not break when
    matching.py does not yet exist (Phase 3 implements it).
    """
    try:
        from app.services import matching  # noqa: PLC0415
        matches = await matching.find_matches(user)
        for match in matches:
            await _handle_new_match(user, match)
    except ImportError:
        log.warning("matching.py not yet available (Phase 3); skipping match attempt.")
    except Exception as exc:
        log.error(f"Matching error for {user.chat_id}: {exc}")


async def _handle_new_match(user: User, match: Match) -> None:
    """
    React to a newly created Match document:
      - Notify the initiator via WhatsApp.
      - Initiate consent call/message to the target.
    """
    try:
        target = await User.get(match.target_id)
        if not target:
            log.warning(f"Match target {match.target_id} not found.")
            return

        intro_to_user = await ai.build_intro_message(user, target)
        await whatsapp.send_message(
            user.chat_id,
            f"🏡 Great news — I found someone you should meet!\n\n{intro_to_user}\n\n"
            "They've been notified about you too.",
        )

        # Consent flow for the target (prefer WA call, VAPI fallback — R-WA-11)
        await _request_consent(target, user, str(match.id))
    except Exception as exc:
        log.error(f"handle_new_match error: {exc}")


async def _request_consent(to_user: User, new_user: User, match_id: str) -> None:
    """
    Ask the target user for consent to be introduced.

    Prefers a WhatsApp call when the target has an active WA session (R-WA-11),
    falls back to VAPI otherwise.
    """
    from app.core.config import settings  # noqa: PLC0415

    consent_msg = (
        f"Hey {to_user.name or 'there'}! I found a match for you — "
        f"{new_user.name or 'someone'} is a "
        f"{new_user.role.value if new_user.role else 'professional'} "
        f"in {new_user.location or 'your market'}. "
        f"Would you like to connect? Reply YES or NO."
    )

    if to_user.whatsapp_active:
        # Send WA text notification + initiate WA call
        await whatsapp.send_message(to_user.chat_id, consent_msg)
        try:
            await whatsapp.initiate_call(to_user.chat_id, match_id=match_id)
        except Exception as exc:
            log.error(f"WA consent call to {to_user.chat_id} failed: {exc}")
    else:
        # VAPI fallback
        if settings.VAPI_API_KEY and to_user.phone:
            try:
                from app.services.vapi import start_consent_call  # noqa: PLC0415

                await start_consent_call(
                    phone=to_user.phone,
                    name=to_user.name or "there",
                    match_name=new_user.name or "",
                    match_role=new_user.role.value if new_user.role else "professional",
                    match_location=new_user.location or "your market",
                    match_summary=new_user.requirements
                    or f"looking in {new_user.location or 'your market'}",
                    match_id=match_id,
                )
            except Exception as exc:
                log.error(f"VAPI consent call to {to_user.phone} failed: {exc}")
        else:
            # No VAPI configured and no WA session — send plain text fallback
            await whatsapp.send_message(to_user.chat_id, consent_msg)


# ---------------------------------------------------------------------------
# Embedding stub (Phase 3 will implement)
# ---------------------------------------------------------------------------


async def _try_embed(user: User) -> None:
    """
    Regenerate and persist the user's profile embedding.

    Wrapped in try/except so Phase 2 does not break when embeddings.py
    does not yet exist (Phase 3 implements it).
    """
    try:
        from app.services import embeddings  # noqa: PLC0415

        await embeddings.embed_and_save(user)
    except ImportError:
        log.debug("embeddings.py not yet available (Phase 3); skipping embedding.")
    except Exception as exc:
        log.error(f"Embedding error for {user.chat_id}: {exc}")
