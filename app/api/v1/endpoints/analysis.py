import json
import time
import uuid
import logging
from typing import AsyncGenerator, Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from app.schemas.api_models import QueryRequest, AnalysisResponse
from app.core.rate_limit import limiter
from app.db.session import SessionLocal
from app.db.models import ConversationThread, ChatMessage, QueryAudit

logger = logging.getLogger("AMALDA_API")
router = APIRouter()

def _extract_text(value: Any) -> str:
    """
    Safely normalizes arbitrary LLM output formats into a flat string for Pydantic compliance.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        extracted = []
        for item in value:
            if isinstance(item, dict):
                if "text" in item:
                    extracted.append(str(item["text"]))
                elif "content" in item:
                    extracted.append(str(item["content"]))
                else:
                    extracted.append(str(item))
            elif hasattr(item, "content"):
                extracted.append(str(item.content))
            else:
                extracted.append(str(item))
        return "\n".join(extracted)
    if hasattr(value, "content"):
        return _extract_text(value.content)
    return str(value)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True
)
def resilient_agent_invoke(agent_graph, inputs):
    # Set recursion limit to 12 (allows 1 full pass + ~2 reflexion loops)
    return agent_graph.invoke(inputs, {"recursion_limit": 12})

def _retrieve_context_and_history(query: str, thread_id: str | None, document_ids: list | None, vector_db):
    """Executes with a short-lived DB session to prevent connection starvation."""
    db = SessionLocal()
    try:
        # 1. Resolve or create thread
        if thread_id:
            thread = db.query(ConversationThread).filter(ConversationThread.id == thread_id).first()
            if not thread:
                thread = ConversationThread(id=thread_id, title=query[:80])
                db.add(thread)
                db.commit()
        else:
            thread_id = str(uuid.uuid4())
            thread = ConversationThread(id=thread_id, title=query[:80])
            db.add(thread)
            db.commit()

        # 2. Retrieve conversation history (most recent 6 messages)
        history_records = (
            db.query(ChatMessage)
            .filter(ChatMessage.thread_id == thread_id)
            .order_by(ChatMessage.created_at.asc())
            .limit(6)
            .all()
        )
        conversation_history = [{"role": m.role, "content": _extract_text(m.content)} for m in history_records]
    finally:
        # Close the connection IMMEDIATELY so we don't hold it during vector search or LLM generation
        db.close()

    # 3. Vector DB Search (NOW WITH FILTERS)
    results = vector_db.search(query, document_ids=document_ids, n_results=4)
    if not results or not results.get('documents') or not results['documents'][0]:
        raise HTTPException(status_code=404, detail="No relevant context found in documents.")

    evidence_chunks = []
    sources_payload = [] 
    
    docs = results['documents'][0]
    ids = results['ids'][0]
    
    # Safely extract metadatas and distances (scores) if Chroma provides them
    metadatas = results.get('metadatas', [[]])[0] if results.get('metadatas') else [{}] * len(docs)
    distances = results.get('distances', [[]])[0] if results.get('distances') else [0.9] * len(docs)

    for doc_text, doc_id, meta, dist in zip(docs, ids, metadatas, distances):
        # Build the LLM's context string
        evidence_chunks.append(f"\n{doc_text}")
        
        # Build the exact Next.js JSON object for this chunk
        safe_meta = meta or {}
        # Convert Chroma distance to a confidence percentage (mock logic based on distance)
        confidence = round(1.0 - (dist if dist < 1.0 else 0.5), 2)
        
        sources_payload.append({
            "document_id": safe_meta.get("filename", "unknown.pdf"),
            # Ensure page_number defaults to 1 if ingestion failed to tag it
            "page_number": int(safe_meta.get("page", safe_meta.get("page_number", 1))),
            "snippet": doc_text[:150].replace("\n", " ").strip() + "...",
            "confidence_score": confidence
        })

    evidence_text = "\n\n".join(evidence_chunks)

    # Return sources alongside the text so the endpoint can pass it to the client
    return thread_id, evidence_text, conversation_history, sources_payload

def _record_audit_and_messages(thread_id: str, query: str, final_state: dict, duration_ms: int):
    """Executes with a fresh, short-lived DB session AFTER the LLM completes."""
    db = SessionLocal()
    try:
        # Save User message
        user_msg = ChatMessage(thread_id=thread_id, role="user", content=query)
        db.add(user_msg)

        # Normalize AI verdict and key actions
        key_actions_dumped = [
            action.model_dump() if hasattr(action, "model_dump") else action 
            for action in final_state.get("key_actions", [])
        ]
        
        final_verdict_str = _extract_text(final_state.get("final_verdict", ""))

        ai_msg = ChatMessage(
            thread_id=thread_id,
            role="ai",
            content=final_verdict_str,
            citations=json.dumps(key_actions_dumped)
        )
        db.add(ai_msg)

        # Save Telemetry Audit
        audit = QueryAudit(
            thread_id=thread_id,
            query_text=query,
            execution_time_ms=duration_ms,
            risk_level=str(final_state.get("risk_level", "UNKNOWN")),
            confidence_score=int(final_state.get("confidence_score", 0)),
            total_tokens=0
        )
        db.add(audit)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to save audit records: {e}")
    finally:
        db.close()

@router.post("/", response_model=AnalysisResponse)
@limiter.limit("5/minute")
async def analyze_query(request: Request, request_data: QueryRequest):
    start_time = time.perf_counter()
    logger.info(f"Received analysis query: '{request_data.query}' (Thread: {request_data.thread_id})")

    try:
        vector_db = request.app.state.db
        
        # 1. Retrieval (Opens and closes DB instantly)
        thread_id, evidence_text, conversation_history, sources = _retrieve_context_and_history(
            request_data.query, request_data.thread_id, request_data.document_ids, vector_db
        )

        agent_inputs = {
            "query": request_data.query,
            "thread_id": thread_id,
            "retrieved_context": evidence_text,
            "conversation_history": conversation_history,
            "verification_attempts": 0,
            "verification_critique": ""
        }

        agent_graph = request.app.state.agent_graph
        final_state = resilient_agent_invoke(agent_graph, agent_inputs) or {}

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        
        # 2. Saving Audit (Opens a fresh, quick DB connection)
        _record_audit_and_messages(thread_id, request_data.query, final_state, duration_ms)

        key_actions_dumped = [
            action.model_dump() if hasattr(action, "model_dump") else action 
            for action in final_state.get("key_actions", [])
        ]

        response_data = {
            "thread_id": thread_id,
            "query": request_data.query,
            "retrieved_evidence": evidence_text,
            "clause_analysis": _extract_text(final_state.get("clause_analysis", "")),
            "risk_analysis": _extract_text(final_state.get("risk_analysis", "")),
            "final_verdict": _extract_text(final_state.get("final_verdict", "")),
            "risk_level": str(final_state.get("risk_level", "UNKNOWN")),
            "confidence_score": int(final_state.get("confidence_score", 0)),
            "key_actions": key_actions_dumped,
            "verification_attempts": int(final_state.get("verification_attempts", 1)),
            "sources": sources # Injecting the citations
        }
        
        return AnalysisResponse(**response_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during analysis query: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stream")
@limiter.limit("5/minute")
async def analyze_query_stream(request: Request, request_data: QueryRequest):
    """Server-Sent Events (SSE) streaming endpoint for live UI agent state emission."""
    logger.info(f"Starting SSE stream analysis for query: '{request_data.query}'")
    
    async def event_generator() -> AsyncGenerator[str, None]:
        start_time = time.perf_counter()
        try:
            vector_db = request.app.state.db
            
            yield f"event: status\ndata: {json.dumps({'stage': 'retrieval', 'message': 'Searching document vector index...'})}\n\n"
            
            # 1. State: Retrieval (Opens and closes DB instantly)
            thread_id, evidence_text, conversation_history, sources = _retrieve_context_and_history(
                request_data.query, request_data.thread_id, request_data.document_ids, vector_db
            )

            agent_inputs = {
                "query": request_data.query,
                "thread_id": thread_id,
                "retrieved_context": evidence_text,
                "conversation_history": conversation_history,
                "verification_attempts": 0,
                "verification_critique": ""
            }

            agent_graph = request.app.state.agent_graph

            yield f"event: status\ndata: {json.dumps({'stage': 'clause_agent', 'message': 'Analyzing relevant contractual clauses...'})}\n\n"
            
            final_state = {}
            # 2. State: Streaming LangGraph Node Execution (NO DB CONNECTIONS HELD OPEN)
            for output in agent_graph.stream(agent_inputs, {"recursion_limit": 12}):
                if not output or not isinstance(output, dict):
                    continue
                    
                for node_name, node_output in output.items():
                    if not node_output or not isinstance(node_output, dict):
                        continue

                    if node_name == "clause_agent":
                        yield f"event: status\ndata: {json.dumps({'stage': 'risk_agent', 'message': 'Evaluating liabilities and risks...'})}\n\n"
                    elif node_name == "risk_agent":
                        yield f"event: status\ndata: {json.dumps({'stage': 'judge_agent', 'message': 'Synthesizing judicial verdict and citations...'})}\n\n"
                    elif node_name == "judge_agent":
                        yield f"event: status\ndata: {json.dumps({'stage': 'verifier_agent', 'message': 'Auditing claims against source documents...'})}\n\n"
                    elif node_name == "verifier_agent":
                        critique = node_output.get("verification_critique")
                        attempts = final_state.get("verification_attempts", 1)
                        if critique:
                            yield f"event: status\ndata: {json.dumps({'stage': 'judge_agent', 'message': f'Hallucination detected. Forcing self-correction (Attempt {attempts})...'})}\n\n"
                        else:
                            yield f"event: status\ndata: {json.dumps({'stage': 'finalizing', 'message': 'Verification passed. Finalizing payload...'})}\n\n"
                    
                    final_state.update(node_output)

            duration_ms = int((time.perf_counter() - start_time) * 1000)
            
            # 3. Save Audit (Opens a fresh, quick DB connection at the very end)
            _record_audit_and_messages(thread_id, request_data.query, final_state, duration_ms)

            key_actions_dumped = [
                action.model_dump() if hasattr(action, "model_dump") else action 
                for action in final_state.get("key_actions", [])
            ]

            complete_payload = {
                "thread_id": thread_id,
                "query": request_data.query,
                "retrieved_evidence": evidence_text,
                "clause_analysis": _extract_text(final_state.get("clause_analysis", "")),
                "risk_analysis": _extract_text(final_state.get("risk_analysis", "")),
                "final_verdict": _extract_text(final_state.get("final_verdict", "")),
                "risk_level": str(final_state.get("risk_level", "UNKNOWN")),
                "confidence_score": int(final_state.get("confidence_score", 0)),
                "key_actions": key_actions_dumped,
                "verification_attempts": int(final_state.get("verification_attempts", 1)),
                "sources": sources # Next.js citations payload is finally attached!
            }
            yield f"event: complete\ndata: {json.dumps(complete_payload)}\n\n"

        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")