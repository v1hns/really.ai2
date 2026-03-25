"""
really.ai v2 — FastAPI application entry point.

Phase 2: all routers now fully implemented and registered directly.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.db.engine import init_db

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Initialise the database connection pool before the server starts serving."""
    log.info("Starting really.ai v2…")
    await init_db()
    log.info("really.ai v2 is ready.")
    yield
    log.info("Shutting down really.ai v2.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


app = FastAPI(
    title="really.ai v2",
    description="WhatsApp-native real estate matchmaker with MongoDB + vector matching.",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routers — Phase 2 implementations
# ---------------------------------------------------------------------------

from app.api.intake import router as intake_router  # noqa: E402
from app.api.vapi_webhook import router as vapi_router  # noqa: E402
from app.api.consent import router as consent_router  # noqa: E402
from app.api.whatsapp_inbound import router as wa_router  # noqa: E402

app.include_router(intake_router, prefix="/api/intake", tags=["intake"])
log.debug("Registered intake router.")

app.include_router(vapi_router, prefix="/api/vapi", tags=["vapi"])
log.debug("Registered vapi_webhook router.")

app.include_router(consent_router, prefix="/api/consent", tags=["consent"])
log.debug("Registered consent router.")

app.include_router(wa_router, prefix="/api/whatsapp", tags=["whatsapp"])
log.debug("Registered whatsapp_inbound router.")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
async def health():
    """Simple liveness probe."""
    return {"status": "ok", "service": "really.ai", "version": "2.0.0"}
