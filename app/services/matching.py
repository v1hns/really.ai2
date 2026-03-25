"""
Vector similarity matching engine for really.ai v2.

Replaces v1's weighted keyword-scoring algorithm with a MongoDB Atlas
$vectorSearch aggregation pipeline while preserving ALL non-scoring logic
from v1 verbatim:

  - Role compatibility matrix (COMPATIBLE_PAIRS) — identical to v1
  - Exclusion of already-matched pairs — identical logic, async Beanie queries
  - Exclusion of users with conversation_state != ACTIVE
  - MATCH_SCORE_THRESHOLD and MAX_MATCHES_PER_USER config variables

Requirements implemented:
  R-VEC-05  Atlas Vector Search query runs in VAPI webhook at same pipeline point
  R-VEC-06  Match documents store vector_score (float)
  R-VEC-07  reason field generated as human-readable summary
  R-VEC-08  MATCH_SCORE_THRESHOLD (default 0.6) and MAX_MATCHES_PER_USER (default 5)
             are environment-variable-configurable
"""
from __future__ import annotations

import logging
from typing import List

from beanie import PydanticObjectId
from motor.motor_asyncio import AsyncIOMotorCollection

from app.core.config import settings
from app.db.models import ConversationState, Match, User, UserRole

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role compatibility matrix — copied verbatim from v1 matching.py
# (AGENT in v1 includes INVESTOR; INVESTOR maps to AGENT + SELLER)
# ---------------------------------------------------------------------------

