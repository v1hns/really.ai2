# Product Requirements Document
## really.ai v2 — WhatsApp-Native Real Estate Matchmaker with MongoDB + Vector Matching

**Version:** 2.0
**Date:** 2026-03-24
**Status:** Draft
**References:**
- Source repo: https://github.com/v1hns/really.ai (agent logistics, decision making, conversation flows, matching workflow — all behaviour MUST be replicated exactly unless explicitly superseded below)
- WhatsApp client library: https://github.com/wwebjs/whatsapp-web.js

---

## 1. Executive Summary

really.ai v2 is a full migration and enhancement of the original really.ai platform. It retains the complete agent decision-making model and multi-stage consent/introduction workflow from v1 while making three targeted infrastructure changes:

1. **WhatsApp replaces Telegram/SMS** as the primary messaging channel, using `whatsapp-web.js` for session-based messaging and calling.
2. **MongoDB replaces SQLite/PostgreSQL** as the datastore, enabling flexible document schemas and native vector index support.
3. **Vector embeddings replace keyword scoring** for buyer-seller matching, enabling semantic similarity across location, budget, and property preference.

All agent prompting strategy, role branching, conversation state machine, consent collection flow, and introduction logic defined in the original repo MUST be preserved and replicated precisely.

---

## 2. Goals & Non-Goals

### Goals
- Full feature parity with really.ai v1 agent workflows
- Zero regression on intake → match → consent → introduction pipeline
- WhatsApp as the sole real-time user communication layer (text + call initiation)
- MongoDB Atlas for storage with vector search indexes for matching
- Embedding-based match scoring (cosine similarity on profile embeddings)

### Non-Goals
- Rebuilding the VAPI phone intake system (phone calls remain via VAPI)
- Adding new agent roles beyond those in v1 (buyer, seller, renter, landlord, agent, investor)
- Mobile/web frontend
- Multi-tenancy or SaaS packaging

---

## 3. Background — What Must Be Preserved from v1

> **CRITICAL:** Engineers implementing v2 must read the following v1 files in full before writing any code. The agent behaviour described in these files is authoritative and non-negotiable — it defines the product.

| v1 File | What it defines |
|---|---|
| `app/services/ai.py` | Role-based system prompts, single-question methodology, XML-tagged profile extraction, 20-message context window, 3-sentence response cap |
| `app/services/matching.py` | Weighted three-factor scoring, role compatibility matrix, exclusion rules, threshold/max config |
| `app/services/vapi.py` | Intake call, consent call, and introduction call orchestration |
| `app/api/vapi_webhook.py` | State machine: GREETING → ROLE_SELECTION → PROFILE_BUILDING → ACTIVE, profile extraction and storage on call completion |
| `app/core/handler.py` | Message routing, profile update parsing, conversation dispatch |
| `app/db/models.py` | Data relationships: User, Message, Match, ConsentRequest |
| `demo.py` | End-to-end flow reference — use as acceptance test specification |

---

## 4. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          USER DEVICE                            │
│                    (WhatsApp Mobile/Web)                        │
└────────────────────────┬────────────────────────────────────────┘
                         │ WhatsApp Messages / Calls
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   whatsapp-web.js Client                        │
│              (Node.js session — persistent auth)                │
│   • Receives inbound messages                                   │
│   • Sends text replies                                          │
│   • Initiates WhatsApp calls (PTT / voice call API)            │
└────────────────────────┬────────────────────────────────────────┘
                         │ Internal HTTP / event bridge
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI Backend (Python)                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  API Layer                                               │   │
│  │  POST /api/intake/submit    — form-based intake trigger  │   │
│  │  POST /api/vapi/webhook     — VAPI call completion hook  │   │
│  │  POST /api/consent          — consent YES/NO handler     │   │
│  │  POST /api/whatsapp/inbound — WA message webhook         │   │
│  └──────────────────────────┬──────────────────────────────┘   │
│                             │                                   │
│  ┌──────────────────────────▼──────────────────────────────┐   │
│  │  Service Layer                                           │   │
│  │  ai.py          — GPT-4o conversation engine (v1 exact)  │   │
│  │  matching.py    — vector similarity matching             │   │
│  │  vapi.py        — VAPI intake/consent/intro calls        │   │
│  │  whatsapp.py    — WA messaging + call dispatch           │   │
│  │  embeddings.py  — profile → vector encoding              │   │
│  └──────────────────────────┬──────────────────────────────┘   │
│                             │                                   │
│  ┌──────────────────────────▼──────────────────────────────┐   │
│  │  Data Layer (MongoDB Atlas)                              │   │
│  │  users collection       — profiles + conversation state  │   │
│  │  messages collection    — dialogue history               │   │
│  │  matches collection     — scored pairs                   │   │
│  │  consent_requests coll  — async consent workflow state   │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
         │ VAPI webhooks / calls
         ▼
   VAPI (phone intake + consent + intro calls) — unchanged from v1
