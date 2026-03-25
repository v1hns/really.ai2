"""
MongoDB/Beanie document models for really.ai v2.

Replicates all v1 SQLModel fields (User, Message, Match, ConsentRequest) and
adds v2-specific fields: embedding, whatsapp_active, vector_score, ConsentStatus.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from beanie import Document, PydanticObjectId
from pydantic import Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UserRole(str, Enum):
    BUYER = "buyer"
    SELLER = "seller"
    RENTER = "renter"
    LANDLORD = "landlord"
    AGENT = "agent"
    INVESTOR = "investor"
    UNKNOWN = "unknown"


class ConversationState(str, Enum):
    GREETING = "greeting"
    ROLE_SELECTION = "role_selection"
    PROFILE_BUILDING = "profile_building"
    ACTIVE = "active"


class ConsentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class User(Document):
    """
    Represents a real estate participant.

    Identity fields replicate v1 User exactly.
    Profile fields replicate all conversational data collected in v1.
    v2 additions: embedding (1536-dim), whatsapp_active.
    """

    # --- Identity (v1 parity) ---
    chat_id: str = Field(..., description="WhatsApp number in wa_id format, e.g. '14155551234@c.us'")
    phone: Optional[str] = Field(default=None, description="E.164 phone number for VAPI calls")
    email: Optional[str] = None
    name: Optional[str] = None

    # --- Role & conversation state (v1 parity) ---
    role: Optional[UserRole] = Field(default=UserRole.UNKNOWN)
    conversation_state: ConversationState = Field(default=ConversationState.GREETING)

    # --- Profile fields (v1 parity) ---
    location: Optional[str] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    # Stored as a list of strings (v1 stored comma-separated; v2 uses a proper list)
    property_types: Optional[List[str]] = None
    bedrooms: Optional[int] = None
    requirements: Optional[str] = None  # free-text summary of needs
    timeline: Optional[str] = None       # e.g. "3 months", "ASAP"

    # --- Seller / landlord listing fields (v1 parity) ---
    listing_address: Optional[str] = None
    listing_price: Optional[float] = None
    listing_description: Optional[str] = None

    # --- VAPI intake tracking (v1 parity) ---
    vapi_call_id: Optional[str] = None

    # --- Opt-in / consent (v1 parity) ---
    opt_in: bool = True

    # --- v2 additions ---
    embedding: Optional[List[float]] = Field(
        default=None,
        description="1536-dimensional profile embedding from text-embedding-3-small",
    )
    whatsapp_active: bool = Field(
        default=False,
        description="True when the user has an active WhatsApp session with the bridge",
    )

    # --- Timestamps ---
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_active: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "users"
        # Indexes — the Atlas Vector Search index must be created separately
        # via the Atlas UI or CLI (it is not a standard pymongo index).
        indexes = [
            "chat_id",
            "phone",
            "role",
            "conversation_state",
        ]


class Message(Document):
    """
    A single turn in the conversation between a user and the assistant.

    Mirrors v1 Message table.  speaker replaces v1's `role` column to avoid
    confusion with UserRole; both "user" and "assistant" are valid values.
    """

    user_id: PydanticObjectId = Field(..., description="Reference to the User document")
    speaker: str = Field(
        ...,
        description="Who sent this message: 'user' or 'assistant'",
    )
    content: str
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "messages"
        indexes = [
            "user_id",
            [("user_id", 1), ("created_at", -1)],  # compound for history queries
        ]


class Match(Document):
    """
    A scored pairing between two users.

    v1 had a scalar `score` field.  v2 keeps that concept but renames it to
    `vector_score` (cosine similarity, 0.0–1.0) as the PRD requires.
    All other v1 fields are preserved.
    """

    initiator_id: PydanticObjectId = Field(
        ..., description="User who triggered the match (completed intake first)"
    )
    target_id: PydanticObjectId = Field(
        ..., description="Candidate user identified as a compatible match"
    )
    vector_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Cosine similarity score from Atlas Vector Search (0.0–1.0)",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Human-readable explanation of why this pair was matched",
    )
    introduced: bool = Field(
        default=False,
        description="True once both parties have consented and introductions have been sent",
    )
    # Flag set when call initiation fails after retry (see R-WA-13)
    call_failed: bool = Field(
        default=False,
        description="True if the consent/intro call persistently failed after one retry",
    )
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "matches"
        indexes = [
            "initiator_id",
            "target_id",
            [("initiator_id", 1), ("target_id", 1)],  # fast duplicate-pair look-up
        ]


class ConsentRequest(Document):
    """
    Tracks the per-user consent state within a Match.

    One ConsentRequest is created for each party that needs to give consent
    (the initiator is pre-approved; the target gets a ConsentRequest with
    status=PENDING until they respond).
    """

    match_id: PydanticObjectId = Field(..., description="Reference to the Match document")
    user_id: PydanticObjectId = Field(
        ..., description="The user being asked for consent"
    )
    status: ConsentStatus = Field(
        default=ConsentStatus.PENDING,
        description="PENDING | APPROVED | DECLINED",
    )
    responded_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the user gave their YES/NO response",
    )
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "consent_requests"
        indexes = [
            "match_id",
            "user_id",
            [("match_id", 1), ("user_id", 1)],
            "status",
        ]
