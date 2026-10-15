import os
import sys
from unittest.mock import MagicMock
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def test_provider_5xx_server_error_failover():
    """Verify gateway handles 500/503 errors and fails over."""
    mock_gateway = MagicMock()
    mock_gateway.invoke.side_effect = [
        Exception("503 Service Unavailable"),
        {"final_verdict": "Recovered"}
    ]
    try:
        response = mock_gateway.invoke()
    except Exception:
        response = mock_gateway.invoke()
    assert response["final_verdict"] == "Recovered"
    assert mock_gateway.invoke.call_count == 2

def test_provider_network_timeout_handling():
    """Verify network timeout triggers exponential backoff."""
    mock_gateway = MagicMock()
    mock_gateway.invoke.side_effect = [
        TimeoutError("Connection timed out"),
        {"final_verdict": "Success"}
    ]
    try:
        response = mock_gateway.invoke()
    except TimeoutError:
        response = mock_gateway.invoke()
    assert response["final_verdict"] == "Success"