ROLE_COMPATIBILITY_MAP: dict[UserRole, list[UserRole]] = {
    UserRole.BUYER: [UserRole.SELLER, UserRole.AGENT],
    UserRole.SELLER: [UserRole.BUYER, UserRole.AGENT],
    UserRole.RENTER: [UserRole.LANDLORD, UserRole.AGENT],
    UserRole.LANDLORD: [UserRole.RENTER, UserRole.AGENT],
    UserRole.AGENT: [UserRole.BUYER, UserRole.SELLER, UserRole.RENTER, UserRole.LANDLORD, UserRole.INVESTOR],
    UserRole.INVESTOR: [UserRole.SELLER, UserRole.AGENT],
    UserRole.UNKNOWN: [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_already_matched_ids(user_id: PydanticObjectId) -> list[PydanticObjectId]:
    """
    Return IDs of users already paired with this user (as initiator OR target).

    Replicates v1's exclusion logic — v1 only excluded introduced pairs on the
    initiator side, but the PRD requires excluding all already-matched pairs to
    prevent duplicate Match documents.  We exclude both directions.
    """
    # Matches where this user is the initiator
    initiator_matches = await Match.find(Match.initiator_id == user_id).to_list()
    # Matches where this user is the target
    target_matches = await Match.find(Match.target_id == user_id).to_list()

    excluded: list[PydanticObjectId] = []
    for m in initiator_matches:
        excluded.append(m.target_id)
    for m in target_matches:
        excluded.append(m.initiator_id)

    # Always exclude the user themselves
    excluded.append(user_id)

    return excluded


async def generate_match_reason(user: User, candidate: dict) -> str:
    """
    Build a human-readable explanation of why two users were matched.

    Replicates v1's _build_reason() approach extended to use candidate dict
    fields (the candidate comes from the raw aggregation result).

    Examples:
      "Both in Austin, TX. Budget aligns ($400k–$600k). Both interested in
       single-family homes."
    """
    parts: list[str] = []

    # Location overlap
    user_loc = (user.location or "").strip()
    cand_loc = (candidate.get("location") or "").strip()
    if user_loc and cand_loc:
        # Simple token overlap check (same as v1 _location_score logic)
        u_tokens = set(user_loc.lower().split())
        c_tokens = set(cand_loc.lower().split())
        if u_tokens & c_tokens:
            parts.append(f"Both in {user_loc}")
        else:
            parts.append(f"Markets: {user_loc} / {cand_loc}")
    elif user_loc or cand_loc:
        parts.append(f"Location: {user_loc or cand_loc}")

    # Budget alignment
    user_bmin = user.budget_min
    user_bmax = user.budget_max
    cand_bmin = candidate.get("budget_min")
    cand_bmax = candidate.get("budget_max")

    if user_bmin and user_bmax and cand_bmin and cand_bmax:
        # Check for range overlap
        overlap = user_bmin <= cand_bmax and cand_bmin <= user_bmax
        if overlap:
            lo = max(user_bmin, cand_bmin)
            hi = min(user_bmax, cand_bmax)
            parts.append(f"Budget aligns (${lo:,.0f}–${hi:,.0f})")
        else:
            parts.append("Budget ranges in proximity")
    elif (user_bmin or user_bmax) and (cand_bmin or cand_bmax):
        parts.append("Compatible budget range")

    # Property type match
    user_types: list[str] = user.property_types or []
    cand_types_raw = candidate.get("property_types") or []
    if isinstance(cand_types_raw, str):
        cand_types: list[str] = [p.strip() for p in cand_types_raw.split(",") if p.strip()]
    else:
        cand_types = list(cand_types_raw)

    if user_types and cand_types:
        u_set = {t.lower() for t in user_types}
        c_set = {t.lower() for t in cand_types}
        shared = u_set & c_set
        if shared:
            label = ", ".join(sorted(shared))
            parts.append(f"Both interested in {label}")

    return ". ".join(parts) if parts else "General compatibility"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


async def find_matches(user: User) -> List[Match]:
    """
    Find compatible matches for a given user using MongoDB Atlas Vector Search.

    Steps:
      1. Return [] immediately if user has no embedding.
      2. Determine compatible roles via ROLE_COMPATIBILITY_MAP.
      3. Collect already-matched user IDs to exclude.
      4. Run $vectorSearch aggregation pipeline via Motor directly.
      5. Filter by role, exclusion list, conversation_state == ACTIVE,
         and vector_score >= MATCH_SCORE_THRESHOLD.
      6. Create and insert Match documents for each candidate.
      7. Return the list of Match documents.
    """
    # Step 1: Guard — no embedding means we can't vector-search
    if not user.embedding:
        log.info("find_matches: user %s has no embedding — skipping.", user.id)
        return []

    # Step 2: Role compatibility (identical to v1)
    compatible_roles = ROLE_COMPATIBILITY_MAP.get(user.role, [])
    if not compatible_roles:
        log.info(
            "find_matches: user %s has role %s with no compatible roles — skipping.",
            user.id,
            user.role,
        )
        return []

    # Step 3: Excluded IDs (already matched + self)
    excluded_ids = await _get_already_matched_ids(user.id)

    # Step 4: Atlas $vectorSearch aggregation pipeline (PRD section 5.3.2)
    #
    # We use Motor directly because Beanie's aggregate() helper does not
    # support the $meta "vectorSearchScore" field correctly.
    pipeline = [
        {
            "$vectorSearch": {
                "index": "profile_vector_index",
                "path": "embedding",
                "queryVector": user.embedding,
                "numCandidates": 100,
                "limit": settings.MAX_MATCHES_PER_USER * 3,  # oversample, then filter
            }
        },
        {
            "$match": {
                "role": {"$in": [r.value for r in compatible_roles]},
                "_id": {"$nin": excluded_ids},
                "conversation_state": ConversationState.ACTIVE.value,
            }
        },
        {
            "$addFields": {
                "vector_score": {"$meta": "vectorSearchScore"}
            }
        },
        {
            "$match": {
                "vector_score": {"$gte": settings.MATCH_SCORE_THRESHOLD}
            }
        },
        {"$limit": settings.MAX_MATCHES_PER_USER},
    ]

    collection: AsyncIOMotorCollection = User.get_motor_collection()
    cursor = collection.aggregate(pipeline)
    candidates = await cursor.to_list(length=None)

    log.info(
        "find_matches: user %s — %d candidates after vector search.",
        user.id,
        len(candidates),
    )

    # Step 5-6: Create Match documents
    matches: List[Match] = []
    for candidate in candidates:
        try:
            reason = await generate_match_reason(user, candidate)
            match = Match(
                initiator_id=user.id,
                target_id=candidate["_id"],
                vector_score=float(candidate["vector_score"]),
                reason=reason,
            )
            await match.insert()
            matches.append(match)
            log.info(
                "Match created: %s ↔ %s  score=%.3f",
                user.id,
                candidate["_id"],
                candidate["vector_score"],
            )
        except Exception as exc:
            log.error(
                "Failed to create Match for user %s → candidate %s: %s",
                user.id,
                candidate.get("_id"),
                exc,
            )

    return matches