```

---

## 5. Feature Requirements

### 5.1 WhatsApp Messaging Layer

#### 5.1.1 Client Setup (whatsapp-web.js)

**Library:** `whatsapp-web.js` (https://github.com/wwebjs/whatsapp-web.js)

The implementation must use `whatsapp-web.js` running as a persistent Node.js process alongside the Python FastAPI backend. Authentication must use `LocalAuth` strategy so the session survives restarts without re-scanning a QR code in production.

```
// Minimum required initialization
const { Client, LocalAuth } = require('whatsapp-web.js');

const client = new Client({
    authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
    puppeteer: { args: ['--no-sandbox', '--disable-setuid-sandbox'] }
});
```

The Node.js process exposes a lightweight Express HTTP server (internal only, not public-facing) that the Python FastAPI backend calls to send messages and initiate calls. The Node.js process forwards inbound WhatsApp messages to the Python backend via HTTP POST to `/api/whatsapp/inbound`.

**Requirements:**
- `R-WA-01`: The client MUST maintain a persistent authenticated session using `LocalAuth`.
- `R-WA-02`: On startup, if no session exists, emit a QR code to stdout for initial scan.
- `R-WA-03`: The internal Express server MUST expose:
  - `POST /send-message` — sends a text message to a WhatsApp number
  - `POST /send-call` — initiates a WhatsApp voice call to a number
- `R-WA-04`: All inbound messages (`client.on('message', ...)`) MUST be forwarded to the Python backend at `POST /api/whatsapp/inbound` with `{ from, body, timestamp }`.
- `R-WA-05`: The client MUST handle reconnection automatically on session drop.

#### 5.1.2 Replacing Telegram

All logic currently in `app/core/handler.py` (Telegram-specific message routing, profile extraction dispatch, conversation state transitions) MUST be ported to a new `app/core/whatsapp_handler.py`. The internal logic — role detection, `<profile_update>` XML parsing, conversation state machine — MUST be identical to the Telegram handler. Only the transport layer changes.

**Reference:** Read `app/core/handler.py` in v1 in full. Replicate every branch and fallback path.

**Requirements:**
- `R-WA-06`: `whatsapp_handler.py` MUST handle the same conversation states as v1: `GREETING`, `ROLE_SELECTION`, `PROFILE_BUILDING`, `ACTIVE`.
- `R-WA-07`: Profile update parsing (`<profile_update>...</profile_update>` XML extraction) MUST work identically to v1.
- `R-WA-08`: Message history context (last 20 messages) MUST be loaded from MongoDB on each request, matching v1 behaviour.
- `R-WA-09`: The system MUST strip `<profile_update>` XML from the AI response before sending to the user, exactly as v1 does.

#### 5.1.3 WhatsApp Calls

WhatsApp voice calls are used as an alternative or supplement to VAPI phone calls for the consent and introduction stages. When a user's `phone` field is a WhatsApp number (international format, e.g., `+14155551234`), the system MUST prefer a WhatsApp call over a VAPI call for consent requests and introductions.

**Requirements:**
- `R-WA-10`: The `whatsapp.py` service MUST expose `initiate_call(wa_number: str)` which POSTs to the Node.js `/send-call` endpoint.
- `R-WA-11`: For consent requests, if the target user has an active WhatsApp session (determined by checking the `whatsapp_active` flag on the User document), call via WhatsApp; otherwise fall back to VAPI.
- `R-WA-12`: For introduction calls, both parties MUST receive a WhatsApp message notification with the other party's name, role, and contact number in addition to (or instead of) the VAPI intro call.
- `R-WA-13`: Call initiation failures MUST be caught and retried once after 60 seconds; persistent failure MUST be logged and flagged on the Match document.

---

### 5.2 MongoDB Migration

#### 5.2.1 Database Setup

Replace all SQLModel/SQLAlchemy code with **Motor** (async MongoDB driver for Python) or **Beanie** (ODM built on Motor). Beanie is preferred for its Pydantic model compatibility.

**Collections and document schemas** MUST replicate all fields from the v1 SQLModel entities in `app/db/models.py`. Additional MongoDB-specific fields (e.g., `_id`, `embedding`, `whatsapp_active`) are additive.

**Requirements:**
- `R-DB-01`: Use MongoDB Atlas as the deployment target. Local `mongod` is acceptable for development.
- `R-DB-02`: All four v1 entities (User, Message, Match, ConsentRequest) MUST be implemented as MongoDB collections with equivalent field coverage.
- `R-DB-03`: The `users` collection MUST include a `embedding` field (list of floats, 1536-dimensional for `text-embedding-3-small`) that stores the user's latest profile embedding.
- `R-DB-04`: The `users` collection MUST include a `whatsapp_active` boolean field indicating whether the user has an active WA session.
- `R-DB-05`: The `matches` collection MUST store `vector_score` (float, 0.0–1.0, cosine similarity) alongside any legacy scalar scores for auditability.
- `R-DB-06`: All queries that were synchronous in v1 MUST become async/await compatible with Motor/Beanie.
- `R-DB-07`: Create a MongoDB Atlas Vector Search index on `users.embedding` field named `profile_vector_index` with 1536 dimensions and cosine similarity metric.

#### 5.2.2 Schema Reference

Below is the target MongoDB document shape. Implement these as Beanie `Document` models:

```python
# users collection
class User(Document):
    # Identity (v1 parity)
    chat_id: str                        # WhatsApp number (wa_id), e.g. "14155551234@c.us"
    phone: Optional[str]
    email: Optional[str]
    name: Optional[str]

    # Profile (v1 parity)
    role: Optional[UserRole]            # BUYER | SELLER | RENTER | LANDLORD | AGENT | INVESTOR
    location: Optional[str]
    budget_min: Optional[float]
    budget_max: Optional[float]
    property_types: Optional[List[str]]
    timeline: Optional[str]

    # Conversation state (v1 parity)
    conversation_state: ConversationState  # GREETING | ROLE_SELECTION | PROFILE_BUILDING | ACTIVE

    # v2 additions
    embedding: Optional[List[float]]    # 1536-dim profile embedding
    whatsapp_active: bool = False
    created_at: datetime
    updated_at: datetime

    class Settings:
        name = "users"

