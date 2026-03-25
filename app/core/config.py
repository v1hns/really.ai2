"""
Application configuration for really.ai v2.

All settings are read from environment variables (or a .env file).
Removed from v1: TELEGRAM_BOT_TOKEN, DATABASE_URL (SQLite/PostgreSQL).
Added for v2: MONGODB_URI, MONGODB_DB_NAME, WHATSAPP_BRIDGE_URL.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- MongoDB (replaces DATABASE_URL from v1) ---
    MONGODB_URI: str
    MONGODB_DB_NAME: str = "really_ai"

    # --- WhatsApp bridge (Node.js process) ---
    WHATSAPP_BRIDGE_URL: str = "http://localhost:3001"

    # --- OpenAI (GPT-4o chat + text-embedding-3-small) ---
    OPENAI_API_KEY: str

    # --- VAPI phone calls (fallback for non-WA users; optional) ---
    VAPI_API_KEY: str = ""
    VAPI_PHONE_NUMBER_ID: str = ""

    # --- Resend email introductions (optional) ---
    RESEND_API_KEY: str = ""
    EMAIL_FROM_DOMAIN: str = "really.ai"

    # --- Matching ---
    MATCH_SCORE_THRESHOLD: float = 0.6
    MAX_MATCHES_PER_USER: int = 5

    # --- Public webhook base URL (required for VAPI + consent callbacks) ---
    PUBLIC_BASE_URL: str = ""

    # --- App behaviour ---
    BOT_NAME: str = "Really"
    DEBUG: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
