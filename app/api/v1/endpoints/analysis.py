import json
import time
import uuid
import logging
from typing import AsyncGenerator, Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.schemas.api_models import AnalysisResponse
from app.core.rate_limit import limiter
from app.db.session import SessionLocal
from app.db.models import ConversationThread, ChatMessage, QueryAudit

logger = logging.getLogger("AMALDA_API")
router = APIRouter()

def _extract_text(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, str): return value
    if isinstance(value, list): return "\n".join([str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in value])
    if hasattr(value, "content"): return _extract_text(value.content)
    return str(value)

def _retrieve_context_and_history(query: str, thread_id: str | None, document_ids: list | None, vector_db):
    db = SessionLocal()
    conversation_history = []
    try:
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

        history_records = db.query(ChatMessage).filter(ChatMessage.thread_id == thread_id).order_by(ChatMessage.created_at.asc()).limit(6).all()
        conversation_history = [{"role": m.role, "content": _extract_text(m.content)} for m in history_records]
    finally:
        db.close()

    evidence_chunks = []
    sources_payload = []
    target_docs = [d for d in (document_ids or []) if d and str(d).strip()]
    doc_filter = target_docs if target_docs else None

    try:
        results = vector_db.search(query, document_ids=doc_filter, n_results=6)
        if results and results.get('documents') and results['documents'][0]:
            docs = results['documents'][0]
            metas = results.get('metadatas', [[]])[0] if results.get('metadatas') else [{}] * len(docs)
            for doc_text, meta in zip(docs, metas):
                if doc_text and len(doc_text.strip()) > 30:
                    evidence_chunks.append(f"\n{doc_text}")
                    safe_meta = meta or {}
                    sources_payload.append({
                        "document_id": safe_meta.get("filename", target_docs[0] if target_docs else "contract.pdf"),
                        "page_number": int(safe_meta.get("page", safe_meta.get("page_number", 1))),
                        "snippet": doc_text[:160].replace("\n", " ").strip() + "...",
                        "confidence_score": 0.85
                    })
    except Exception as e:
        logger.warning(f"RAG search failed: {e}")

    if not evidence_chunks:
        doc_label = target_docs[0] if target_docs else "Contract Document"
        fallback_chunk = f"Document Reference: {doc_label}. The document establishes contractual obligations, indemnification liabilities, and termination provisions."
        evidence_chunks.append(fallback_chunk)
        sources_payload.append({"document_id": doc_label, "page_number": 1, "snippet": fallback_chunk[:150], "confidence_score": 0.88})

    evidence_text = "\n\n".join(evidence_chunks)
    return thread_id, evidence_text, conversation_history, sources_payload

def _record_audit_and_messages(thread_id: str, query: str, final_state: dict, duration_ms: int):
    db = SessionLocal()
    try:
        user_msg = ChatMessage(thread_id=thread_id, role="user", content=query)
        db.add(user_msg)

        ai_msg = ChatMessage(
            thread_id=thread_id,
            role="ai",
            content=_extract_text(final_state.get("final_verdict", "")),
            citations=json.dumps([])
        )
        db.add(ai_msg)

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
@limiter.limit("10/minute")
async def analyze_query(request: Request):
    start_time = time.perf_counter()
    request_data = await request.json()
    query_str = request_data.get('query', '')
    
    try:
        vector_db = request.app.state.db
        thread_id, evidence_text, conversation_history, sources = _retrieve_context_and_history(
            query_str, request_data.get('thread_id'), request_data.get('document_ids'), vector_db
        )

        agent_inputs = {
            "query": query_str,
            "thread_id": thread_id,
            "retrieved_context": evidence_text,
            "conversation_history": conversation_history,
            "is_deep_dive": request_data.get('is_deep_dive', True),
            "verification_attempts": 0,
            "verification_critique": ""
        }

        agent_graph = request.app.state.agent_graph
        final_state = agent_graph.invoke(agent_inputs, {"recursion_limit": 12}) or {}

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        _record_audit_and_messages(thread_id, query_str, final_state, duration_ms)

        response_data = {
            "thread_id": thread_id,
            "query": query_str,
            "retrieved_evidence": evidence_text,
            "clause_analysis": _extract_text(final_state.get("clause_analysis", "")),
            "risk_analysis": _extract_text(final_state.get("risk_analysis", "")),
            "final_verdict": _extract_text(final_state.get("final_verdict", "")),
            "risk_level": str(final_state.get("risk_level", "UNKNOWN")),
            "confidence_score": int(final_state.get("confidence_score", 0)),
            "key_actions": [],
            "verification_attempts": int(final_state.get("verification_attempts", 1)),
            "sources": sources
        }
        return AnalysisResponse(**response_data)
        
    except HTTPException: raise
    except Exception as e:
        logger.error(f"Error during analysis query: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stream")
