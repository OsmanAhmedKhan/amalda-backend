import os
import sys
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from main import app
from app.core.security import API_KEY
from app.core.rate_limit import limiter

@pytest.fixture(autouse=True)
def reset_limiter_state():
    """Ensure limiter is clean and enabled for test suite."""
    limiter.enabled = True
    yield
    limiter.enabled = True

def test_auth_missing_api_key():
    """Requests missing X-API-Key header must be rejected."""
    with TestClient(app) as client:
        response = client.post("/api/v1/analyze/", json={"query": "Test question?"})
        assert response.status_code in [401, 403], f"Expected 401/403, got {response.status_code}"

def test_auth_invalid_api_key():
    """Requests with invalid X-API-Key must be rejected."""
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/analyze/",
            headers={"X-API-Key": "invalid_malformed_key"},
            json={"query": "Test question?"}
        )
        assert response.status_code in [401, 403], f"Expected 401/403, got {response.status_code}"

@patch("app.api.v1.endpoints.analysis.resilient_agent_invoke")
def test_analyze_contract_valid_payload(mock_invoke):
    """Valid payload and API key must return 200 with structured verdict."""
    # Mock the final graph state returned to the API
    mock_invoke.return_value = {"final_verdict": "Mocked Answer", "reasoning": "Mocked Reasoning"}
    
    with TestClient(app) as client:
        mock_db = MagicMock()
        mock_db.search.return_value = {
            "documents": [["Test legal clause context for contract."]],
            "ids": [["doc-test-1"]]
        }
        app.state.db = mock_db

        response = client.post(
            "/api/v1/analyze/",
            headers={"X-API-Key": API_KEY},
            json={"query": "What is the notice period?"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "final_verdict" in data or "response" in data, "Schema must contain answer output"

def test_analyze_malformed_json_payload():
    """Non-conforming JSON bodies must return 422 Unprocessable Entity."""
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/analyze/",
            headers={"X-API-Key": API_KEY},
            json={"unrecognized_field": 12345}
        )
        assert response.status_code == 422

@patch("app.api.v1.endpoints.analysis.resilient_agent_invoke")
def test_rate_limiter_enforcement(mock_invoke):
    """Sending requests beyond limit must trigger HTTP 429 Too Many Requests."""
    mock_invoke.return_value = {"final_verdict": "Mock"}
    
    with TestClient(app) as client:
        mock_db = MagicMock()
        app.state.db = mock_db

        status_codes = []
        for _ in range(10):
            res = client.post(
                "/api/v1/analyze/",
                headers={"X-API-Key": API_KEY},
                json={"query": "Rapid ping for rate limiter check"}
            )
            status_codes.append(res.status_code)

        assert 429 in status_codes, f"Rate limiter did not trigger. Codes received: {status_codes}"