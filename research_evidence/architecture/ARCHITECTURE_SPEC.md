# AMALDA Architectural Specification

## 1. System Overview
AMALDA (Adaptive Multi-Agent Legal Document Analysis) is a cloud-resilient, asynchronous Python backend powered by FastAPI, LangGraph, and ChromaDB. It features a secure ingestion pipeline and a reflective multi-agent reasoning core.

## 2. Component Specifications

### 2.1 API Gateway & Security
* **Framework:** FastAPI (ASGI)
* **Authentication:** Global dependency injection (`verify_api_key`) via `X-API-Key` headers.
* **Rate Limiting:** `slowapi` enforces strict memory-based throttling (e.g., 10/min for uploads).
* **Logging:** Async-safe ContextVars inject a unique `X-Request-ID` into every lifecycle event.

### 2.2 Document Ingestion & Extraction (`ExtractorRegistry`)
* **MIME Validation:** Structural inspection of ZIP-based OOXML formats (`docx`, `xlsx`, `pptx`) prevents spoofing.
* **Supported Formats:** PDF, DOCX, XLSX, PPTX, TXT, CSV, JPG, PNG, TIFF, WEBP.
* **OCR Fallback:** PyMuPDF (`fitz`) evaluates text density; delegates to `pytesseract` upon optical detection.

### 2.3 Durable Background Queue
* **Locking Mechanism:** Atomic `UPDATE ... RETURNING` SQL statements prevent race conditions across parallel workers.
* **Crash Recovery:** `sweep_orphaned_tasks()` resets `PROCESSING` tasks to `PENDING` during Uvicorn startup.

### 2.4 Vector Engine
* **Embedding Model:** `sentence-transformers/all-MiniLM-L6-v2` (Local).
* **Store:** ChromaDB (Local Persistence).
* **Chunking Strategy:** Recursive character splitting (1500 tokens, 150 overlap) preserving structural metadata.

### 2.5 LangGraph Multi-Agent Core
* **Topology:** Linear sequence (`Clause` $\rightarrow$ `Risk` $\rightarrow$ `Judge`) followed by a conditional `Verifier`.
* **Reflection Loop:** The Verifier agent detects hallucinations against source context; re-triggers `Judge` up to a recursion limit of 12.
* **Streaming:** Yields Server-Sent Events (SSE) reflecting live LangGraph state transitions.

### 2.6 Persistence Layer
* **Primary SQL:** InsForge PostgreSQL (Cloud) / SQLite (Local Fallback).
* **ORM:** SQLAlchemy.
