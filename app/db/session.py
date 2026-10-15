import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

# Detect environment: Remote InsForge Postgres vs Local SQLite
INSFORGE_URL = os.getenv("INSFORGE_DB_URL")

if INSFORGE_URL:
    # ---------------------------------------------------------
    # PRODUCTION: PostgreSQL with Connection Pool Constraints
    # ---------------------------------------------------------
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
else:
    # ---------------------------------------------------------
    # LOCAL DEVELOPMENT: SQLite with WAL Concurrency
    # ---------------------------------------------------------
    os.makedirs("./data", exist_ok=True)
    SQLALCHEMY_DATABASE_URL = "sqlite:///./data/amalda_ledger.db"

    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency to generate a safe, isolated database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()