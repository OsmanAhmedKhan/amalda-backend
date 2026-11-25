import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "AMALDA Legal AI API"
    VERSION: str = "1.0.0"
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Model Configuration
    DEFAULT_MODEL: str = "llama-3.1-8b-instant"
    FALLBACK_MODEL: str = "gemma2-9b-it"
    EMBEDDING_MODEL: str = "models/text-embedding-004"  # Bypasses 512MB RAM limit

    # Provider Settings (API Keys)
    GROQ_API_KEY: str = ""
    GROQ_API_KEY_2: str = ""
    CLOUDFLARE_API_KEY: str = ""
    CF_ACCOUNT_ID: str = ""
    OPENROUTER_API_KEY: str = ""
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    API_KEY: str = os.getenv("API_KEY", "amalda_secure_api_key_2026")

    # Absolute paths prevent FileNotFoundError on Render
    BASE_DIR: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "data", "uploads"))
    CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", os.path.join(BASE_DIR, "data", "chroma_db"))

    # Supabase Infrastructure Settings
    DATABASE_URL: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_SECRET_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "amalda-legal-docs"

    @property
    def SUPABASE_JWKS_URL(self) -> str:
        return f"{self.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

# Ensure local temp directories exist
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.CHROMA_PERSIST_DIR, exist_ok=True)