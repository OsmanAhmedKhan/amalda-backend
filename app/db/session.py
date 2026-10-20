import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

# STRICT REQUIREMENT: InsForge Cloud PostgreSQL
INSFORGE_URL = os.getenv("INSFORGE_DB_URL")

if not INSFORGE_URL:
    raise ValueError("CRITICAL ERROR: INSFORGE_DB_URL is missing. Local SQLite fallbacks have been disabled for this stateless architecture.")

# Convert postgresql:// to postgresql+psycopg:// for psycopg3
if INSFORGE_URL.startswith("postgresql://"):
    INSFORGE_URL = INSFORGE_URL.replace("postgresql://", "postgresql+psycopg://")
    
engine = create_engine(
    INSFORGE_URL,
    pool_size=5,             # Max persistent connections to prevent DB exhaustion
    max_overflow=5,          # Temporary overflow limit
    pool_recycle=1800,       # Recycle connections every 30 mins to prevent idle drops
    pool_pre_ping=True,      # Verify connection is alive before routing query
    connect_args={"sslmode": "require"}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency to generate a safe, isolated database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()