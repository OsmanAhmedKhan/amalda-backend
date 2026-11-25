import os
import logging
import jwt
from jwt import PyJWKClient
from fastapi import Security, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.security.api_key import APIKeyHeader
from app.core.config import settings

logger = logging.getLogger("AMALDA_API")

# 1. API Key Auth (Internal / Postman testing)
API_KEY = os.getenv("API_KEY", "amalda_secure_api_key_2026")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# 2. Supabase JWT Auth (Next.js Frontend Users)
bearer_scheme = HTTPBearer(auto_error=False)
jwks_client = PyJWKClient(settings.SUPABASE_JWKS_URL)


async def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        logger.warning("🚨 SECURITY ALERT: Unauthorized API key attempt.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header. Access Denied.",
        )
    return api_key


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
) -> dict:
    """
    Verifies the Supabase JWT (ECC P-256) sent by the Next.js frontend.
    Returns {"user_id": "<uuid>", "email": "<email>"}.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization Bearer token.",
        )

    token = credentials.credentials
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload: missing user ID.",
            )
        return {"user_id": user_id, "email": payload.get("email")}
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
        )
    except Exception as e:
        logger.warning(f"🚨 JWT verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or untrusted authentication token.",
        )