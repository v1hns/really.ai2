"""
WhatsApp messaging service for really.ai v2.

Wraps HTTP calls to the internal Node.js whatsapp-web.js bridge.
Endpoints on the bridge:
  POST /send-message  — send a text message
  POST /send-call     — initiate a WhatsApp voice call

Requirement coverage:
  R-WA-10  initiate_call() POSTs to {WHATSAPP_BRIDGE_URL}/send-call
  R-WA-13  call failures caught and retried once after 60 s; persistent failure
            logged and flagged on the Match document.
            If match_id is not provided, the most recent non-introduced Match
            where the user is initiator or target is located and flagged.
"""
import asyncio
import logging

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)


async def send_message(wa_id: str, text: str) -> None:
    """
    Send a WhatsApp text message via the Node.js bridge.

    POSTs to {settings.WHATSAPP_BRIDGE_URL}/send-message with JSON body
    {"to": wa_id, "body": text}.  Uses a 10-second timeout.

    Args:
        wa_id:  WhatsApp number in wa_id format (e.g. "14155551234@c.us").
        text:   Plain-text message body.
    """
    url = f"{settings.WHATSAPP_BRIDGE_URL}/send-message"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={"to": wa_id, "body": text})
            if not r.is_success:
                log.error(f"WA bridge send-message failed {r.status_code}: {r.text}")
            r.raise_for_status()
    except Exception as exc:
        log.error(f"send_message to {wa_id} raised: {exc}")
        raise


async def initiate_call(wa_id: str, match_id: str | None = None) -> None:
    """
    Initiate a WhatsApp voice call via the Node.js bridge.

    POSTs to {settings.WHATSAPP_BRIDGE_URL}/send-call.  Uses a 10-second
    timeout.  Implements R-WA-10 and R-WA-13:

      - On HTTP error or any exception: wait 60 seconds (asyncio.sleep(60))
        and retry once.
      - On second failure: log the error, then find the most recent
        non-introduced Match where this user is initiator or target, set
        match.call_failed = True, and await match.save().  If match_id is
        provided it is used directly; otherwise the most recent match is
        queried from MongoDB.

    Args:
        wa_id:     WhatsApp number in wa_id format.
        match_id:  Optional MongoDB ObjectId string of the Match document to
                   flag on failure.  When None, the most recent non-introduced
                   Match for this wa_id is looked up automatically.
    """
    url = f"{settings.WHATSAPP_BRIDGE_URL}/send-call"

    async def _attempt() -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json={"to": wa_id})
                if not r.is_success:
                    log.warning(f"WA bridge send-call {r.status_code}: {r.text}")
                    return False
                return True
        except Exception as exc:
            log.warning(f"initiate_call to {wa_id} attempt failed: {exc}")
            return False

    success = await _attempt()
    if not success:
        log.info(f"Retrying WA call to {wa_id} in 60 s …")
        await asyncio.sleep(60)
        success = await _attempt()

    if not success:
        log.error(f"Persistent WA call failure to {wa_id} (match_id={match_id})")
        await _flag_call_failed(wa_id, match_id)


async def _flag_call_failed(wa_id: str, match_id: str | None) -> None:
    """
    Set Match.call_failed = True in MongoDB (R-WA-13).

    If match_id is provided, look up that specific Match.  Otherwise find the
    most recent non-introduced Match where the user (identified by wa_id) is
    either initiator or target.
    """
    try:
        from app.db.models import Match, User
        from beanie import PydanticObjectId

        if match_id:
            match = await Match.get(PydanticObjectId(match_id))
            if match:
                match.call_failed = True
                await match.save()
                log.info(f"Flagged call_failed on match {match_id}")
            return

        # No match_id provided — find the most recent non-introduced Match
        # for this wa_id (user may be initiator or target).
        user = await User.find_one(User.chat_id == wa_id)
        if not user:
            log.warning(f"_flag_call_failed: no user found for wa_id {wa_id}")
            return

        # Check initiator-side matches first, then target-side
        match: Match | None = None
        for kwargs in [
            {"Match.initiator_id": user.id, "Match.introduced": False},
            {"Match.target_id": user.id, "Match.introduced": False},
        ]:
            candidate = (
                await Match.find(
                    Match.initiator_id == user.id,
                    Match.introduced == False,  # noqa: E712
                )
                .sort(-Match.created_at)
                .first_or_none()
                if "initiator_id" in str(kwargs)
                else await Match.find(
                    Match.target_id == user.id,
                    Match.introduced == False,  # noqa: E712
                )
                .sort(-Match.created_at)
                .first_or_none()
            )
            if candidate:
                match = candidate
                break

        if match:
            match.call_failed = True
            await match.save()
            log.info(f"Flagged call_failed on most recent match {match.id} for {wa_id}")
        else:
            log.warning(f"_flag_call_failed: no non-introduced match found for {wa_id}")
    except Exception as exc:
        log.error(f"Failed to flag call_failed for {wa_id} (match_id={match_id}): {exc}")
