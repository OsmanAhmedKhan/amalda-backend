import logging
from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.llm_gateway import llm_gateway

logger = logging.getLogger("AMALDA_API")

class JudgeDecision(BaseModel):
    final_verdict: str = Field(description="The comprehensive legal verdict derived from the context.")
    risk_level: str = Field(description="Must be EXACTLY one of: LOW, MEDIUM, HIGH, CRITICAL")
    risk_percentage: int = Field(description="Integer between 0 and 100 representing the severity of the risk.")
    confidence_score: int = Field(description="Integer between 0 and 100 representing confidence.")
    exposure_type: str = Field(description="Must be exactly: Financial, Termination, Compliance, or General Liability")
    impact: str = Field(description="Must be exactly: High, Medium, or Low")
    likelihood: str = Field(description="Must be exactly: High, Medium, or Low")
    metadata_parties: str = Field(description="The parties involved in the contract. (Use 'Unknown' if not found)")
    metadata_jurisdiction: str = Field(description="The legal jurisdiction. (Use 'Unknown' if not found)")
    metadata_governing_law: str = Field(description="The governing law. (Use 'Unknown' if not found)")
    original_text: str = Field(description="The exact risky clause from the document.")
    xai_analysis: str = Field(description="Explainable AI reasoning on WHY this is a risk. MUST BE PLAIN TEXT PARAGRAPHS. NO MARKDOWN TABLES.")
    safe_alternative: str = Field(description="A legally sound revision of the clause.")
    strategic_action_plan: str = Field(description="Step-by-step negotiation strategy.")

class VerificationReport(BaseModel):
    is_faithful: bool = Field(description="True if claims generally align with the legal context; False only for massive hallucinations.")
    critique: str = Field(description="If unfaithful, detail the error. If faithful, return 'PASS'.")

class AgentState(TypedDict):
    query: str
    thread_id: Optional[str]
    retrieved_context: str
    conversation_history: List[Dict[str, str]]
    clause_analysis: str
    risk_analysis: str
    is_deep_dive: bool
    final_verdict: str
    risk_level: str
    risk_percentage: int
    confidence_score: int
    exposure_type: str
    impact: str
    likelihood: str
    metadata_parties: str
    metadata_jurisdiction: str
    metadata_governing_law: str
    original_text: str
    xai_analysis: str
    safe_alternative: str
    strategic_action_plan: str
    verification_attempts: int
    verification_critique: str

standard_retry = retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: logger.warning(f"⚠️ Retrying Agent call... Attempt {retry_state.attempt_number}")
)

@standard_retry
def clause_agent(state: AgentState):
    # Skip deep analysis if it's just a conversation/summary
    if state.get("is_deep_dive"):
        return {
            "clause_analysis": "Bypassed for conversational query.",
            "verification_attempts": 0,
            "verification_critique": ""
        }

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an expert legal Clause Analyzer.
Extract and explain the legal obligations directly relevant to the query.
Base your analysis strictly on the provided Document Context. Do not invent clauses.

Document Context:
{context}"""),
        ("human", "{query}")
    ])
    
    formatted_prompt = prompt.invoke({
        "context": state["retrieved_context"],
        "query": state["query"]
    })
    
    response = llm_gateway.invoke_with_failover(formatted_prompt, estimated_tokens=1500)
    
    return {
        "clause_analysis": response.content,
        "verification_attempts": 0,
        "verification_critique": ""
    }

@standard_retry
def risk_agent(state: AgentState):
    if state.get("is_deep_dive"):
        return {"risk_analysis": "Bypassed for conversational query."}

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
    
    response = llm_gateway.invoke_with_failover(formatted_prompt, estimated_tokens=1500)
    return {"risk_analysis": response.content}

@standard_retry
def judge_agent(state: AgentState):
    if state.get("is_deep_dive"):
        # 🔥 FIX: Extremely strict conversational instructions
        sys_prompt = """You are a Senior Legal Counsel advising a client.
Your ONLY job is to execute the user's specific request (e.g., drafting an email, summarizing text, predicting outcomes) using the provided document context.
Do NOT perform a risk audit unless explicitly asked.
Do NOT output JSON. Use clean, professional Markdown paragraphs.

Document Context:
{context}

