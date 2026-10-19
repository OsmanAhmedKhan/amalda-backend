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
    EMBEDDING_MODEL: str = "models/text-embedding-004" # Bypasses 512MB RAM limit
    
    # Provider Settings (API Keys)
    GROQ_API_KEY: str = ""
    GROQ_API_KEY_2: str = ""
    CLOUDFLARE_API_KEY: str = ""
    CF_ACCOUNT_ID: str = ""
    OPENROUTER_API_KEY: str = ""
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    API_KEY: str = os.getenv("API_KEY", "amalda_secure_api_key_2026")
    
    # CRITICAL FIX: Absolute paths prevent FileNotFoundError on Render
    BASE_DIR: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "data", "uploads"))
    CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", os.path.join(BASE_DIR, "data", "chroma_db"))
    LOCAL_DB_PATH: str = os.getenv("LOCAL_DB_PATH", os.path.join(BASE_DIR, "data", "amalda.db"))

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