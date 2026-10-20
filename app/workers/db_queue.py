import os
import asyncio
import logging
from sqlalchemy import text
from app.db.session import SessionLocal
from app.core.config import settings

# Import InsForge to pull bytes from S3
from app.core.insforge import insforge

# Import the Format-Agnostic Registry
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
    """Durable, multi-worker safe queue processor using Atomic Postgres locks."""
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
                    # 🟢 STATELESS FIX: Download bytes directly from InsForge S3 into RAM
                    try:
                        file_bytes = insforge.get_file_bytes(filename)
                        logger.info(f"Downloaded {len(file_bytes)} bytes from S3 for {filename}")
                    except Exception as s3_err:
                        raise FileNotFoundError(f"Uploaded file not found in InsForge S3: {s3_err}")

                    # Format-Agnostic Registry Extraction (Passes bytes instead of file_path)
                    parsed_doc = registry.extract(file_bytes=file_bytes, filename=filename)
                    
                    # Validate that parsed_doc has actual nodes/content
                    if not parsed_doc or not getattr(parsed_doc, "nodes", None):
                        raise ValueError(f"Document {filename} extracted zero readable text nodes.")
                        
                    app_state.db.index_document(parsed_doc)
                    
                    # Explicitly update status to COMPLETED and clear previous errors
                    db.execute(
                        text("UPDATE document_records SET status = 'COMPLETED', error_message = NULL WHERE id = :id"), 
                        {"id": doc_id}
                    )
                    db.commit()
                    logger.info(f"[Worker] ✅ Successfully finished processing {filename}")
                    
                except Exception as e:
                    logger.error(f"[Worker] ❌ Error processing {filename}: {e}", exc_info=True)
                    clean_err = str(e).replace("'", "")
                    db.execute(
                        text("UPDATE document_records SET status = 'FAILED', error_message = :err WHERE id = :id"), 
                        {"id": doc_id, "err": clean_err}
                    )
                    db.commit()
            else:
                # No tasks found, sleep before polling again
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"[Worker] Queue polling error: {e}")
            await asyncio.sleep(5)
        finally:
            db.close()