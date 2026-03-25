"""
WhatsApp messaging service for really.ai v2.

Wraps HTTP calls to the internal Node.js whatsapp-web.js bridge.
Endpoints on the bridge:
  POST /send-message  — send a text message
  POST /send-call     — initiate a WhatsApp voice call

Requirement coverage:
  R-WA-10  initiate_call() implemented here
  R-WA-13  call failures caught and retried once after 60 s; persistent failure
            logged and flagged on the Match document.
"""
import asyncio
import logging

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)


async def send_message(wa_id: str, text: str) -> None:
    """
    Send a WhatsApp text message via the Node.js bridge.

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

    Implements R-WA-10 and R-WA-13:
      - On failure, wait 60 seconds and retry once.
      - On persistent failure, log and set match.call_failed = True (if match_id provided).

    Args:
        wa_id:     WhatsApp number in wa_id format.
        match_id:  Optional MongoDB ObjectId string of the Match document to flag on failure.
    """
    url = f"{settings.WHATSAPP_BRIDGE_URL}/send-call"

    async def _attempt() -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
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
        if match_id:
            await _flag_call_failed(match_id)


async def _flag_call_failed(match_id: str) -> None:
    """Set Match.call_failed = True in MongoDB (R-WA-13)."""
    try:
        from app.db.models import Match
        from beanie import PydanticObjectId

        match = await Match.get(PydanticObjectId(match_id))
        if match:
            match.call_failed = True
            await match.save()
    except Exception as exc:
        log.error(f"Failed to flag call_failed on match {match_id}: {exc}")
