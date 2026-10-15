from typing import TypedDict, List

class AgentState(TypedDict):
    """
    This is the shared memory notepad that our AI agents pass to each other.
    """
    query: str                   # What the user asked
    retrieved_context: str       # The text we got from our Vector Database
    
    clause_analysis: str         # What the Clause Agent thinks
    risk_analysis: str           # What the Risk Agent thinks
    
    debate_history: List[str]    # A log of their arguments
    final_verdict: str           # The Judge Agent's final decision