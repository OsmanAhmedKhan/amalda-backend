import logging
from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Import the Enterprise Gateway
from app.core.llm_gateway import llm_gateway

logger = logging.getLogger("AMALDA_API")

# =====================================================================
# 1. STRUCTURED SCHEMAS
# =====================================================================
class ActionItem(BaseModel):
    advice: str = Field(description="Actionable advice for the user.")
    exact_quote: str = Field(description="The exact substring extracted from the context to substantiate this claim.")
    source_id: str = Field(description="The exact tag corresponding to the evidence chunk.")

class JudgeDecision(BaseModel):
    final_verdict: str = Field(description="The comprehensive legal verdict derived strictly from the retrieved context.")
    risk_level: str = Field(description="Must be EXACTLY one of: LOW, MEDIUM, HIGH, CRITICAL")
    confidence_score: int = Field(description="Integer between 0 and 100 representing confidence in context support.")
    key_actions: List[ActionItem] = Field(description="1 to 3 actionable items grounded in exact document quotes.")

class VerificationReport(BaseModel):
    is_faithful: bool = Field(description="True if all claims and quotes logically entail from the context; False otherwise.")
    critique: str = Field(description="If unfaithful, detail the exact unsupported claims or hallucinations. If faithful, return 'PASS'.")

# =====================================================================
# 2. STATE DEFINITION
# =====================================================================
class AgentState(TypedDict):
    query: str
    thread_id: Optional[str]
    retrieved_context: str
    conversation_history: List[Dict[str, str]]
    clause_analysis: str
    risk_analysis: str
    final_verdict: str
    risk_level: str
    confidence_score: int
    key_actions: List[ActionItem]
    verification_attempts: int
    verification_critique: str

# =====================================================================
# 3. ENTERPRISE RETRY LOGIC
# =====================================================================
standard_retry = retry(
    stop=stop_after_attempt(3), # Reduced to 3 so bad schema loops fail upwards faster
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: logger.warning(f"⚠️ Retrying Agent call... Attempt {retry_state.attempt_number}")
)

# =====================================================================
# 4. AGENT NODES (Using Gateway)
# =====================================================================
@standard_retry
def clause_agent(state: AgentState):
    history_context = "\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in state.get("conversation_history", [])[-2:]])
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an expert legal Clause Analyzer.
Extract and explain the legal obligations, terms, and conditions directly relevant to the query.
Base your analysis strictly on the provided Context. Do not invent clauses.

Prior Conversation Context (if any):
{history}

Document Context:
{context}"""),
        ("human", "{query}")
    ])
    
    formatted_prompt = prompt.invoke({
        "history": history_context or "No prior conversation.",
        "context": state["retrieved_context"],
        "query": state["query"]
    })
    
    est_tokens = len(state["retrieved_context"]) // 4 + 500
    response = llm_gateway.invoke_with_failover(formatted_prompt, estimated_tokens=est_tokens)
    
    return {
        "clause_analysis": response.content,
        "verification_attempts": 0,
        "verification_critique": ""
    }

@standard_retry
def risk_agent(state: AgentState):
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an expert Legal Risk Assessor.
Identify liabilities, penalties, or legal exposures based solely on the extracted clauses and context.

Document Context:
{context}"""),
        ("human", "{query}")
    ])
    
    formatted_prompt = prompt.invoke({
        "context": state["retrieved_context"],
        "query": state["query"]
    })
    
    est_tokens = len(state["retrieved_context"]) // 4 + 300
    response = llm_gateway.invoke_with_failover(formatted_prompt, estimated_tokens=est_tokens)
    return {"risk_analysis": response.content}

@standard_retry
def judge_agent(state: AgentState):
    sys_prompt = """You are the Lead Legal Judge. Synthesize the analyses into an authoritative final verdict.

CRITICAL RESEARCH & COMPLIANCE CONSTRAINTS:
1. STRICT ENTAILMENT: Your final_verdict must be derived EXCLUSIVELY from the Context.
2. REFUSAL PROTOCOL: If the Context lacks facts, state: "The provided document does not contain information regarding this." and set risk_level to LOW.
3. COPY-PASTE ONLY: DO NOT summarize or rephrase. Copy the exact text from the Context. 
4. EXACT PROVENANCE: 'exact_quote' must be a verbatim substring from the Context.

Context: {context}
Clause Analysis: {clause_analysis}
Risk Analysis: {risk_analysis}"""

    if state.get("verification_critique"):
        sys_prompt += f"\n\nCRITICAL AUDIT CRITIQUE (Your previous answer failed verification. Resolve these issues immediately):\n{state['verification_critique']}"

    prompt = ChatPromptTemplate.from_messages([
        ("system", sys_prompt),
        ("human", "{query}")
    ])
    
    formatted_prompt = prompt.invoke({
        "clause_analysis": state["clause_analysis"],
        "risk_analysis": state["risk_analysis"],
        "context": state["retrieved_context"],
        "query": state["query"]
    })
    
    est_tokens = len(state["retrieved_context"]) // 4 + 800
    decision: JudgeDecision = llm_gateway.invoke_with_failover(formatted_prompt, schema=JudgeDecision, estimated_tokens=est_tokens)
    
    return {
        "final_verdict": decision.final_verdict,
        "risk_level": decision.risk_level,
        "confidence_score": decision.confidence_score,
        "key_actions": decision.key_actions,
        "verification_attempts": state.get("verification_attempts", 0) + 1
    }

