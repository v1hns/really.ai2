"""
really.ai v2 — FastAPI application entry point.

Phase 1 skeleton: initialises MongoDB via Beanie on startup and mounts
placeholder routers for each endpoint group.  Router implementations will
be filled in Phase 2.
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
# Placeholder routers (Phase 2 will fill in the implementations)
# ---------------------------------------------------------------------------

# Import routers lazily so that missing Phase-2 files don't break Phase-1
# validation.  Each try/except block registers a stub router when the real
# implementation does not yet exist.

try:
    from app.api.intake import router as intake_router
    app.include_router(intake_router, prefix="/api/intake", tags=["intake"])
    log.debug("Registered intake router.")
except ImportError:
    from fastapi import APIRouter as _APIRouter
    _stub = _APIRouter()

    @_stub.post("/submit")
    async def _intake_stub():  # noqa: D401
        return {"status": "Phase 2 not yet implemented — intake"}

    app.include_router(_stub, prefix="/api/intake", tags=["intake"])
    log.warning("intake router not found — using stub.")

try:
    from app.api.vapi_webhook import router as vapi_router
    app.include_router(vapi_router, prefix="/api/vapi", tags=["vapi"])
    log.debug("Registered vapi_webhook router.")
except ImportError:
    from fastapi import APIRouter as _APIRouter
    _stub = _APIRouter()

    @_stub.post("/webhook")
    async def _vapi_stub():  # noqa: D401
        return {"status": "Phase 2 not yet implemented — vapi_webhook"}

    app.include_router(_stub, prefix="/api/vapi", tags=["vapi"])
    log.warning("vapi_webhook router not found — using stub.")

try:
    from app.api.consent import router as consent_router
    app.include_router(consent_router, prefix="/api/consent", tags=["consent"])
    log.debug("Registered consent router.")
except ImportError:
    from fastapi import APIRouter as _APIRouter
    _stub = _APIRouter()

    @_stub.post("")
    async def _consent_stub():  # noqa: D401
        return {"status": "Phase 2 not yet implemented — consent"}

    app.include_router(_stub, prefix="/api/consent", tags=["consent"])
    log.warning("consent router not found — using stub.")

try:
    from app.api.whatsapp_inbound import router as wa_router
    app.include_router(wa_router, prefix="/api/whatsapp", tags=["whatsapp"])
    log.debug("Registered whatsapp_inbound router.")
except ImportError:
    from fastapi import APIRouter as _APIRouter
    _stub = _APIRouter()

    @_stub.post("/inbound")
    async def _wa_stub():  # noqa: D401
        return {"status": "Phase 2 not yet implemented — whatsapp_inbound"}

    app.include_router(_stub, prefix="/api/whatsapp", tags=["whatsapp"])
    log.warning("whatsapp_inbound router not found — using stub.")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
async def health():
    """Simple liveness probe."""
    return {"status": "ok", "service": "really.ai", "version": "2.0.0"}
