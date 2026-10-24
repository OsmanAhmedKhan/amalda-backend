import hashlib
import logging
import zipfile
import io
import os
import time
import boto3
import mimetypes
import re
from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Request, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

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

# 🔥 DEPENDENCY OPTIMIZATION: Lightweight secure_filename without Werkzeug
def secure_filename(filename: str) -> str:
    """
    Pass it a filename and it will return a secure version of it.
    This function will replace all non-alphanumeric characters 
    (except for '.', '-', and '_') with underscores and remove 
    leading/trailing whitespace or dots.
    """
    if not filename:
        return ""
    
    # Replace non-alphanumeric (and specific allowed chars) with underscores
    filename = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
    
    # Strip leading/trailing dots and spaces
    filename = filename.strip(' .')
    
    # Ensure it's not empty after cleaning
    if not filename:
        filename = "unnamed_file"
        
    return filename

# 🔥 DEPENDENCY OPTIMIZATION: Use built-in Python mimetypes and zipfile scanning instead of python-magic
def secure_mime_sniff_bytes(file_bytes: bytes, filename: str, fallback_mime: str) -> str:
    """Safely determines MIME type using built-in libraries without C-bindings."""
    mime = fallback_mime
    
    # Use python's built-in mimetype guesser if the frontend didn't supply one
    if not mime or mime == "application/octet-stream":
        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type:
            mime = guessed_type

    # Deep inspect zip archives for modern Office formats
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
            
    return mime or "application/octet-stream"

@router.get("/")
async def list_documents(
    status_filter: Optional[DocStatus] = Query(None, description="Filter by document status"),
    skip: int = Query(0, description="Number of records to skip"), 
    limit: int = Query(100, description="Maximum number of records to return"), 
    db: Session = Depends(get_db)
):
    try:
        # 1. Fetch records from local SQL DB
        query = db.query(DocumentRecord).filter(DocumentRecord.deleted_at == None)
        if status_filter:
            query = query.filter(DocumentRecord.status == status_filter)
        db_records = query.offset(skip).limit(limit).all()

        # 2. Cross-reference with Supabase S3 bucket
        s3_client = boto3.client(
            's3',
            endpoint_url=settings.INSFORGE_S3_ENDPOINT,
            aws_access_key_id=settings.INSFORGE_S3_ACCESS_KEY,
            aws_secret_access_key=settings.INSFORGE_S3_SECRET_KEY,
            region_name=os.getenv("S3_REGION", "ap-southeast-1")
        )
        
        actual_files_in_bucket = set()
        try:
            response = s3_client.list_objects_v2(Bucket=settings.INSFORGE_S3_BUCKET)
            if 'Contents' in response:
                actual_files_in_bucket = {obj['Key'] for obj in response['Contents']}
        except Exception as e:
            logger.error(f"Failed to list objects in S3 for validation: {e}")
            pass

        # 3. Filter the DB records against actual S3 contents (SILENTLY)
        validated_documents = []
        for r in db_records:
            if actual_files_in_bucket and r.filename not in actual_files_in_bucket:
                # Silently skip the ghost file to keep the terminal clean
                continue
                
            validated_documents.append({
                "filename": r.filename, 
                "status": r.status.value if r.status else None, 
                "created_at": r.created_at,
                "error": r.error_message
            })

        return {
            "total_records": len(validated_documents),
            "skip": skip,
            "limit": limit,
            "documents": validated_documents
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
        
        insforge.delete_file(safe_name)
        
        logger.info(f"Successfully soft-deleted document '{safe_name}'.")
        return {"status": "success", "filename": safe_name, "embeddings_purged": purged_count, "message": f"Document '{safe_name}' deleted."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{filename}/download")
async def download_document(filename: str, api_key: str = Depends(verify_api_key)):
    safe_name = secure_filename(filename)
    try:
        s3_client = boto3.client(
            's3',
            endpoint_url=settings.INSFORGE_S3_ENDPOINT,
            aws_access_key_id=settings.INSFORGE_S3_ACCESS_KEY,
            aws_secret_access_key=settings.INSFORGE_S3_SECRET_KEY,
            region_name=os.getenv("S3_REGION", "ap-southeast-1")
        )
        
        response = s3_client.get_object(Bucket=settings.INSFORGE_S3_BUCKET, Key=safe_name)
        file_bytes = response['Body'].read()
        
        return StreamingResponse(
            io.BytesIO(file_bytes), 
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{safe_name}"',
                "Cache-Control": "no-store"
            }
        )
    except Exception as e:
        logger.error(f"Failed to stream document {safe_name}: {e}")
        raise HTTPException(status_code=404, detail=f"File not found in S3: {str(e)}")

@router.post("/upload", response_model=UploadResponse)
@limiter.limit("10/minute")
async def upload_document(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    safe_filename = secure_filename(file.filename)
    
    try:
        file_bytes = await file.read()
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="File too large. Maximum size is 10MB.")
        
        # Use FastAPI's content_type as the primary identifier, fallback to python's built-in mimetypes
        mime_type = secure_mime_sniff_bytes(file_bytes, safe_filename, file.content_type)
        is_text = mime_type.startswith("text/") or mime_type in ["inode/x-empty", "application/csv"]
        
        if mime_type not in ALLOWED_MIME_TYPES and not is_text:
            raise HTTPException(status_code=415, detail=f"Unsupported or unsafe file format: {mime_type}")
        
        raw_hash = hashlib.sha256(file_bytes).hexdigest()
        unique_file_hash = f"{raw_hash}_{int(time.time())}"
        
        insforge.save_file_bytes(safe_filename, file_bytes)
        
        existing_doc = db.query(DocumentRecord).filter(DocumentRecord.filename == safe_filename).first()
        
        if existing_doc:
            if existing_doc.deleted_at is None and existing_doc.status in [DocStatus.COMPLETED, DocStatus.PROCESSING, DocStatus.PENDING]:
                return UploadResponse(filename=safe_filename, message=f"Document already exists (Status: {existing_doc.status.value if existing_doc.status else 'UNKNOWN'}).")
            else:
                existing_doc.deleted_at = None
                existing_doc.status = DocStatus.PENDING
                existing_doc.file_hash = unique_file_hash
                existing_doc.error_message = None
                db.commit()
        else:
            new_doc = DocumentRecord(filename=safe_filename, file_hash=unique_file_hash, status=DocStatus.PENDING)
            db.add(new_doc)
            db.commit()
            
        return UploadResponse(filename=safe_filename, message="Upload complete! Document is now processing.")
    except IntegrityError as e:
        db.rollback()
        logger.error(f"Database Integrity Error during upload: {e}")
        raise HTTPException(status_code=500, detail="Database constraint violation. Please try renaming the file.")
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Upload Crash: {e}")
        raise HTTPException(status_code=500, detail=str(e))