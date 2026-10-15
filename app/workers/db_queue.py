import os
import asyncio
import logging
from sqlalchemy import text
from app.db.session import SessionLocal

# Import the new Format-Agnostic Registry
from app.parsers.extractors import registry

logger = logging.getLogger("AMALDA_API")

def sweep_orphaned_tasks():
    """Crash Recovery: Resets jobs stuck in PROCESSING back to PENDING on startup."""
    db = SessionLocal()
    try:
        result = db.execute(text("UPDATE document_records SET status = 'PENDING' WHERE status = 'PROCESSING'"))
        db.commit()
        if result.rowcount > 0:
            logger.warning(f"🧹 CRASH RECOVERY: Reset {result.rowcount} orphaned document(s) back to PENDING.")
    except Exception as e:
        logger.error(f"Failed to sweep orphans: {e}")
    finally:
        db.close()

async def document_worker_loop(app_state):
    """Durable, multi-worker safe queue processor using Atomic SQLite locks."""
    logger.info("⚙️ Background task worker initialized and polling...")
    while True:
        db = SessionLocal()
        try:
            # Atomic Claim: Only one worker can claim a specific row
            result = db.execute(text("""
                UPDATE document_records 
                SET status = 'PROCESSING' 
                WHERE id = (
                    SELECT id FROM document_records 
                    WHERE status = 'PENDING' AND deleted_at IS NULL 
                    LIMIT 1
                )
                RETURNING id, filename;
            """))
            row = result.fetchone()
            db.commit()

            if row:
                doc_id, filename = row
                logger.info(f"[Worker] 🚀 Claimed document parsing task: {filename}")
                
                try:
                    file_path = os.path.join("./data/uploads", filename)
                    
                    # ---------------------------------------------------------
                    # ENTERPRISE UPGRADE: Format-Agnostic Registry Extraction
                    # ---------------------------------------------------------
                    parsed_doc = registry.extract(file_path, filename)
                    app_state.db.index_document(parsed_doc)
                    
                    # Explicitly nullify any previous error message on success
                    db.execute(
                        text("UPDATE document_records SET status = 'COMPLETED', error_message = NULL WHERE id = :id"), 
                        {"id": doc_id}
                    )
                    db.commit()
                    logger.info(f"[Worker] ✅ Successfully finished processing {filename}")
                except Exception as e:
                    logger.error(f"[Worker] ❌ Error processing {filename}: {e}", exc_info=True)
                    db.execute(text("UPDATE document_records SET status = 'FAILED', error_message = :err WHERE id = :id"), {"id": doc_id, "err": str(e)})
                    db.commit()
            else:
                # No tasks found, sleep before polling again
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"[Worker] Queue polling error: {e}")
            await asyncio.sleep(5)
        finally:
            db.close()
