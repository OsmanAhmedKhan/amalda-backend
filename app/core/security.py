import os
import logging
from fastapi import Security, HTTPException, status
from fastapi.security.api_key import APIKeyHeader

logger = logging.getLogger("AMALDA_API")

# Load exactly from environment, fallback to default
API_KEY = os.getenv("API_KEY", "amalda_secure_api_key_2026")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    # SECURITY LOGGING: See exactly what the client is sending
    if api_key != API_KEY:
        logger.warning(f"🚨 SECURITY ALERT: Unauthorized access attempt with key: '{api_key}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header. Access Denied.",
        )
    return api_key