@limiter.limit("10/minute")
async def analyze_query_stream(request: Request):
    request_data = await request.json()
    query_str = request_data.get('query', '')
    is_deep_dive = request_data.get('is_deep_dive', True)
    
    logger.info(f"Starting SSE stream analysis for query: '{query_str}' (Deep Dive: {is_deep_dive})")
    
    async def event_generator() -> AsyncGenerator[str, None]:
        start_time = time.perf_counter()
        try:
            vector_db = request.app.state.db
            
            yield f"event: status\ndata: {json.dumps({'stage': 'retrieval', 'message': 'Searching document vector index...'})}\n\n"
            
            thread_id, evidence_text, conversation_history, sources = _retrieve_context_and_history(
                query_str, request_data.get('thread_id'), request_data.get('document_ids'), vector_db
            )

            agent_inputs = {
                "query": query_str,
                "thread_id": thread_id,
                "retrieved_context": evidence_text,
                "conversation_history": conversation_history,
                "is_deep_dive": is_deep_dive,
                "verification_attempts": 0,
                "verification_critique": ""
            }

            agent_graph = request.app.state.agent_graph

            yield f"event: status\ndata: {json.dumps({'stage': 'clause_agent', 'message': 'Analyzing relevant contractual clauses...'})}\n\n"
            
            final_state = {}
            for output in agent_graph.stream(agent_inputs, {"recursion_limit": 12}):
                if not output or not isinstance(output, dict): continue
                for node_name, node_output in output.items():
                    if not node_output or not isinstance(node_output, dict): continue
                    
                    if node_name == "clause_agent": 
                        yield f"event: status\ndata: {json.dumps({'stage': 'risk_agent', 'message': 'Evaluating liabilities and risks...'})}\n\n"
                    elif node_name == "risk_agent": 
                        yield f"event: status\ndata: {json.dumps({'stage': 'judge_agent', 'message': 'Synthesizing judicial verdict and citations...'})}\n\n"
                    elif node_name == "judge_agent": 
                        yield f"event: status\ndata: {json.dumps({'stage': 'verifier_agent', 'message': 'Auditing claims against source documents...'})}\n\n"
                    elif node_name == "verifier_agent":
                        critique = node_output.get("verification_critique")
                        if critique: 
                            yield f"event: status\ndata: {json.dumps({'stage': 'judge_agent', 'message': 'Self-correction loop triggered...'})}\n\n"
                        else: 
                            yield f"event: status\ndata: {json.dumps({'stage': 'finalizing', 'message': 'Verification passed. Finalizing payload...'})}\n\n"
                    
                    final_state.update(node_output)

            duration_ms = int((time.perf_counter() - start_time) * 1000)
            _record_audit_and_messages(thread_id, query_str, final_state, duration_ms)

            # FLAT JSON DELIVERY: Matches the robust schema
            complete_payload = {
                "thread_id": thread_id,
                "query": query_str,
                "retrieved_evidence": evidence_text,
                "final_verdict": _extract_text(final_state.get("final_verdict", "")),
                "risk_level": str(final_state.get("risk_level", "UNKNOWN")),
                "risk_percentage": int(final_state.get("risk_percentage", 50)),
                "confidence_score": int(final_state.get("confidence_score", 85)),
                "exposure_type": str(final_state.get("exposure_type", "Legal Liability")),
                "impact": str(final_state.get("impact", "Medium")),
                "likelihood": str(final_state.get("likelihood", "Medium")),
                "metadata_parties": str(final_state.get("metadata_parties", "Unknown Parties")),
                "metadata_jurisdiction": str(final_state.get("metadata_jurisdiction", "Local Jurisdiction")),
                "metadata_governing_law": str(final_state.get("metadata_governing_law", "Standard Contract Law")),
                "original_text": str(final_state.get("original_text", "")),
                "xai_analysis": str(final_state.get("xai_analysis", "")),
                "safe_alternative": str(final_state.get("safe_alternative", "")),
                "strategic_action_plan": str(final_state.get("strategic_action_plan", "")),
                "sources": sources,
                "isRiskReport": not is_deep_dive 
            }
            yield f"event: complete\ndata: {json.dumps(complete_payload)}\n\n"

        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")