import os
import sys
import uuid
from sqlalchemy import Column, String, Integer
from sqlalchemy.exc import IntegrityError

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.db.session import engine, SessionLocal, Base

class TestDocumentRecord(Base):
    __tablename__ = "test_document_records_final"
    __table_args__ = {'extend_existing': True}
    id = Column(Integer, primary_key=True, index=True)
    status = Column(String, default="PENDING")
    filename = Column(String, nullable=False, unique=True)

Base.metadata.create_all(bind=engine)

def test_real_sqlite_rollback_on_integrity_error():
    db = SessionLocal()
    unique_file = f"duplicate_{uuid.uuid4().hex}.pdf"
    
    # Insert first record successfully
    doc1 = TestDocumentRecord(filename=unique_file)
    db.add(doc1)
    db.commit()

    # Attempt to insert duplicate filename to trigger IntegrityError
    doc2 = TestDocumentRecord(filename=unique_file)
    db.add(doc2)
    try:
        db.commit()
        assert False, "Should have raised IntegrityError"
    except IntegrityError:
        db.rollback()

    # Verify state: Session is clean and exactly 1 record exists
    count = db.query(TestDocumentRecord).filter_by(filename=unique_file).count()
    assert count == 1, "Rollback failed: partial transaction persisted."
    
    # Clean up test record safely without affecting application data
    db.delete(doc1)
    db.commit()
    db.close()
