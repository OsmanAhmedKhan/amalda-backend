import pytest
from fastapi.testclient import TestClient
from main import app
from app.core.security import API_KEY

# Create a fixture that safely boots up and shuts down the FastAPI lifespan engines
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c

# --- 1. TEST SYSTEM OBSERVABILITY ---
def test_deep_health_check(client):
    """Verify that the load balancer health check returns 200 OK and confirms DB health."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["dependencies"]["sql_database"] == "healthy"
    assert data["dependencies"]["vector_store"] == "healthy"

# --- 2. TEST SECURITY & RATE LIMITING ---
def test_security_rejection_no_key(client):
    """Verify the API blocks unauthorized access when no key is provided."""
    response = client.get("/api/v1/documents/")
    assert response.status_code == 401
    assert "Invalid or missing X-API-Key header" in response.json()["detail"]

def test_security_rejection_bad_key(client):
    """Verify the API blocks unauthorized access when a bad key is provided."""
    response = client.get(
        "/api/v1/documents/", 
        headers={"X-API-Key": "hacker_key_123"}
    )
    assert response.status_code == 401

def test_security_acceptance(client):
    """Verify the API allows access with the correct key."""
    response = client.get(
        "/api/v1/documents/", 
        headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 200
    assert "total_records" in response.json()

# --- 3. TEST API CONTRACTS ---
def test_pagination_contract(client):
    """Verify the pagination parameters (skip, limit) return the correct schema."""
    response = client.get(
        "/api/v1/documents/?skip=0&limit=5", 
        headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["skip"] == 0
    assert data["limit"] == 5
    assert isinstance(data["documents"], list)

def test_missing_document_chunks(client):
    """Verify the chunking endpoint properly handles non-existent documents."""
    response = client.get(
        "/api/v1/documents/DOES_NOT_EXIST.pdf/chunks", 
        headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 404

# --- 4. TEST AI MEMORY & STREAMING ---
def test_analysis_with_thread_and_memory(client):
    """Verify that analysis generates and persists a thread_id."""
    payload = {
        "query": "What are the rules regarding trade secrets?"
    }
    response = client.post(
        "/api/v1/analyze/",
        headers={"X-API-Key": API_KEY},
        json=payload
    )
    assert response.status_code == 200
    data = response.json()
    
    # Assert thread_id was generated and returned
    assert "thread_id" in data
    assert len(data["thread_id"]) > 10
    
    # Assert standard fields exist
    assert "final_verdict" in data
    assert "risk_level" in data

def test_analysis_streaming_endpoint(client):
    """Verify the SSE streaming endpoint responds with text/event-stream."""
    payload = {
        "query": "What is the penalty if employee leaves early?"
    }
    
    # Using client.stream() from httpx to capture the Server-Sent Events
    with client.stream(
        "POST",
        "/api/v1/analyze/stream",
        headers={"X-API-Key": API_KEY},
        json=payload
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        
        # Read the stream lines and verify event markers
        events = list(response.iter_lines())
        assert any("event: status" in line for line in events)
        assert any("event: complete" in line for line in events)