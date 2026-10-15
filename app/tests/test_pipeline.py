import os
import sys
import pytest
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.db.models import DocumentRecord, WorkUnit
from app.retrieval.vector_store import VectorDatabase
from app.workers import db_queue

# Create an isolated test engine
engine = create_engine("sqlite:///./data/amalda_ledger_test.db", connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    from app.db.models import Base
    Base.metadata.create_all(bind=engine)

    # Force db_queue to use the isolated test database to prevent production data corruption
    monkeypatch.setattr(db_queue, "SessionLocal", TestingSessionLocal, raising=False)

    yield
    Base.metadata.drop_all(bind=engine)

def test_vector_store_initialization():
    vd = VectorDatabase()
    assert hasattr(vd, "index_document") and hasattr(vd, "search"), "Vector store lacks the expected index_document/search methods."

def test_queue_sweep_recovery_strict_state_transition():
    db = TestingSessionLocal()

    # 1. Setup isolated data
    unique_hash = uuid.uuid4().hex
    doc = DocumentRecord(filename="strict_orphan.pdf", file_hash=unique_hash, status="PROCESSING")
    db.add(doc)
    db.commit()

    try:
        # 2. Execute target function (takes no args, uses monkeypatched SessionLocal internally)
        db_queue.sweep_orphaned_tasks()

        # 3. Assert precise state transitions on DocumentRecord
        db.refresh(doc)
        status_val = doc.status.name if hasattr(doc.status, "name") else doc.status

        assert status_val == "PENDING", f"DocumentRecord was not reset to PENDING. Current status: {status_val}"
    finally:
        db.delete(doc)
        db.commit()
        db.close()
