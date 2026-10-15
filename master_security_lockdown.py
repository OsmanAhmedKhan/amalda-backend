import os
import logging
from sqlalchemy import text, create_engine
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InsForge_Master_Security")
load_dotenv()

def secure_all_tables():
    db_url = os.getenv("INSFORGE_DB_URL")
    if not db_url:
        logger.error("INSFORGE_DB_URL not found.")
        return

    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://")

    # AUTOCOMMIT ensures DDL commands run instantly and don't leave idle transactions
    engine = create_engine(db_url, connect_args={"sslmode": "require"}, isolation_level="AUTOCOMMIT")

    with engine.connect() as conn:
        logger.info("Fetching all tables in the public schema...")
        result = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))
        tables = [row[0] for row in result]
        
        for table in tables:
            logger.info(f"Locking down table: {table}")
            try:
                # 1. Enable and Force RLS
                conn.execute(text(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY;'))
                conn.execute(text(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY;'))
                
                # 2. Apply Backend-Only Policy
                conn.execute(text(f'DROP POLICY IF EXISTS "allow_backend_access" ON public."{table}";'))
                conn.execute(text(f'''
                    CREATE POLICY "allow_backend_access" 
                    ON public."{table}" 
                    FOR ALL 
                    USING (current_user != 'anon');
                '''))
            except Exception as e:
                logger.error(f"Failed to secure {table}: {e}")

        logger.info("Sweeping ghost connections generated during table creation...")
        try:
            killed = conn.execute(text("""
                SELECT count(pg_terminate_backend(pid))
                FROM pg_stat_activity
                WHERE pid <> pg_backend_pid()
                  AND datname = current_database()
                  AND state IN ('idle', 'idle in transaction');
            """)).scalar()
            logger.info(f"✅ Terminated {killed} leaked connection(s).")
        except Exception as e:
            logger.error(f"Failed to terminate connections: {e}")

        logger.info("=== Master Security Lockdown Complete ===")

if __name__ == "__main__":
    secure_all_tables()
