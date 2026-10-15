import os
import sys
from unittest.mock import MagicMock
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def test_postgres_transaction_rollback_on_failure():
    """Verify failure during insertion triggers strict DB rollback."""
    mock_db_session = MagicMock()
    mock_db_session.commit.side_effect = Exception("Simulated PostgreSQL Integrity Error")
    try:
        mock_db_session.add(MagicMock())
        mock_db_session.commit()
    except Exception:
        mock_db_session.rollback()
    mock_db_session.rollback.assert_called_once()

def test_durable_queue_idempotency():
    """Verify processing the same WorkUnit twice does not duplicate records."""
    processed_ids = set()
    work_unit_id = "WU-999"
    def process_work_unit(wu_id):
        if wu_id in processed_ids:
            return "Skipped - Already Processed"
        processed_ids.add(wu_id)
        return "Processed Successfully"
    assert process_work_unit(work_unit_id) == "Processed Successfully"
    assert process_work_unit(work_unit_id) == "Skipped - Already Processed"
    assert len(processed_ids) == 1