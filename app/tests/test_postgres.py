import os
import pytest
from sqlalchemy import text, create_engine
from dotenv import load_dotenv

load_dotenv()

def test_insforge_postgresql_connection():
    db_url = os.getenv("INSFORGE_DB_URL")
    if not db_url:
        pytest.skip("BLOCKED: INSFORGE_DB_URL missing. Cloud database unverified.")

    if db_url.startswith("postgresql://"):
        sync_url = db_url.replace("postgresql://", "postgresql+psycopg2://")
    else:
        sync_url = db_url

    engine = create_engine(sync_url, connect_args={"sslmode": "require"})

    try:
        with engine.connect() as conn:
            # Execute a safe, read-only query that proves authentication and dialect
            result = conn.execute(text("SELECT current_database(), version();")).fetchone()
            assert result is not None
            assert "insforge" in result[0] or "postgres" in result[0].lower()
            assert "PostgreSQL" in result[1]
    except Exception as e:
        pytest.fail(f"InsForge connection/authentication failed: {e}")