# messages collection
class Message(Document):
    user_id: PydanticObjectId
    speaker: str                        # "user" | "assistant"
    content: str
    created_at: datetime

    class Settings:
        name = "messages"

# matches collection
class Match(Document):
    initiator_id: PydanticObjectId
    target_id: PydanticObjectId
    vector_score: float                 # cosine similarity (0.0–1.0)
    reason: Optional[str]
    introduced: bool = False
    created_at: datetime

    class Settings:
        name = "matches"

# consent_requests collection
class ConsentRequest(Document):
    match_id: PydanticObjectId
    user_id: PydanticObjectId           # the party being asked
    status: ConsentStatus               # PENDING | APPROVED | DECLINED
    responded_at: Optional[datetime]

    class Settings:
        name = "consent_requests"
```

---

### 5.3 Vector Embedding Matching

This section replaces the weighted keyword-scoring algorithm in v1's `app/services/matching.py` with a vector similarity approach. The existing role-compatibility rules, exclusion logic, and threshold/max-matches config MUST be preserved exactly — only the similarity calculation changes.

#### 5.3.1 Profile Embedding

When a user's profile is updated (any of: `location`, `budget_min`, `budget_max`, `property_types`, `timeline` changes), generate a new embedding and write it to `users.embedding`.

**Embedding input format** — serialize the profile to a natural-language string before encoding:

```python
def profile_to_text(user: User) -> str:
    parts = []
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
```

**Model:** `text-embedding-3-small` (OpenAI, 1536 dimensions). This is the same OpenAI dependency already present in v1.

**Requirements:**
- `R-VEC-01`: `embeddings.py` MUST expose `async def embed_profile(user: User) -> List[float]` which calls the OpenAI Embeddings API and returns a 1536-dim vector.
- `R-VEC-02`: Embeddings MUST be regenerated and persisted to MongoDB any time a profile field changes during conversation.
- `R-VEC-03`: If the OpenAI Embeddings API call fails, the system MUST not block the conversation — log the error and retry embedding generation as a background task.
- `R-VEC-04`: Embeddings MUST also be generated at the end of the VAPI intake webhook (matching the point in v1 where `find_matches()` is called) before matching runs.

#### 5.3.2 Vector Similarity Matching

Replace v1's `matching.py` `find_matches()` with a MongoDB Atlas Vector Search query. All non-scoring logic from v1 MUST be preserved:

- Role compatibility matrix (buyers match sellers/agents; renters match landlords; etc.)
- Exclusion of already-matched pairs
- Exclusion of users with `conversation_state != ACTIVE`
- `MATCH_SCORE_THRESHOLD` and `MAX_MATCHES_PER_USER` config variables

**Reference:** Read `app/services/matching.py` in v1 line-by-line. Every `if` branch that is not part of the score calculation MUST be ported verbatim.

```python
# app/services/matching.py (v2 — pseudocode)

