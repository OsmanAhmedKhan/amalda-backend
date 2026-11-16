"""
AMALDA Legal Memory Service
===========================
ChatGPT-style long-term + short-term memory built on InsForge PostgreSQL.
Survives Render free-tier spin-downs because everything is in PostgreSQL,
not local disk or local RAM.

Short-term memory: current conversation context window (in-request).
Long-term memory: conversation threads, messages, user preferences, document summaries
(all persisted in InsForge PostgreSQL).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.db.session import SessionLocal
from app.db.models import (
    ConversationThread, ChatMessage, QueryAudit,
    UserPreference, DocumentMemory, DocumentRecord, DocStatus
)

logger = logging.getLogger("AMALDA_API")

# ---------------------------------------------------------------------------
# Short-term (in-request) context window
# ---------------------------------------------------------------------------

class ShortTermMemory:
    """
    Holds the current conversation's context window during a single request.
    Not persisted — rebuilt from long-term memory on each new request.
    """
    def __init__(self, thread_id: str, max_messages: int = 10):
        self.thread_id = thread_id
        self.max_messages = max_messages
        self.messages: List[Dict[str, str]] = []  # {"role": ..., "content": ...}

    def add(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        # Trim to max_messages
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def get_context(self) -> str:
        """Return formatted context for the LLM."""
        if not self.messages:
            return ""
        lines = []
        for m in self.messages:
            lines.append(f"{m['role'].upper()}: {m['content']}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Long-term memory service (PostgreSQL-backed)
# ---------------------------------------------------------------------------

class MemoryService:
    """
    Persistent memory layer on InsForge PostgreSQL.
    Provides ChatGPT-like long-term memory for AMALDA Legal.
    """

    # -----------------------------------------------------------
    # Conversation threads
    # -----------------------------------------------------------
    def get_or_create_thread(self, db: Session, thread_id: Optional[str] = None,
                             title: Optional[str] = None) -> ConversationThread:
        if thread_id:
            thread = db.query(ConversationThread).filter(
                ConversationThread.id == thread_id
            ).first()
            if thread:
                return thread
        # Create new thread
        thread = ConversationThread(
            id=thread_id or str(uuid.uuid4()),
            title=title or "New Legal Conversation"
        )
        db.add(thread)
        db.commit()
        db.refresh(thread)
        return thread

    def list_threads(self, db: Session, limit: int = 50) -> List[ConversationThread]:
        return (
            db.query(ConversationThread)
            .order_by(desc(ConversationThread.updated_at))
            .limit(limit)
            .all()
        )

    def update_thread_title(self, db: Session, thread_id: str, title: str):
        thread = db.query(ConversationThread).filter(
            ConversationThread.id == thread_id
        ).first()
        if thread:
            thread.title = title
            db.commit()

    # -----------------------------------------------------------
    # Messages (short-term context + long-term history)
    # -----------------------------------------------------------
    def add_message(self, db: Session, thread_id: str, role: str,
                    content: str, citations: Optional[str] = None) -> ChatMessage:
        msg = ChatMessage(
            thread_id=thread_id,
            role=role,
            content=content,
            citations=citations
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg

    def get_recent_messages(self, db: Session, thread_id: str,
                            limit: int = 10) -> List[ChatMessage]:
        return (
            db.query(ChatMessage)
            .filter(ChatMessage.thread_id == thread_id)
            .order_by(desc(ChatMessage.created_at))
            .limit(limit)
            .all()
        )

    def get_conversation_context(self, db: Session, thread_id: str,
                                 max_messages: int = 10) -> str:
        """Build a short-term context string from recent messages."""
        messages = self.get_recent_messages(db, thread_id, limit=max_messages)
        if not messages:
            return ""
        # Reverse to chronological order
        messages = list(reversed(messages))
        lines = []
        for m in messages:
            lines.append(f"{m.role.upper()}: {m.content}")
        return "\n".join(lines)

    # -----------------------------------------------------------
    # User preferences
    # -----------------------------------------------------------
    def get_or_create_preferences(self, db: Session, user_id: str) -> UserPreference:
        prefs = db.query(UserPreference).filter(
            UserPreference.user_id == user_id
        ).first()
        if not prefs:
            prefs = UserPreference(user_id=user_id)
            db.add(prefs)
            db.commit()
            db.refresh(prefs)
        return prefs

    def update_preference(self, db: Session, user_id: str, key: str, value: str):
        prefs = self.get_or_create_preferences(db, user_id)
        setattr(prefs, key, value)
        db.commit()

    # -----------------------------------------------------------
    # Document memory (survives Render restarts)
    # -----------------------------------------------------------
    def save_document_memory(self, db: Session, filename: str, file_hash: str,
                             summary: Optional[str] = None,
                             key_clauses: Optional[List[Dict]] = None,
                             total_pages: int = 0,
                             total_chunks: int = 0,
                             risk_level: Optional[str] = None):
        mem = db.query(DocumentMemory).filter(
            DocumentMemory.filename == filename,
            DocumentMemory.file_hash == file_hash
        ).first()
        if mem:
            mem.summary = summary
            mem.key_clauses = json.dumps(key_clauses) if key_clauses else None
            mem.total_pages = total_pages
            mem.total_chunks = total_chunks
            mem.risk_level = risk_level
            mem.last_accessed = datetime.now(timezone.utc)
        else:
            mem = DocumentMemory(
                filename=filename,
                file_hash=file_hash,
                summary=summary,
                key_clauses=json.dumps(key_clauses) if key_clauses else None,
                total_pages=total_pages,
                total_chunks=total_chunks,
                risk_level=risk_level
            )
            db.add(mem)
        db.commit()
        db.refresh(mem)
        return mem

    def get_document_memory(self, db: Session, filename: str,
                            file_hash: Optional[str] = None) -> Optional[DocumentMemory]:
        query = db.query(DocumentMemory).filter(
            DocumentMemory.filename == filename
        )
        if file_hash:
            query = query.filter(DocumentMemory.file_hash == file_hash)
        return query.first()

    def list_document_memories(self, db: Session, limit: int = 50) -> List[DocumentMemory]:
        return (
            db.query(DocumentMemory)
            .order_by(desc(DocumentMemory.last_accessed))
            .limit(limit)
            .all()
        )

    # -----------------------------------------------------------
    # Cross-conversation memory injection
    # -----------------------------------------------------------
    def get_relevant_past_context(self, db: Session, current_thread_id: str,
                                  query: str, limit: int = 3) -> str:
        """
        Retrieve relevant past conversation context to inject as long-term memory.
        Simple keyword matching — no external embedding API needed (saves RAM).
        """
        # Get recent messages from OTHER threads that might be relevant
        past_messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.thread_id != current_thread_id)
            .filter(ChatMessage.role == "user")
            .order_by(desc(ChatMessage.created_at))
            .limit(limit * 3)
            .all()
        )
        if not past_messages:
            return ""

        # Simple keyword overlap scoring
        query_words = set(query.lower().split())
        scored = []
        for msg in past_messages:
            msg_words = set(msg.content.lower().split())
            overlap = len(query_words & msg_words)
            if overlap > 0:
                scored.append((overlap, msg.content))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:limit]
        if not top:
            return ""

        lines = ["RELEVANT PAST CONVERSATIONS (Long-Term Memory):"]
        for _, content in top:
            lines.append(f"- User asked: {content[:200]}...")
        lines.append("---")
        return "\n".join(lines)


# Singleton instance
memory_service = MemoryService()