User's Specific Instruction (EXECUTE THIS EXACTLY):
{query}"""

        prompt = ChatPromptTemplate.from_messages([
            ("system", sys_prompt),
            ("human", "Please fulfill my exact instruction.")
        ])
        
        formatted_prompt = prompt.invoke({
            "context": state["retrieved_context"][:5000],
            "query": state["query"]
        })
        
        response = llm_gateway.invoke_with_failover(formatted_prompt, estimated_tokens=2500)
        
        return {
            "final_verdict": response.content,
            "risk_level": "UNKNOWN",
            "risk_percentage": 50,
            "confidence_score": 95,
            "verification_attempts": 1,
            "verification_critique": "PASS" 
        }

    sys_prompt = """You are the Lead Legal Judge. Synthesize the analyses into an authoritative final structured verdict.

CRITICAL CONSTRAINTS:
1. 'original_text' must be a direct quote.
2. If information is missing, use "Unknown" or 50.
3. DO NOT OUTPUT MARKDOWN TABLES IN ANY JSON FIELD. Use standard paragraphs only.

Clause Analysis: {clause_analysis}
Risk Analysis: {risk_analysis}"""

    if state.get("verification_critique"):
        sys_prompt += f"\n\nCRITICAL AUDIT CRITIQUE (Your previous answer failed verification. Fix this minor issue):\n{state['verification_critique']}"

    prompt = ChatPromptTemplate.from_messages([
        ("system", sys_prompt),
        ("human", "{query}")
    ])
    
    formatted_prompt = prompt.invoke({
        "clause_analysis": state["clause_analysis"],
        "risk_analysis": state["risk_analysis"],
        "query": state["query"]
    })
    
    decision: JudgeDecision = llm_gateway.invoke_with_failover(formatted_prompt, schema=JudgeDecision, estimated_tokens=2500)
    
    return {
        "final_verdict": decision.final_verdict,
        "risk_level": decision.risk_level,
        "risk_percentage": decision.risk_percentage,
        "confidence_score": decision.confidence_score,
        "exposure_type": decision.exposure_type,
        "impact": decision.impact,
        "likelihood": decision.likelihood,
        "metadata_parties": decision.metadata_parties,
        "metadata_jurisdiction": decision.metadata_jurisdiction,
        "metadata_governing_law": decision.metadata_governing_law,
        "original_text": decision.original_text,
        "xai_analysis": decision.xai_analysis,
        "safe_alternative": decision.safe_alternative,
        "strategic_action_plan": decision.strategic_action_plan,
        "verification_attempts": state.get("verification_attempts", 0) + 1
    }

@standard_retry
def verifier_agent(state: AgentState):
    if state.get("is_deep_dive") or state.get("verification_critique") == "PASS":
        return {"verification_critique": ""} 

    sys_prompt = """You are a pragmatic Legal Auditor. Ensure the core legal claim does not completely contradict the Context.
Be forgiving of minor paraphrasing or conversational tone.
If reasonably faithful, set is_faithful=True and critique='PASS'."""

    human_prompt = """Judge Verdict: {verdict}
Context: {context}"""

    prompt = ChatPromptTemplate.from_messages([
        ("system", sys_prompt),
        ("human", human_prompt)
    ])
    
    formatted_prompt = prompt.invoke({
        "context": state["retrieved_context"][:3000],
        "verdict": state["final_verdict"]
    })
    
    report: VerificationReport = llm_gateway.invoke_with_failover(formatted_prompt, schema=VerificationReport, estimated_tokens=1500)
    return {"verification_critique": report.critique if not report.is_faithful else ""}

def trust_gate_node(state: AgentState):
    if state.get("verification_critique"):
        return {
            "final_verdict": state.get("final_verdict", "Analysis complete but unverified."),
            "risk_level": state.get("risk_level", "MEDIUM"),
            "risk_percentage": state.get("risk_percentage", 50),
            "confidence_score": 50,
            "verification_critique": "ACCEPTED_WITH_WARNINGS_BY_TRUST_GATE"
        }
    return {} 

def should_rejudge(state: AgentState):
    if not state.get("verification_critique") or state.get("is_deep_dive"):
        return "pass"
    if state.get("verification_attempts", 0) >= 2:
        return "trust_gate" 
    return "fail_rewrite"

def build_agent_graph():
    workflow = StateGraph(AgentState)
    workflow.add_node("clause_agent", clause_agent)
    workflow.add_node("risk_agent", risk_agent)
    workflow.add_node("judge_agent", judge_agent)
    workflow.add_node("verifier_agent", verifier_agent)
    workflow.add_node("trust_gate", trust_gate_node) 
    
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
            "pass": "trust_gate" 
        }
    )
    workflow.add_edge("trust_gate", END) 
    return workflow.compile()