# really.ai v2

really.ai v2 is a WhatsApp-native real estate matchmaking platform. It preserves the complete agent decision-making model, multi-stage consent/introduction workflow, and conversation state machine from the original [really.ai v1](https://github.com/v1hns/really.ai) while replacing three infrastructure layers: Telegram is replaced by WhatsApp (via `whatsapp-web.js`), SQLite/PostgreSQL is replaced by MongoDB Atlas (with native vector search), and keyword-based scoring is replaced by semantic embedding similarity using OpenAI `text-embedding-3-small`. The result is an end-to-end pipeline from WhatsApp intake through profile building, vector matching, consent collection, and introduction — entirely over WhatsApp with VAPI phone calls as a fallback.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API backend | Python 3.11+, FastAPI, Uvicorn |
| Database | MongoDB Atlas (M10+ for Vector Search), Beanie ODM |
| WhatsApp bridge | Node.js, whatsapp-web.js, Express |
| AI conversation engine | OpenAI GPT-4o (`gpt-4o`) |
| Profile embeddings | OpenAI `text-embedding-3-small` (1536 dimensions) |
| Phone intake / fallback | VAPI |
| Email introductions | Resend (optional) |

---

## Setup

### 1. MongoDB Atlas — Cluster and Vector Index

1. Create a free or paid MongoDB Atlas cluster. **Vector Search requires M10 or higher** (not the free M0 tier).
2. Create a database named `really_ai` (or override with `MONGODB_DB_NAME`).
3. After the cluster is ready, open **Atlas Search → Create Index** on the `users` collection, select **Vector Search**, and paste the following index definition:

```json
{
  "name": "profile_vector_index",
  "type": "vectorSearch",
  "fields": [
    {
      "type": "vector",
      "path": "embedding",
      "numDimensions": 1536,
      "similarity": "cosine"
    }
  ]
}
```

This definition is also stored in `app/db/atlas_index.json` for reference.

4. Copy your Atlas connection string — you will need it for `MONGODB_URI`.

### 2. Environment Variables

```bash
cp .env.example .env
# Fill in all required values — see the Environment Variables section below.
```

### 3. Python Backend

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

The API will be available at `http://localhost:8000`. The `/health` endpoint confirms the service is running.

### 4. Node.js WhatsApp Bridge

```bash
cd bridge
npm install
node index.js
```

The bridge listens on port 3001 by default and exposes:
- `POST /send-message` — send a text message to a WhatsApp number
- `POST /send-call` — initiate a WhatsApp voice call

### 5. First-Run QR Scan

On first startup, `whatsapp-web.js` will print a QR code to the terminal. Scan it with the WhatsApp app on your phone (**Linked Devices → Link a Device**). The session is persisted via `LocalAuth` (stored in `bridge/.wwebjs_auth`) so subsequent restarts do not require rescanning.

---

## End-to-End Flow

```
User (WhatsApp)
     |
     | sends message
     v
whatsapp-web.js bridge (Node.js, port 3001)
     |
     | POST /api/whatsapp/inbound  { from, body, timestamp }
     v
FastAPI backend
     |
     |-- Look up / create User document in MongoDB
     |-- Route to whatsapp_handler.py
     |     |
     |     |-- Load last 20 messages from MongoDB
     |     |-- Call GPT-4o (ai.py) with role-appropriate system prompt
     |     |-- Parse <profile_update> XML, update User document
     |     |-- Regenerate profile embedding (embeddings.py → OpenAI)
     |     |-- Save updated User to MongoDB
     |     |-- Send reply via bridge POST /send-message
     |     |
     |     `-- If state → ACTIVE:
     |           |-- embed_and_save(user)
     |           |-- matching.find_matches(user)  ← Atlas $vectorSearch
     |           |-- For each match:
     |                 |-- Create ConsentRequest docs
     |                 |-- Send consent WhatsApp message to target
     |                 `-- initiate_call(target) via bridge POST /send-call
     |
     |-- VAPI intake webhook: POST /api/vapi/webhook
     |     |-- Extract structured profile from VAPI analysis
     |     |-- Save User, set state = ACTIVE
     |     |-- embed_and_save(user)
     |     `-- matching.find_matches(user) → consent workflow
     |
     `-- Consent: POST /api/consent
           |-- Record YES/NO
           `-- Both consented → send WhatsApp intro message to both parties
                                 (name, role, phone number)  → Match.introduced = True
```

---

## Environment Variables

All variables are documented in `.env.example`. Required variables:

| Variable | Description |
|---|---|
| `MONGODB_URI` | MongoDB Atlas connection string |
| `WHATSAPP_BRIDGE_URL` | Internal URL of the Node.js bridge (default: `http://localhost:3001`) |
| `OPENAI_API_KEY` | Used for GPT-4o chat and `text-embedding-3-small` embeddings |
| `PUBLIC_BASE_URL` | Public-facing URL for VAPI webhooks and consent callbacks |

Optional:

| Variable | Default | Description |
|---|---|---|
| `MONGODB_DB_NAME` | `really_ai` | MongoDB database name |
| `VAPI_API_KEY` | — | VAPI phone call integration (fallback for non-WA users) |
| `VAPI_PHONE_NUMBER_ID` | — | VAPI outbound number |
| `RESEND_API_KEY` | — | Resend email introductions |
| `MATCH_SCORE_THRESHOLD` | `0.6` | Minimum cosine similarity for a valid match |
| `MAX_MATCHES_PER_USER` | `5` | Maximum matches triggered per user |
| `DEBUG` | `false` | Verbose logging |

See `.env.example` for the full reference with comments.

---

## Agent Logic Reference

All agent prompting strategy, conversation state machine, role-based system prompts, single-question methodology, `<profile_update>` XML extraction, and the consent/introduction workflow are preserved verbatim from [really.ai v1](https://github.com/v1hns/really.ai). Refer to the v1 repository for the authoritative agent logic specification.
