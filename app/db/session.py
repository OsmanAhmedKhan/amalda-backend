import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("AMALDA_API")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: DATABASE_URL is missing from environment variables.")

# Ensure SQLAlchemy uses psycopg (v3) driver
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_size=5,             # Sized safely for Supabase Free (Nano) connection limits
    max_overflow=5,
    pool_recycle=300,        # Recycle idle connections every 5 minutes
    pool_timeout=30,
    pool_pre_ping=True,      # Automatically tests & reconnects dropped SSL connections
    connect_args={
        "sslmode": "require",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5
    }
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency to generate a safe, isolated database session per request."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass  # Swallow rollback errors if connection is already dead
        raise
    finally:
        try:
            db.close()
        except Exception as e:
            logger.warning(f"Ignored DB close error (Connection likely dropped by Supabase): {e}")