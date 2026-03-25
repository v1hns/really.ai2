"""
Profile embedding service for really.ai v2.

Converts a User's profile fields to a natural-language string and encodes
it using OpenAI text-embedding-3-small (1536 dimensions).  The resulting
vector is written back to user.embedding in MongoDB.

Requirements implemented:
  R-VEC-01  expose async embed_profile(user) -> List[float]
  R-VEC-02  persist embedding whenever a profile field changes
  R-VEC-03  never block conversation on API failure — log and return
  R-VEC-04  called at end of VAPI intake webhook before find_matches()
"""
from __future__ import annotations

import logging
from typing import List

from openai import AsyncOpenAI

from app.core.config import settings
from app.db.models import User

log = logging.getLogger(__name__)

# OpenAI model used for profile embeddings (1536 dimensions)
_EMBEDDING_MODEL = "text-embedding-3-small"

# Module-level client — created lazily so the import never fails when
# OPENAI_API_KEY is not yet set (e.g. during test collection).
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def profile_to_text(user: User) -> str:
    """
    Serialize a User's profile fields to a natural-language string.

    Exact format specified in PRD section 5.3.1.  Only non-None fields are
    included so the embedding focuses on what is actually known.
    """
    parts: list[str] = []

    if user.role:
        parts.append(f"Role: {user.role.value}")
    if user.location:
        parts.append(f"Location: {user.location}")
    if user.budget_min and user.budget_max:
        parts.append(f"Budget: ${user.budget_min:,.0f} to ${user.budget_max:,.0f}")
    elif user.budget_max:
        parts.append(f"Budget up to ${user.budget_max:,.0f}")
    if user.property_types:
        parts.append(f"Property types: {', '.join(user.property_types)}")
    if user.timeline:
        parts.append(f"Timeline: {user.timeline}")

    return ". ".join(parts)


async def embed_profile(user: User) -> List[float]:
    """
    Generate a 1536-dimensional embedding vector for the given user's profile.

    Calls the OpenAI Embeddings API (text-embedding-3-small).  On any API
    failure the error is logged and re-raised so the caller can decide how
    to handle it (R-VEC-03: do not silently swallow — caller wraps in
    try/except and does NOT block the conversation).

    Returns:
        List[float] — 1536-element vector of floats.

    Raises:
        Exception — any OpenAI API error is propagated to the caller.
    """
    text = await profile_to_text(user)
    if not text:
        log.warning(
            "embed_profile called for user %s but profile_to_text returned empty string.",
            user.id,
        )
        raise ValueError(f"Cannot embed empty profile for user {user.id}")

    log.debug("Embedding profile for user %s: %r", user.id, text)

    client = _get_client()
    try:
        response = await client.embeddings.create(
            model=_EMBEDDING_MODEL,
            input=text,
        )
    except Exception as exc:
        log.error(
            "OpenAI embeddings API call failed for user %s: %s",
            user.id,
            exc,
        )
        raise  # re-raise so embed_and_save can handle (R-VEC-03)

    vector: List[float] = response.data[0].embedding
    log.debug(
        "Embedding generated for user %s — %d dimensions.", user.id, len(vector)
    )
    return vector


async def embed_and_save(user: User) -> None:
    """
    Generate a fresh embedding for the user and persist it to MongoDB.

    This is the safe, fire-and-forget wrapper used by whatsapp_handler and
    the VAPI webhook.  Any failure is caught, logged, and swallowed so the
    caller's conversation flow is never interrupted (R-VEC-01, R-VEC-02,
    R-VEC-03).

    On success, user.embedding is updated in-place and user.save() is awaited.
    """
    try:
        vector = await embed_profile(user)
        user.embedding = vector
        await user.save()
        log.info("Embedding saved for user %s (%d dims).", user.id, len(vector))
    except Exception as exc:
        # Log and return — never block the conversation (R-VEC-03)
        log.error(
            "embed_and_save failed for user %s — embedding not persisted: %s",
            user.id,
            exc,
        )
