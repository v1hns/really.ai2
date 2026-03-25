"""
MongoDB / Beanie initialisation for really.ai v2.

Call `init_db()` once at application startup (inside the FastAPI lifespan
context manager).  After that, all Beanie Document models are ready to use.
"""
import logging
import os

import motor.motor_asyncio
from beanie import init_beanie

from app.db.models import ConsentRequest, Match, Message, User

log = logging.getLogger(__name__)


async def init_db() -> None:
    """
    Initialise the Motor async client and register all Beanie document models.

    Environment variables read:
    - MONGODB_URI       (required) — MongoDB Atlas connection string
    - MONGODB_DB_NAME   (optional, default: "really_ai") — target database
    """
    mongodb_uri = os.environ.get("MONGODB_URI")
    if not mongodb_uri:
        raise RuntimeError(
            "MONGODB_URI environment variable is not set. "
            "Provide a MongoDB Atlas connection string before starting the server."
        )

    db_name = os.environ.get("MONGODB_DB_NAME", "really_ai")

    log.info("Connecting to MongoDB database '%s'…", db_name)

    client: motor.motor_asyncio.AsyncIOMotorClient = (
        motor.motor_asyncio.AsyncIOMotorClient(mongodb_uri)
    )

    await init_beanie(
        database=client[db_name],
        document_models=[
            User,
            Message,
            Match,
            ConsentRequest,
        ],
    )

    log.info("Beanie initialised — collections: users, messages, matches, consent_requests")
