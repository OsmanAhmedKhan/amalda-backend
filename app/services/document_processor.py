import hashlib
import logging
import time
import threading
from typing import List, Optional
import pymupdf  # Replaced deprecated fitz
from sqlalchemy.orm import Session
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.db.models import DocumentRecord, AnalysisJob, WorkUnit, DocStatus, WorkUnitStatus

logger = logging.getLogger("AMALDA_API")

class DocumentIngestionPipeline:
    """
    Phase 4: Massive PDF Ingestion.
    Splits documents into persistent WorkUnits for safe, resumable background processing.
    """
    def __init__(self, db: Session):
        self.db = db
        # Enterprise-grade semantic chunking parameters
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1500,
            chunk_overlap=150,
            separators=["\n\n", "\n", ".", " ", ""]
        )

    def _generate_file_hash(self, file_bytes: bytes) -> str:
        """Cryptographic SHA-256 for strict idempotency."""
        return hashlib.sha256(file_bytes).hexdigest()

    def submit_document(self, file_bytes: bytes, filename: str) -> str:
        """
        Phase 1 of processing: Fast ingestion.
        Returns the Job ID immediately. Does NOT block for embeddings.
        """
        file_hash = self._generate_file_hash(file_bytes)
        
        # 1. Idempotency Check
        existing_doc = self.db.query(DocumentRecord).filter(DocumentRecord.file_hash == file_hash).first()
        if existing_doc:
            logger.info(f"[Pipeline] Document {filename} (Hash: {file_hash[:8]}) already exists. Idempotency triggered.")
            # If a job exists and is incomplete, we return it to resume
            existing_job = self.db.query(AnalysisJob).filter(AnalysisJob.document_id == existing_doc.id).first()
            if existing_job:
                return existing_job.id

        # 2. Persist Document Record
        new_doc = DocumentRecord(filename=filename, file_hash=file_hash, status=DocStatus.PROCESSING)
        self.db.add(new_doc)
        self.db.flush() # Flush to get ID

        # 3. Create Overarching Analysis Job
        job = AnalysisJob(document_id=new_doc.id, status=DocStatus.PROCESSING)
        self.db.add(job)
        self.db.flush()

        # 4. Extract and Chunk (Memory Bounded)
        logger.info(f"[Pipeline] Extracting text for {filename}...")
        try:
            pdf_document = pymupdf.open(stream=file_bytes, filetype="pdf")
            total_pages = pdf_document.page_count
            
            work_units = []
            chunk_counter = 0
            
            # Page-by-page extraction prevents RAM exhaustion on 5,000+ page PDFs
            for page_num in range(total_pages):
                page = pdf_document.load_page(page_num)
                page_text = page.get_text("text")
                
                if not page_text.strip():
                    continue

                chunks = self.text_splitter.split_text(page_text)
                
                for chunk in chunks:
                    wu = WorkUnit(
                        job_id=job.id,
                        chunk_index=chunk_counter,
                        page_start=page_num + 1,
                        page_end=page_num + 1,
                        payload_text=chunk,
                        status=WorkUnitStatus.PENDING
                    )
                    work_units.append(wu)
                    chunk_counter += 1

            pdf_document.close()

            # 5. Bulk Persist WorkUnits
            self.db.bulk_save_objects(work_units)
            
            job.total_chunks = chunk_counter
            self.db.commit()
            logger.info(f"[Pipeline] Job {job.id} created with {chunk_counter} persistent WorkUnits.")
            
            return job.id

        except Exception as e:
            self.db.rollback()
            logger.error(f"[Pipeline Fatal] Failed to ingest {filename}: {e}")
            raise e

class QueueWorker:
    """
    Phase 4: Resumable Asynchronous Worker.
    Pulls WorkUnits from the DB, embeds them securely, and updates states.
    Survives restarts and API rate limits.
    """
    def __init__(self, db_session_maker, vector_store):
        self.SessionLocal = db_session_maker
        self.vector_store = vector_store  # Your existing ChromaDB instance
        self.is_running = False
    
    def start(self):
        if not self.is_running:
            self.is_running = True
            threading.Thread(target=self._worker_loop, daemon=True).start()
            logger.info("[Worker] Persistent Queue Worker started.")

    def stop(self):
        self.is_running = False

    def _worker_loop(self):
        while self.is_running:
            try:
                self._process_next_unit()
            except Exception as e:
                logger.error(f"[Worker Loop Error] {e}")
                time.sleep(5.0) # Prevent tight crash loop
            
            time.sleep(0.5) # Prevent database hammering when idle

    def _process_next_unit(self):
        with self.SessionLocal() as db:
            # Atomic lock on the next pending work unit
            # SQLite doesn't natively support SKIP LOCKED, so we use a simple fetch-and-update
            unit = db.query(WorkUnit).filter(
                WorkUnit.status.in_([WorkUnitStatus.PENDING, WorkUnitStatus.FAILED_RETRY])
            ).order_by(WorkUnit.created_at.asc()).first()

            if not unit:
                return # Queue is empty

            # Lock it
            unit.status = WorkUnitStatus.PROCESSING
            unit.attempt_count += 1
            db.commit()

            try:
                # ---------------------------------------------------
                # VALIDATED COMPONENT PRESERVATION: Embed & Store
                # We use your existing vector store logic here.
                # ---------------------------------------------------
                metadata = {
                    "source": unit.job.document.filename,
                    "page": unit.page_start,
                    "chunk": unit.chunk_index,
                    "node_id": f"{unit.job.document.file_hash}_chunk_{unit.chunk_index}"
                }
                
                # Assume vector_store has an add_texts method (Standard LangChain Chroma)
                self.vector_store.add_texts(texts=[unit.payload_text], metadatas=[metadata])
                
                # Success
                unit.status = WorkUnitStatus.COMPLETED
                unit.error_category = None
                unit.last_error = None
                
                # Update Job Progress
                job = unit.job
                job.completed_chunks += 1
                
                if job.completed_chunks + job.failed_chunks == job.total_chunks:
                    job.status = DocStatus.PARTIAL if job.failed_chunks > 0 else DocStatus.COMPLETED
                    job.document.status = job.status
                    logger.info(f"[Worker] Job {job.id} Finished. Completed: {job.completed_chunks}/{job.total_chunks}")
                
                db.commit()

            except Exception as e:
                db.rollback()
                error_str = str(e).lower()
                
                with self.SessionLocal() as db_fail:
                    fail_unit = db_fail.query(WorkUnit).filter(WorkUnit.id == unit.id).first()
                    fail_unit.last_error = str(e)
                    
                    # Error Classification
                    if "429" in error_str or "rate limit" in error_str or "timeout" in error_str:
                        fail_unit.error_category = "RATE_LIMIT"
                        if fail_unit.attempt_count < fail_unit.max_retries:
                            fail_unit.status = WorkUnitStatus.FAILED_RETRY
                            logger.warning(f"[Worker] WorkUnit {fail_unit.id} rate-limited. Queued for retry.")
                        else:
                            fail_unit.status = WorkUnitStatus.FAILED_FATAL
                            fail_unit.job.failed_chunks += 1
                    else:
                        fail_unit.error_category = "FATAL_PARSE"
                        fail_unit.status = WorkUnitStatus.FAILED_FATAL
                        fail_unit.job.failed_chunks += 1
                        logger.error(f"[Worker] WorkUnit {fail_unit.id} encountered fatal error: {e}")
                    
                    db_fail.commit()