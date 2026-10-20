import hashlib
import logging
import magic
import zipfile
import io
from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Request, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from werkzeug.utils import secure_filename

from app.core.config import settings
from app.core.security import verify_api_key
from app.schemas.api_models import UploadResponse, ErrorSchema
from app.db.session import get_db
from app.db.models import DocumentRecord, DocStatus, utc_now
from app.core.rate_limit import limiter
from app.core.insforge import insforge

logger = logging.getLogger("AMALDA_API")
router = APIRouter()

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB Limit

ALLOWED_MIME_TYPES = [
    "application/pdf", 
    "text/plain", "inode/x-empty", "application/csv",
    "image/jpeg", "image/png", "image/tiff", "image/webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
]

def secure_mime_sniff_bytes(file_bytes: bytes) -> str:
    """Safely determines MIME type directly from a memory buffer."""
    mime = magic.from_buffer(file_bytes[:2048], mime=True)
    if mime in ['application/zip', 'application/octet-stream', 'application/x-zip-compressed']:
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes), 'r') as zf:
                namelist = zf.namelist()
                if 'word/document.xml' in namelist:
                    return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                elif 'xl/workbook.xml' in namelist:
                    return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                elif 'ppt/presentation.xml' in namelist:
                    return 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        except zipfile.BadZipFile:
            pass
    return mime

@router.get("/")
async def list_documents(
    status_filter: Optional[DocStatus] = Query(None, description="Filter by document status"),
    skip: int = Query(0, description="Number of records to skip"), 
    limit: int = Query(100, description="Maximum number of records to return"), 
    db: Session = Depends(get_db)
):
    try:
        query = db.query(DocumentRecord).filter(DocumentRecord.deleted_at == None)
        if status_filter:
            query = query.filter(DocumentRecord.status == status_filter)
        total = query.count()
        records = query.offset(skip).limit(limit).all()
        return {
            "total_records": total,
            "skip": skip,
            "limit": limit,
            "documents": [
                {
                    "filename": r.filename, 
                    "status": r.status.value if r.status else None, 
                    "created_at": r.created_at,
                    "error": r.error_message
                } for r in records
            ]
        }
    except Exception as e:
        logger.error(f"Failed to list documents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{filename}/status")
async def get_document_status(filename: str, db: Session = Depends(get_db)):
    safe_name = secure_filename(filename)
    record = db.query(DocumentRecord).filter(DocumentRecord.filename == safe_name, DocumentRecord.deleted_at == None).first()
    if not record:
        raise HTTPException(status_code=404, detail="Document not found.")
        
    job_status = insforge.get_job_status(safe_name)
    return {
        "filename": record.filename, 
        "status": record.status.value if record.status else None, 
        "error_message": record.error_message,
        "total_pages": job_status["total_pages"] if job_status else 1,
        "processed_pages": job_status["processed_pages"] if job_status else 0,
        "is_ready": record.status == DocStatus.COMPLETED
    }

@router.get("/{filename}/chunks")
async def get_document_chunks(filename: str, request: Request, db: Session = Depends(get_db)):
    safe_name = secure_filename(filename)
    record = db.query(DocumentRecord).filter(DocumentRecord.filename == safe_name, DocumentRecord.deleted_at == None).first()
    if not record:
        raise HTTPException(status_code=404, detail="Document not found or deleted.")
    try:
        vector_db = request.app.state.db
        results = vector_db.collection.get(where={"filename": safe_name}, include=["metadatas", "documents"])
        if not results or not results["ids"]:
            raise HTTPException(status_code=404, detail="No chunks found for this document in ChromaDB.")
        chunks = [{"node_id": cid, "text": txt, "metadata": meta} for cid, txt, meta in zip(results["ids"], results["documents"], results["metadatas"])]
        return {"filename": safe_name, "total_chunks": len(chunks), "chunks": chunks}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{filename}")
async def delete_document(filename: str, request: Request, db: Session = Depends(get_db)):
    safe_name = secure_filename(filename)
    try:
        record = db.query(DocumentRecord).filter(DocumentRecord.filename == safe_name).first()
        if not record:
            raise HTTPException(status_code=404, detail="Document not found.")
        
        record.deleted_at = utc_now()
        db.commit()
        
        vector_db = request.app.state.db
        purged_count = vector_db.delete_document_embeddings(safe_name)
        
        # Remove from S3
        insforge.delete_file(safe_name)
        
        logger.info(f"🗑️ Successfully soft-deleted document '{safe_name}'.")
        return {"status": "success", "filename": safe_name, "embeddings_purged": purged_count, "message": f"Document '{safe_name}' deleted."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{filename}/download")
async def download_document(filename: str, api_key: str = Depends(verify_api_key)):
    """
    Securely redirects to a temporary S3 presigned URL.
    Offloads massive PDF streaming bandwidth from FastAPI to S3.
    """
    safe_name = secure_filename(filename)
    try:
        presigned_url = insforge.generate_presigned_url(safe_name, expires_in=3600)
        return RedirectResponse(url=presigned_url)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to generate download link: {str(e)}")

@router.post("/upload", response_model=UploadResponse)
@limiter.limit("10/minute")
async def upload_document(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    safe_filename = secure_filename(file.filename)
    
    try:
        # Load file safely into memory
        file_bytes = await file.read()
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="File too large. Maximum size is 10MB.")
        
        # Sniff MIME directly from memory bytes
        mime_type = secure_mime_sniff_bytes(file_bytes)
        is_text = mime_type.startswith("text/") or mime_type in ["inode/x-empty", "application/csv"]
        
        if mime_type not in ALLOWED_MIME_TYPES and not is_text:
            raise HTTPException(status_code=415, detail=f"Unsupported or unsafe file format: {mime_type}")
        
        # Stream bytes straight to InsForge S3
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        insforge.save_file_bytes(safe_filename, file_bytes)
        
        existing_doc = db.query(DocumentRecord).filter((DocumentRecord.file_hash == file_hash) | (DocumentRecord.filename == safe_filename)).first()
        
        if existing_doc:
            if existing_doc.deleted_at is None:
                return UploadResponse(filename=safe_filename, message=f"Document already exists (Status: {existing_doc.status.value if existing_doc.status else 'UNKNOWN'}).")
            else:
                existing_doc.deleted_at = None
                existing_doc.status = DocStatus.PENDING
                existing_doc.file_hash = file_hash
                db.commit()
        else:
            new_doc = DocumentRecord(filename=safe_filename, file_hash=file_hash, status=DocStatus.PENDING)
            db.add(new_doc)
            db.commit()
            
        return UploadResponse(filename=safe_filename, message="Upload complete! Poll the /status endpoint until is_ready=true.")
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))