async def find_matches(user: User) -> List[Match]:
    if not user.embedding:
        return []

    compatible_roles = ROLE_COMPATIBILITY_MAP[user.role]   # identical to v1
    excluded_ids = await get_already_matched_ids(user.id)  # identical logic to v1

    # MongoDB Atlas $vectorSearch aggregation
    pipeline = [
        {
            "$vectorSearch": {
                "index": "profile_vector_index",
                "path": "embedding",
                "queryVector": user.embedding,
                "numCandidates": 100,
                "limit": MAX_MATCHES_PER_USER * 3,         # oversample, then filter
            }
        },
        {
            "$match": {
                "role": {"$in": [r.value for r in compatible_roles]},
                "_id": {"$nin": excluded_ids},
                "conversation_state": "ACTIVE",
            }
        },
        {
            "$addFields": {
                "vector_score": {"$meta": "vectorSearchScore"}
            }
        },
        {
            "$match": {
                "vector_score": {"$gte": MATCH_SCORE_THRESHOLD}
            }
        },
        {"$limit": MAX_MATCHES_PER_USER}
    ]

    candidates = await User.aggregate(pipeline).to_list()
    matches = []
    for candidate in candidates:
        match = Match(
            initiator_id=user.id,
            target_id=candidate["_id"],
            vector_score=candidate["vector_score"],
            reason=generate_match_reason(user, candidate),  # same reason text as v1
        )
        await match.insert()
        matches.append(match)
    return matches
