import os
import sys
import logging
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Supabase_KeepAlive")
load_dotenv()


def ping_database():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("Missing DATABASE_URL environment variable.")
        sys.exit(1)

    try:
        engine = create_engine(db_url, connect_args={"sslmode": "require"})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        logger.info("✅ Successfully pinged Supabase PostgreSQL. Inactivity timer reset.")
    except Exception as e:
        logger.error(f"❌ Failed to ping Supabase database: {e}")
        sys.exit(1)


if __name__ == "__main__":
    ping_database()