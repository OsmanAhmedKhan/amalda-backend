import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "AMALDA Legal AI API"
    VERSION: str = "1.0.0"
    
    # Render free tier: bind to 0.0.0.0 and dynamic $PORT
    HOST: str = "0.0.0.0"
    PORT: int = int(os.getenv("PORT", "8000"))
    
    # Model Configuration (remote APIs only - no local models to save RAM)
    DEFAULT_MODEL: str = "llama-3.1-8b-instant"
    FALLBACK_MODEL: str = "gemma2-9b-it"
    EMBEDDING_MODEL: str = "models/text-embedding-004"
    
    # Provider Settings (API Keys)
    GROQ_API_KEY: str = ""
    GROQ_API_KEY_2: str = ""
    CLOUDFLARE_API_KEY: str = ""
    CF_ACCOUNT_ID: str = ""
    OPENROUTER_API_KEY: str = ""
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    API_KEY: str = os.getenv("API_KEY", "amalda_secure_api_key_2026")
    
    # No local persistence on Render free tier (ephemeral filesystem).
    # All files go to InsForge S3; vectors use in-memory Chroma.
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "/tmp/uploads")
    CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", "/tmp/chroma_db")
    LOCAL_DB_PATH: str = os.getenv("LOCAL_DB_PATH", "/tmp/amalda.db")
    
    # InsForge Cloud Infrastructure (PostgreSQL + S3)
    INSFORGE_DB_URL: str = ""
    INSFORGE_S3_ENDPOINT: str = ""
    INSFORGE_S3_ACCESS_KEY: str = ""
    INSFORGE_S3_SECRET_KEY: str = ""
    INSFORGE_S3_BUCKET: str = "amalda-legal-docs"
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

# Do NOT create local directories on import - Render filesystem is ephemeral.
# Directories are created lazily only if needed (they won't persist anyway).
