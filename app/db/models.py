import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Enum, ForeignKey
from sqlalchemy.orm import relationship
from app.db.session import Base

def utc_now():
    return datetime.now(timezone.utc)

class DocStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"  # New state: Survived but some chunks failed permanently

class WorkUnitStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED_RETRY = "FAILED_RETRY" # e.g., 429 Rate Limit (Safe to try again)
    FAILED_FATAL = "FAILED_FATAL" # e.g., 400 Bad Request / Parse Error (Do not retry)

# ---------------------------------------------------------
# EXISTING VALIDATED CORE (Preserved)
# ---------------------------------------------------------

class DocumentRecord(Base):
    __tablename__ = "document_records"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, unique=True, index=True, nullable=False)
    file_hash = Column(String, unique=True, index=True, nullable=False)
    status = Column(Enum(DocStatus), default=DocStatus.PENDING)
    error_message = Column(String, nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    
    # Relationship to the new durable Job tracker
    jobs = relationship("AnalysisJob", back_populates="document", cascade="all, delete-orphan")

class ConversationThread(Base):
    __tablename__ = "conversation_threads"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    title = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    
    messages = relationship("ChatMessage", back_populates="thread", cascade="all, delete-orphan")
    audits = relationship("QueryAudit", back_populates="thread", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(String, ForeignKey("conversation_threads.id"), nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(String, nullable=False)
    citations = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    thread = relationship("ConversationThread", back_populates="messages")

class QueryAudit(Base):
    __tablename__ = "query_audits"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(String, ForeignKey("conversation_threads.id"), nullable=False, index=True)
    query_text = Column(String, nullable=False)
    total_tokens = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    execution_time_ms = Column(Integer, default=0)
    risk_level = Column(String, nullable=True)
    confidence_score = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    thread = relationship("ConversationThread", back_populates="audits")

# ---------------------------------------------------------
# NEW ENTERPRISE QUEUE ARCHITECTURE (Phases 3 & 4)
# ---------------------------------------------------------

class AnalysisJob(Base):
    """
    Tracks the overarching processing lifecycle of a Massive PDF.
    """
    __tablename__ = "analysis_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    document_id = Column(Integer, ForeignKey("document_records.id"), nullable=False, index=True)
    
    total_chunks = Column(Integer, default=0)
    completed_chunks = Column(Integer, default=0)
    failed_chunks = Column(Integer, default=0)
    
    status = Column(Enum(DocStatus), default=DocStatus.PENDING)
    pipeline_version = Column(String, default="v2.0_resumable")
    
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    
    document = relationship("DocumentRecord", back_populates="jobs")
    work_units = relationship("WorkUnit", back_populates="job", cascade="all, delete-orphan")


class WorkUnit(Base):
    """
    The atomic, idempotent unit of work. 
    Represents a specific chunk of text (e.g., Pages 100-110) that needs to be embedded.
    If the worker crashes or the API rate-limits, this unit alone is safely retried.
    """
    __tablename__ = "work_units"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    job_id = Column(String, ForeignKey("analysis_jobs.id"), nullable=False, index=True)
    
    chunk_index = Column(Integer, nullable=False) # e.g., Chunk #45 of 500
    page_start = Column(Integer, nullable=True)
    page_end = Column(Integer, nullable=True)
    
    # Store the actual text payload temporarily until it is successfully embedded
    payload_text = Column(String, nullable=False)
    
    status = Column(Enum(WorkUnitStatus), default=WorkUnitStatus.PENDING)
    attempt_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=5)
    
    last_error = Column(String, nullable=True)
    error_category = Column(String, nullable=True) # e.g., "RATE_LIMIT", "TIMEOUT"
    
    provider_used = Column(String, nullable=True)
    model_used = Column(String, nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    
    job = relationship("AnalysisJob", back_populates="work_units")