"""
really.ai v2 — services package.

This package contains all business-logic services used by the API layer:

  ai.py          — GPT-4o conversation engine (role-based prompts, profile extraction)
  embeddings.py  — Profile-to-vector encoding via OpenAI text-embedding-3-small
  matching.py    — Atlas Vector Search matching pipeline with role-compatibility rules
  vapi.py        — VAPI intake, consent, and introduction call orchestration
  whatsapp.py    — WhatsApp text messaging and voice call dispatch via Node.js bridge
"""