```

**Requirements:**
- `R-VEC-05`: The Atlas Vector Search query MUST be executed as part of the VAPI webhook handler, at the same point in the pipeline where v1 calls `find_matches()`.
- `R-VEC-06`: Match documents MUST store `vector_score` (float).
- `R-VEC-07`: The `reason` field on Match MUST still be generated as a human-readable summary (e.g., "Both are in Austin, TX. Budget overlap. Both interested in single-family homes.") — replicate v1's reason-generation approach.
- `R-VEC-08`: `MATCH_SCORE_THRESHOLD` (default: 0.6) and `MAX_MATCHES_PER_USER` (default: 5) MUST be environment-variable-configurable, identical to v1.

---

### 5.4 Agent Conversation Engine — Full v1 Parity

This section is non-negotiable. The following v1 behaviours MUST be replicated identically in v2:

#### 5.4.1 System Prompts and Role Branching

**Reference:** `app/services/ai.py` in v1.

- The AI assistant personality ("warm, direct, efficient") MUST be preserved.
- Role-specific system prompts MUST be used — separate prompt variants for buyer, seller, renter, landlord, agent, investor.
- The single-question methodology MUST be enforced: the AI asks exactly one question per turn, never stacking multiple questions.
- Responses MUST be capped at 3 sentences.
- Filler language ("Great!", "Absolutely!", "Of course!") MUST be minimised as defined in v1 prompts.
- The `<profile_update>` XML tagging convention MUST be preserved for structured data extraction from AI responses.

#### 5.4.2 Conversation State Machine

**Reference:** `app/api/vapi_webhook.py` and `app/core/handler.py` in v1.

States: `GREETING → ROLE_SELECTION → PROFILE_BUILDING → ACTIVE`

- `GREETING`: Welcome message sent. Next message triggers role detection.
- `ROLE_SELECTION`: AI determines user type. Role stored when identified.
- `PROFILE_BUILDING`: AI collects location, budget, property types, timeline via single questions. Each response triggers profile update + embedding regeneration.
- `ACTIVE`: Profile complete. Matching runs. Consent workflow starts.

State transitions MUST match v1 exactly. Do not add new states or skip states.

#### 5.4.3 Intake via VAPI (Unchanged)

VAPI phone call intake is preserved from v1 with no changes to call flow, prompts, or webhook handling. The only change: after the VAPI webhook completes and the profile is stored, it triggers vector embedding generation before calling `find_matches()`.

#### 5.4.4 Consent Workflow

**Reference:** `app/api/vapi_webhook.py` consent handling and `app/services/vapi.py` consent call in v1.

The multi-stage consent pipeline MUST be preserved:

1. New user completes intake → `ConsentRequest` docs created for all matches (initiator pre-approved).
2. Consent call placed to target (WhatsApp call preferred, VAPI fallback).
3. Target responds YES → `ConsentRequest.status = APPROVED`, `responded_at` set.
4. Both parties consented → `Match.introduced = True`.
5. Introduction: WhatsApp message + optional VAPI intro call to both parties with each other's contact info.
6. Optional email via Resend API (unchanged from v1).

#### 5.4.5 Message History Context

- Last 20 messages per user MUST be loaded from MongoDB `messages` collection on each AI call.
- Messages MUST be stored with `speaker: "user"` or `speaker: "assistant"` (identical to v1).
- The context window management from v1 (most recent 20, no truncation within that window) MUST be replicated.

---

## 6. API Endpoints

All v1 endpoints are preserved. One new endpoint is added for WhatsApp inbound:

| Method | Path | Description |
|---|---|---|
| POST | `/api/intake/submit` | Form-based intake trigger (unchanged) |
| POST | `/api/vapi/webhook` | VAPI call completion + profile extraction (unchanged logic, MongoDB writes) |
| POST | `/api/consent` | Consent YES/NO handler (unchanged) |
| POST | `/api/whatsapp/inbound` | Receives forwarded inbound WA messages from Node.js bridge |
| POST | `/api/calls` | Twilio fallback voice handling (unchanged) |

### `/api/whatsapp/inbound` Specification

**Request body:**
```json
{
  "from": "14155551234@c.us",
  "body": "I'm looking to buy a home in Austin",
  "timestamp": 1711234567
}
```

**Processing:**
1. Look up User by `chat_id = from` in MongoDB.
2. If user not found, create new User with `chat_id = from`, `conversation_state = GREETING`, `whatsapp_active = True`.
3. Dispatch to `whatsapp_handler.py` — identical routing logic to v1's Telegram handler.
4. Store inbound message in `messages` collection.
5. Call `ai.py` conversation engine with role-appropriate system prompt and message history.
6. Parse `<profile_update>` XML from AI response, update User document, regenerate embedding.
7. Strip XML from response, send clean text via WhatsApp.
8. If state transitions to `ACTIVE`, trigger `find_matches()`.

**Response:** `200 OK` (async processing; actual WA reply sent via Node.js bridge)

---

## 7. Configuration & Environment Variables

All v1 environment variables are preserved. New variables added for v2:

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGODB_URI` | Yes | — | MongoDB Atlas connection string |
| `MONGODB_DB_NAME` | No | `really_ai` | Database name |
| `WHATSAPP_BRIDGE_URL` | Yes | `http://localhost:3001` | Internal URL of Node.js WA bridge |
| `OPENAI_API_KEY` | Yes | — | Used for GPT-4o (chat) + text-embedding-3-small |
| `VAPI_API_KEY` | No | — | VAPI for phone calls (fallback for non-WA users) |
| `VAPI_PHONE_NUMBER_ID` | No | — | VAPI number |
| `RESEND_API_KEY` | No | — | Email introductions |
| `MATCH_SCORE_THRESHOLD` | No | `0.6` | Minimum vector similarity score for a valid match |
| `MAX_MATCHES_PER_USER` | No | `5` | Maximum matches triggered per user |
| `PUBLIC_BASE_URL` | Yes | — | Webhook callback base URL |
| `DEBUG` | No | `false` | Verbose logging |

