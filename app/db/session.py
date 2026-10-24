import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("AMALDA_API")

INSFORGE_URL = os.getenv("INSFORGE_DB_URL")

if not INSFORGE_URL:
    raise ValueError("CRITICAL ERROR: INSFORGE_DB_URL is missing.")

if INSFORGE_URL.startswith("postgresql://"):
    INSFORGE_URL = INSFORGE_URL.replace("postgresql://", "postgresql+psycopg://")
    
engine = create_engine(
    INSFORGE_URL,
    pool_size=10,
    max_overflow=20,
    pool_recycle=60,         # Recycle connections every 60 seconds
    pool_timeout=30,
    pool_pre_ping=True,      # Verify connection before routing query
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
            pass # Swallow rollback errors if connection is already dead
        raise
    finally:
        try:
            db.close()
        except Exception as e:
            # Prevent dead sockets from crashing the ASGI server on cleanup
            logger.warning(f"Ignored DB close error (Connection likely dropped by Supabase): {e}")