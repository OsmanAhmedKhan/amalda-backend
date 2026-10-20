import os
import uuid
import logging
import asyncio
import contextvars
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.orm import Session

# --- 1. LOGGING & DB SETUP ---
os.makedirs("./logs", exist_ok=True)

# Define an Async-Safe Context Variable for the Request ID
request_id_context_var = contextvars.ContextVar("request_id", default="SYSTEM")

class RequestIDFilter(logging.Filter):
    """Injects the async-safe request ID into every log record."""
    def filter(self, record):
        record.request_id = request_id_context_var.get()
        return True

# Initialize Global Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - [ReqID: %(request_id)s] - %(message)s",
    handlers=[
        logging.FileHandler("./logs/amalda_backend.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# CRITICAL FIX: Attach the filter to the ROOT handlers so third-party logs (httpx, numexpr) don't crash
for handler in logging.root.handlers:
    handler.addFilter(RequestIDFilter())

logger = logging.getLogger("AMALDA_API")

from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.session import engine, Base, get_db
from app.api.v1.api_router import api_router

from app.retrieval.vector_store import VectorDatabase
from app.parsers.pdf_parser import LegalDocumentParser
from app.agents.graph import build_agent_graph
from app.workers.db_queue import document_worker_loop, sweep_orphaned_tasks

async def resilient_worker_launcher(app_state):
    """
    THE FIX: Makes the background worker immortal. 
    If a corrupted PDF causes the worker thread to crash, it instantly reboots 
    without taking down the whole FastAPI application.
    """
    while True:
        try:
            await document_worker_loop(app_state)
        except asyncio.CancelledError:
            logger.info("Worker thread cleanly cancelled.")
            break
        except Exception as e:
            logger.error(f"Worker loop crashed fatally: {e}. Restarting in 5s...")
            await asyncio.sleep(5)

# --- 2. LIFESPAN MANAGEMENT ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🟢 STARTUP: Initializing engines securely...")
    app.state.db = VectorDatabase()
    app.state.parser = LegalDocumentParser()
    app.state.agent_graph = build_agent_graph()
    
    # Run Crash Recovery
    sweep_orphaned_tasks()
    
    # Start the Immortal Worker Task
    worker_task = asyncio.create_task(resilient_worker_launcher(app.state))
    
    logger.info("🟢 STARTUP: All systems operational.")
    
    yield
    
    logger.info("🔴 SHUTDOWN: Safely closing engines...")
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
        
    app.state.db = None
    app.state.parser = None
    app.state.agent_graph = None

# --- 3. APP INITIALIZATION ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Enterprise Backend for Multi-Agent Legal Document Analysis",
    version=settings.VERSION,
    lifespan=lifespan
)

# --- 4. ENTERPRISE MIDDLEWARE ---
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        
        # Safely bind the request ID to the async context
        token = request_id_context_var.set(request_id)
        
        try:
            response: Response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # Clean up context after request finishes to prevent memory leaks
            request_id_context_var.reset(token)

app.add_middleware(RequestIDMiddleware)

allowed_origins = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, 
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- 5. ROUTER REGISTRATION ---
app.include_router(api_router, prefix="/api/v1")

# --- 6. DEEP HEALTH CHECK ---
@app.get("/")
@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    health_status = {
        "system": settings.PROJECT_NAME,
        "version": "v1",
        "status": "healthy",
        "dependencies": {
            "sql_database": "unknown",
            "vector_store": "unknown"
        }
    }
    
    try:
        db.execute(text("SELECT 1"))
        health_status["dependencies"]["sql_database"] = "healthy"
    except Exception as e:
        logger.error(f"Health Check Failed (SQL): {e}")
        health_status["dependencies"]["sql_database"] = "unhealthy"
        health_status["status"] = "degraded"
        
    try:
        vector_db = app.state.db
        vector_db.collection.count()
        health_status["dependencies"]["vector_store"] = "healthy"
    except Exception as e:
        logger.error(f"Health Check Failed (Vector DB): {e}")
        health_status["dependencies"]["vector_store"] = "unhealthy"
        health_status["status"] = "degraded"
        
    if health_status["status"] == "degraded":
        raise HTTPException(status_code=503, detail=health_status)
        
    return health_status

if __name__ == "__main__":
    logger.info(f"🚀 Starting {settings.PROJECT_NAME} API on port {settings.PORT}...")
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT)