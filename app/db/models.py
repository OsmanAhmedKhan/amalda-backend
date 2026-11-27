import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Enum, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.session import Base


def utc_now():
    return datetime.now(timezone.utc)


class DocStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class WorkUnitStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED_RETRY = "FAILED_RETRY"
    FAILED_FATAL = "FAILED_FATAL"


# ---------------------------------------------------------
# CORE TABLES (Per-User Multi-Tenant Ownership)
# ---------------------------------------------------------

class DocumentRecord(Base):
    __tablename__ = "document_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=True)  # Supabase auth.users UUID
    filename = Column(String, index=True, nullable=False)
    file_hash = Column(String, unique=True, index=True, nullable=False)
    status = Column(Enum(DocStatus), default=DocStatus.PENDING)
    error_message = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    jobs = relationship("AnalysisJob", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("user_id", "filename", name="uq_document_records_user_filename"),
    )


class ConversationThread(Base):
    __tablename__ = "conversation_threads"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(String, index=True, nullable=True)  # Supabase auth.users UUID
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
# ENTERPRISE QUEUE ARCHITECTURE
# ---------------------------------------------------------

class AnalysisJob(Base):
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
    __tablename__ = "work_units"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    job_id = Column(String, ForeignKey("analysis_jobs.id"), nullable=False, index=True)

    chunk_index = Column(Integer, nullable=False)
    page_start = Column(Integer, nullable=True)
    page_end = Column(Integer, nullable=True)

    payload_text = Column(String, nullable=False)

    status = Column(Enum(WorkUnitStatus), default=WorkUnitStatus.PENDING)
    attempt_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=5)

    last_error = Column(String, nullable=True)
    error_category = Column(String, nullable=True)

    provider_used = Column(String, nullable=True)
    model_used = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    job = relationship("AnalysisJob", back_populates="work_units")


# ---------------------------------------------------------
# LONG-TERM MEMORY (Persistent per-user on Supabase PostgreSQL)
# ---------------------------------------------------------

class UserPreference(Base):
    __tablename__ = "user_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=True)
    preferred_model = Column(String, default="groq")
    temperature = Column(String, default="0.0")
    max_chunks_retrieved = Column(Integer, default=6)
    memory_enabled = Column(String, default="true")
    auto_summarize = Column(String, default="true")
    theme = Column(String, default="light")
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class DocumentMemory(Base):
    __tablename__ = "document_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=True)  # Supabase auth.users UUID
    filename = Column(String, index=True, nullable=False)
    file_hash = Column(String, index=True, nullable=False)
    summary = Column(String, nullable=True)
    key_clauses = Column(String, nullable=True)
    total_pages = Column(Integer, default=0)
    total_chunks = Column(Integer, default=0)
    risk_level = Column(String, nullable=True)
    last_accessed = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_document_memories_user_filename_hash", "user_id", "filename", "file_hash"),
    )