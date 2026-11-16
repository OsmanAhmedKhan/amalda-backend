from fastapi import APIRouter, Depends
from app.api.v1.endpoints import documents, analysis
from app.core.security import verify_api_key
from app.core.config import settings

# By passing the dependency here, EVERY route inside v1 is instantly secured!
api_router = APIRouter(dependencies=[Depends(verify_api_key)])

api_router.include_router(documents.router, prefix="/documents", tags=["Documents"])
api_router.include_router(analysis.router, prefix="/analyze", tags=["Analysis"])


@api_router.get("/", include_in_schema=False)
@api_router.head("/", include_in_schema=False)
async def api_v1_root():
    return {
        "system": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "healthy",
        "api_version": "v1",
        "endpoints": {
            "documents": "/api/v1/documents/",
            "analyze": "/api/v1/analyze",
            "health": "/health"
        }
    }