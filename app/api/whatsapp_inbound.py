"""
WhatsApp inbound message endpoint for really.ai v2.

POST /api/whatsapp/inbound

Receives forwarded messages from the Node.js whatsapp-web.js bridge and
dispatches them to the conversation handler.

Request body (from Node.js bridge):
    {
        "from": "14155551234@c.us",
        "body": "I'm looking to buy a home in Austin",
        "timestamp": 1711234567
    }

Response: 200 immediately — actual WA reply is sent async by the handler.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core import whatsapp_handler
from app.db.models import ConversationState, User

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class InboundMessage(BaseModel):
    from_: str = Field(..., alias="from", description="Sender wa_id e.g. '14155551234@c.us'")
    body: str = Field(..., description="Message body text")
    timestamp: int = Field(..., description="Unix timestamp of the message")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/inbound")
async def whatsapp_inbound(msg: InboundMessage):
    """
    Receive an inbound WhatsApp message from the Node.js bridge.

    Steps:
    1. Look up User by chat_id (wa_id) in MongoDB.
    2. Create new User if not found (state=GREETING, whatsapp_active=True).
    3. Update last_active timestamp.
    4. Dispatch to whatsapp_handler.handle_message().
    5. Return 200 immediately.
    """
    wa_id = msg.from_

    user = await User.find_one(User.chat_id == wa_id)
    if not user:
        log.info(f"New WhatsApp user: {wa_id}")
        user = User(
            chat_id=wa_id,
            conversation_state=ConversationState.GREETING,
            whatsapp_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            last_active=datetime.now(timezone.utc),
        )
        await user.insert()
    else:
        user.last_active = datetime.now(timezone.utc)
        user.whatsapp_active = True
        await user.save()

    # Dispatch to handler — reply is sent async by the handler
    try:
        await whatsapp_handler.handle_message(user, msg.body)
    except Exception as exc:
        log.error(f"Handler error for {wa_id}: {exc}")

    return {"status": "ok"}