@standard_retry
def verifier_agent(state: AgentState):
    sys_prompt = """You are a strict Academic Natural Language Inference (NLI) Auditor.
Audit the Judge's verdict and key actions against the raw document context.

AUDIT RULES:
1. Extract distinct factual and legal claims from the verdict.
2. If ANY claim relies on external knowledge, mark is_faithful=False.
3. For 'exact_quote', evaluate semantic presence: ignore minor OCR noise or linebreaks. 
4. If 100% faithful, set is_faithful=True and critique='PASS'. Otherwise, set is_faithful=False and specify the unsupported claim."""

    human_prompt = """Judge Verdict: {verdict}
Judge Actions: {actions}
Context: {context}"""

    prompt = ChatPromptTemplate.from_messages([
        ("system", sys_prompt),
        ("human", human_prompt)
    ])
    
    formatted_prompt = prompt.invoke({
        "context": state["retrieved_context"],
        "verdict": state["final_verdict"],
        "actions": str(state["key_actions"])
    })
    
    est_tokens = len(state["retrieved_context"]) // 4 + 500
    report: VerificationReport = llm_gateway.invoke_with_failover(formatted_prompt, schema=VerificationReport, estimated_tokens=est_tokens)
    
    current_attempt = state.get("verification_attempts", 1)
    logger.info(f"[Verifier] Attempt {current_attempt}: Faithful={report.is_faithful}")
    return {"verification_critique": report.critique if not report.is_faithful else ""}

# =====================================================================
# 5. TRUST GATE NODE (Phase 5)
# =====================================================================
def trust_gate_node(state: AgentState):
    """
    Final deterministic gate. If the verifier critique is NOT empty after the loop finishes,
    it means the LLM failed to produce a faithful answer within the allowed retries.
    We forcefully overwrite the hallucination with an abstention.
    """
    if state.get("verification_critique"):
        logger.error("[Trust Gate] Verification failed max retries. Overriding with Abstention.")
        return {
            "final_verdict": "Insufficient evidence found in the provided document to answer this query securely.",
            "risk_level": "LOW",
            "confidence_score": 0,
            "key_actions": [],
            "verification_critique": "ABSTAINED_BY_TRUST_GATE"
        }
    
    logger.info("[Trust Gate] Verification passed. Claim is evidence-grounded.")
    return {} # State is already faithful, no override needed.

# =====================================================================
# 6. CONDITIONAL ROUTING (Cyclic Self-Correction)
# =====================================================================
def should_rejudge(state: AgentState):
    if not state.get("verification_critique"):
        return "pass"
    if state.get("verification_attempts", 0) >= 3:
        logger.warning("Max verification attempts reached. Routing to Trust Gate for abstention.")
        return "trust_gate" # Route to the Trust Gate instead of END
    return "fail_rewrite"

# =====================================================================
# 7. GRAPH CONSTRUCTION
# =====================================================================
def build_agent_graph():
    workflow = StateGraph(AgentState)
    workflow.add_node("clause_agent", clause_agent)
    workflow.add_node("risk_agent", risk_agent)
    workflow.add_node("judge_agent", judge_agent)
    workflow.add_node("verifier_agent", verifier_agent)
    workflow.add_node("trust_gate", trust_gate_node) # Add the new node
    
    workflow.set_entry_point("clause_agent")
    workflow.add_edge("clause_agent", "risk_agent")
    workflow.add_edge("risk_agent", "judge_agent")
    workflow.add_edge("judge_agent", "verifier_agent")
    
    workflow.add_conditional_edges(
        "verifier_agent",
        should_rejudge,
        {
            "fail_rewrite": "judge_agent",
            "trust_gate": "trust_gate",
            "pass": "trust_gate" # All paths funnel through the Trust Gate before ending
        }
    )
    
    workflow.add_edge("trust_gate", END) # The graph always securely terminates here
    
    return workflow.compile()