Variables removed: `TELEGRAM_BOT_TOKEN`, `DATABASE_URL` (SQLite/PostgreSQL).

---

## 8. Implementation Phases

### Phase 1: Infrastructure Setup
- Provision MongoDB Atlas cluster with vector search enabled (M10+ tier required for Atlas Vector Search)
- Create `profile_vector_index` vector index on `users.embedding`
- Port all SQLModel models to Beanie Document models
- Write and validate data migration script (if any existing v1 data needs migrating)
- Set up Node.js `whatsapp-web.js` bridge process with `LocalAuth`, QR onboarding, and internal Express API

### Phase 2: Agent Parity Migration
- Port `app/services/ai.py` — zero functional changes, swap SQLModel session reads for MongoDB async reads
- Port `app/core/handler.py` → `app/core/whatsapp_handler.py` — replicate all branches, swap Telegram transport for WA bridge HTTP calls
- Port `app/api/vapi_webhook.py` — swap DB calls, add embedding generation step before `find_matches()`
- Port `app/api/consent.py` — swap DB calls, add WhatsApp call path for consent

### Phase 3: Vector Matching
- Implement `app/services/embeddings.py` — `embed_profile()` using `text-embedding-3-small`
- Rewrite `app/services/matching.py` — Atlas `$vectorSearch` pipeline, preserve all non-scoring logic from v1
- Add embedding regeneration hooks in `whatsapp_handler.py` on profile field changes
- Validate match quality against v1 demo scenarios using `demo.py` as reference test cases

### Phase 4: WhatsApp Call Integration
- Implement `initiate_call()` in `app/services/whatsapp.py`
- Add `whatsapp_active` detection logic in consent workflow
- Update introduction flow to send WhatsApp message + optional VAPI call
- Test consent collection end-to-end via WhatsApp

### Phase 5: QA & Validation
- Run all scenarios from `demo.py` against the v2 system
- Validate: intake → profile complete → embedding generated → matches found (vector score ≥ 0.6) → consent WA call → introduction message
- Load test MongoDB queries with Atlas vector search
- Verify session persistence across Node.js restarts

---

## 9. Acceptance Criteria

The following scenarios from v1's `demo.py` MUST pass unchanged:

1. **New user via WhatsApp** → sends first message → system greets, asks role → user says "buyer" → system asks location → user provides location → system asks budget → user provides range → system asks property type → user provides type → state transitions to `ACTIVE`.
2. **Post-intake matching** → after state reaches `ACTIVE`, `find_matches()` returns at least one result when a compatible seller exists in MongoDB with vector score ≥ 0.6.
3. **Consent collection** → matched seller receives WhatsApp call/message requesting consent → responds YES → `ConsentRequest.status = APPROVED`.
4. **Introduction** → both parties consented → both receive WhatsApp message with the other's contact info → `Match.introduced = True`.
5. **Re-entry** → user sends another WhatsApp message after reaching `ACTIVE` state → system responds contextually using message history → does not restart intake.
6. **Session recovery** → Node.js `whatsapp-web.js` process restarts → `LocalAuth` restores session without QR rescan → inbound messages continue routing correctly.

---

## 10. Technical Notes & Risks

| Risk | Mitigation |
|---|---|
| `whatsapp-web.js` session instability (WhatsApp may invalidate web sessions) | Implement health-check endpoint on Node.js bridge; alert on session drop; use `LocalAuth` with persistent volume in production |
| Atlas Vector Search requires M10+ cluster (not free tier) | Document in setup guide; provide fallback to cosine similarity computed in Python for local dev |
| OpenAI embedding API latency adding to message response time | Generate embeddings async after responding to user; never block user reply on embedding call |
| WhatsApp ToS — automated messaging | Scope to personal/business WhatsApp numbers owned by the operator; do not use for mass outreach |
| MongoDB async session management differs from SQLModel sync | Use Beanie's motor-based async context throughout; never mix sync/async DB calls |

---

## 11. Out of Scope (Deferred to v3)

- Multi-language support
- Agent performance analytics dashboard
- Buyer saved search / listing alerts
- WhatsApp Business API (Cloud API) migration — v2 uses `whatsapp-web.js` which wraps the web client; Cloud API migration is a separate workstream
- Fine-tuned embedding model on real estate domain corpus

---

*End of PRD*
