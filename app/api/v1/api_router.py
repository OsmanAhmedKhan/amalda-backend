from fastapi import APIRouter, Depends
from app.api.v1.endpoints import documents, analysis
from app.core.security import verify_api_key

# By passing the dependency here, EVERY route inside v1 is instantly secured!
api_router = APIRouter(dependencies=[Depends(verify_api_key)])

api_router.include_router(documents.router, prefix="/documents", tags=["Documents"])
api_router.include_router(analysis.router, prefix="/analyze", tags=["Analysis"])