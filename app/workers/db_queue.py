import asyncio
import logging
from sqlalchemy import text
from app.db.session import SessionLocal
from app.core.supabase_client import supabase_client
from app.parsers.extractors import registry

logger = logging.getLogger("AMALDA_API")


def sweep_orphaned_tasks():
    """Crash Recovery: Resets jobs stuck in PROCESSING back to PENDING on startup."""
    db = SessionLocal()
    try:
        result = db.execute(
            text("UPDATE document_records SET status = 'PENDING' WHERE status = 'PROCESSING'")
        )
        db.commit()
        if result.rowcount > 0:
            logger.warning(
                f"🧹 CRASH RECOVERY: Reset {result.rowcount} orphaned document(s) back to PENDING."
            )
    except Exception as e:
        logger.error(f"Failed to sweep orphans: {e}")
    finally:
        db.close()


def _poll_and_process_once(app_state) -> bool:
    """
    Runs all blocking DB, Storage, PDF parsing, and embedding work inside a worker thread
    so FastAPI's async event loop and /health endpoint are never blocked.
    """
    db = SessionLocal()
    try:
        result = db.execute(
            text("""
                UPDATE document_records
                SET status = 'PROCESSING'
                WHERE id = (
                    SELECT id FROM document_records
                    WHERE status = 'PENDING' AND deleted_at IS NULL
                    LIMIT 1
                )
                RETURNING id, filename, user_id;
            """)
        )
        row = result.fetchone()
        db.commit()

        if not row:
            return False

        doc_id, filename, user_id = row
        logger.info(f"[Worker] 🚀 Claimed document parsing task: {filename} (user={user_id})")

        try:
            try:
                file_bytes = supabase_client.get_file_bytes(filename, user_id=user_id)
                logger.info(
                    f"Downloaded {len(file_bytes)} bytes from Supabase Storage for {filename}"
                )
            except Exception as storage_err:
                raise FileNotFoundError(
                    f"Uploaded file not found in Supabase Storage: {storage_err}"
                )

            parsed_doc = registry.extract(file_bytes=file_bytes, filename=filename)

            if not parsed_doc or not getattr(parsed_doc, "nodes", None):
                raise ValueError(f"Document {filename} extracted zero readable text nodes.")

            app_state.db.index_document(parsed_doc, user_id=user_id)

            db.execute(
                text(
                    "UPDATE document_records SET status = 'COMPLETED', error_message = NULL WHERE id = :id"
                ),
                {"id": doc_id},
            )
            db.commit()
            logger.info(f"[Worker] ✅ Successfully finished processing {filename}")

        except Exception as e:
            logger.error(f"[Worker] ❌ Error processing {filename}: {e}", exc_info=True)
            clean_err = str(e).replace("'", "")
            db.execute(
                text(
                    "UPDATE document_records SET status = 'FAILED', error_message = :err WHERE id = :id"
                ),
                {"id": doc_id, "err": clean_err},
            )
            db.commit()

        return True
    finally:
        db.close()


async def document_worker_loop(app_state):
    """Durable, non-blocking background queue processor."""
    logger.info("⚙️ Background task worker initialized and polling...")
    while True:
        try:
            processed = await asyncio.to_thread(_poll_and_process_once, app_state)
            if not processed:
                await asyncio.sleep(4)
        except Exception as e:
            logger.error(f"[Worker] Queue polling error: {e}")
            await asyncio.sleep(5)