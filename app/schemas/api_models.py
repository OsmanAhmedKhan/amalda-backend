from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class ActionableAdvice(BaseModel):
    advice: str
    exact_quote: str = Field(description="The exact substring from the document that proves this advice.")
    source_id: str = Field(description="The chunk ID where this quote was found.")

class QueryRequest(BaseModel):
    query: str = Field(..., description="The user's legal question or follow-up.")
    thread_id: Optional[str] = Field(None, description="Optional UUID to continue an existing conversation memory.")
    document_ids: Optional[List[str]] = Field(None, description="Optional list of filenames to restrict the search to.")
    strict_mode: Optional[bool] = Field(False, description="Enforce strict zero-hallucination parameters.")

class AnalysisResponse(BaseModel):
    thread_id: str
    query: str
    retrieved_evidence: str
    clause_analysis: str
    risk_analysis: str
    final_verdict: str
    risk_level: str
    confidence_score: int
    key_actions: List[ActionableAdvice] # Upgraded for exact provenance
    verification_attempts: int = Field(default=1, description="Number of self-correction loops taken.")
    sources: Optional[List[Dict[str, Any]]] = Field(default=[], description="List of source metadata citations for Next.js UI")

class UploadResponse(BaseModel):
    filename: str
    message: str

class ErrorSchema(BaseModel):
    error_code: str
    message: str
    actionable_remedy: str