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
    EMBEDDING_MODEL: str = "models/text-embedding-004" # FIXED: Bypasses 512MB RAM limit
    
    # Provider Settings (API Keys)
    GROQ_API_KEY: str = ""
    GROQ_API_KEY_2: str = ""
    CLOUDFLARE_API_KEY: str = ""
    CF_ACCOUNT_ID: str = ""
    OPENROUTER_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    
    # Local Storage Paths (Fallbacks)
    UPLOAD_DIR: str = "./data/uploads"
    CHROMA_PERSIST_DIR: str = "./data/chroma_db"
    LOCAL_DB_PATH: str = "./data/amalda.db"

    # InsForge Infrastructure Settings
    INSFORGE_DB_URL: str = ""
    INSFORGE_S3_ENDPOINT: str = ""
    INSFORGE_S3_ACCESS_KEY: str = ""
    INSFORGE_S3_SECRET_KEY: str = ""
    INSFORGE_S3_BUCKET: str = "amalda-legal-docs"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

# Automatically create local directories if InsForge is not active
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.CHROMA_PERSIST_DIR, exist_ok=True)