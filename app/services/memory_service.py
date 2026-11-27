"""
AMALDA Legal Per-User LSTM Memory Service
=========================================
Enterprise multi-tenant Long-Term + Short-Term Memory on Supabase PostgreSQL.
Every thread, message, preference, and document summary is strictly scoped by user_id.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.db.models import (
    ConversationThread,
    ChatMessage,
    UserPreference,
    DocumentMemory,
)

logger = logging.getLogger("AMALDA_API")


class ShortTermMemory:
    """In-request conversation context window."""
    def __init__(self, thread_id: str, max_messages: int = 10):
        self.thread_id = thread_id
        self.max_messages = max_messages
        self.messages: List[Dict[str, str]] = []

    def add(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def get_context(self) -> str:
        if not self.messages:
            return ""
        return "\n".join(f"{m['role'].upper()}: {m['content']}" for m in self.messages)


class MemoryService:
    """Persistent per-user memory layer on Supabase PostgreSQL."""

    # -----------------------------------------------------------
    # Conversation Threads (Strictly scoped by user_id)
    # -----------------------------------------------------------
    def get_or_create_thread(
        self,
        db: Session,
        user_id: str,
        thread_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ConversationThread:
        if thread_id:
            thread = (
                db.query(ConversationThread)
                .filter(
                    ConversationThread.id == thread_id,
                    ConversationThread.user_id == user_id,
                )
                .first()
            )
            if thread:
                return thread

        thread = ConversationThread(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=title or "New Legal Conversation",
        )
        db.add(thread)
        db.commit()
        db.refresh(thread)
        return thread

    def list_threads(
        self, db: Session, user_id: str, limit: int = 50
    ) -> List[ConversationThread]:
        return (
            db.query(ConversationThread)
            .filter(ConversationThread.user_id == user_id)
            .order_by(desc(ConversationThread.updated_at))
            .limit(limit)
            .all()
        )

    def update_thread_title(self, db: Session, user_id: str, thread_id: str, title: str):
        thread = (
            db.query(ConversationThread)
            .filter(
                ConversationThread.id == thread_id,
                ConversationThread.user_id == user_id,
            )
            .first()
        )
        if thread:
            thread.title = title
            db.commit()

    # -----------------------------------------------------------
    # Messages (Short-Term Context + Long-Term History)
    # -----------------------------------------------------------
    def add_message(
        self,
        db: Session,
        thread_id: str,
        role: str,
        content: str,
        citations: Optional[str] = None,
    ) -> ChatMessage:
        msg = ChatMessage(
            thread_id=thread_id,
            role=role,
            content=content,
            citations=citations,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg

    def get_recent_messages(
        self, db: Session, thread_id: str, limit: int = 10
    ) -> List[ChatMessage]:
        return (
            db.query(ChatMessage)
            .filter(ChatMessage.thread_id == thread_id)
            .order_by(desc(ChatMessage.created_at))
            .limit(limit)
            .all()
        )

    def get_conversation_context(
        self, db: Session, thread_id: str, max_messages: int = 10
    ) -> str:
        messages = self.get_recent_messages(db, thread_id, limit=max_messages)
        if not messages:
            return ""
        messages = list(reversed(messages))
        return "\n".join(f"{m.role.upper()}: {m.content}" for m in messages)

    # -----------------------------------------------------------
    # User Preferences
    # -----------------------------------------------------------
    def get_or_create_preferences(self, db: Session, user_id: str) -> UserPreference:
        prefs = (
            db.query(UserPreference)
            .filter(UserPreference.user_id == user_id)
            .first()
        )
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
    # Document Memory (Per-User)
    # -----------------------------------------------------------
    def save_document_memory(
        self,
        db: Session,
        user_id: str,
        filename: str,
        file_hash: str,
        summary: Optional[str] = None,
        key_clauses: Optional[List[Dict]] = None,
        total_pages: int = 0,
        total_chunks: int = 0,
        risk_level: Optional[str] = None,
    ):
        mem = (
            db.query(DocumentMemory)
            .filter(
                DocumentMemory.user_id == user_id,
                DocumentMemory.filename == filename,
                DocumentMemory.file_hash == file_hash,
            )
            .first()
        )
        if mem:
            mem.summary = summary
            mem.key_clauses = json.dumps(key_clauses) if key_clauses else None
            mem.total_pages = total_pages
            mem.total_chunks = total_chunks
            mem.risk_level = risk_level
            mem.last_accessed = datetime.now(timezone.utc)
        else:
            mem = DocumentMemory(
                user_id=user_id,
                filename=filename,
                file_hash=file_hash,
                summary=summary,
                key_clauses=json.dumps(key_clauses) if key_clauses else None,
                total_pages=total_pages,
                total_chunks=total_chunks,
                risk_level=risk_level,
            )
            db.add(mem)
        db.commit()
        db.refresh(mem)
        return mem

    def get_document_memory(
        self,
        db: Session,
        user_id: str,
        filename: str,
        file_hash: Optional[str] = None,
    ) -> Optional[DocumentMemory]:
        query = db.query(DocumentMemory).filter(
            DocumentMemory.user_id == user_id,
            DocumentMemory.filename == filename,
        )
        if file_hash:
            query = query.filter(DocumentMemory.file_hash == file_hash)
        return query.first()

    def list_document_memories(
        self, db: Session, user_id: str, limit: int = 50
    ) -> List[DocumentMemory]:
        return (
            db.query(DocumentMemory)
            .filter(DocumentMemory.user_id == user_id)
            .order_by(desc(DocumentMemory.last_accessed))
            .limit(limit)
            .all()
        )

    # -----------------------------------------------------------
    # Cross-Conversation Long-Term Memory Injection (Strictly Per-User)
    # -----------------------------------------------------------
    def get_relevant_past_context(
        self,
        db: Session,
        user_id: str,
        current_thread_id: str,
        query: str,
        limit: int = 3,
    ) -> str:
        """
        Retrieves relevant past conversation context ONLY from this user's other threads.
        """
        past_messages = (
            db.query(ChatMessage)
            .join(ConversationThread, ChatMessage.thread_id == ConversationThread.id)
            .filter(ConversationThread.user_id == user_id)
            .filter(ChatMessage.thread_id != current_thread_id)
            .filter(ChatMessage.role == "user")
            .order_by(desc(ChatMessage.created_at))
            .limit(limit * 4)
            .all()
        )
        if not past_messages:
            return ""

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

        lines = ["RELEVANT PAST CONVERSATIONS (User Long-Term Memory):"]
        for _, content in top:
            lines.append(f"- User previously asked: {content[:200]}...")
        lines.append("---")
        return "\n".join(lines)


memory_service = MemoryService()