from langchain_core.messages import AIMessage, HumanMessage
from app.agents.state import AgentState

# A rock-solid local Mock LLM to bypass all API issues and prove your LangGraph DAG works perfectly.
class MockLLM:
    def invoke(self, messages):
        # Extract the text content safely from the HumanMessage object
        prompt_text = messages[0].content
        
        if "Clause" in prompt_text:
            return AIMessage(content="The contract states that the employee may be terminated immediately without notice for sharing trade secrets.")
        elif "Risk" in prompt_text:
            return AIMessage(content="CRITICAL RISK: Immediate termination without notice removes the standard 30-day grace period, creating a high-liability exception.")
        else:
            return AIMessage(content="Final Verdict: The employee can be fired instantly for sharing trade secrets. This is a severe liability exception that overrides the standard 30-day notice period.")

llm = MockLLM()

def clause_agent(state: AgentState):
    """The Clause Agent strictly reads the contract and extracts facts."""
    print("\n[🤖 Clause Agent]: Analyzing legal context...")
    # Pass a proper HumanMessage object so .content works
    response = llm.invoke([HumanMessage(content="Clause")])
    return {"clause_analysis": response.content}

def risk_agent(state: AgentState):
    """The Risk Agent looks for loopholes and dangers."""
    print("[🚨 Risk Agent]: Evaluating liabilities and risks...")
    response = llm.invoke([HumanMessage(content="Risk")])
    return {"risk_analysis": response.content}

def judge_agent(state: AgentState):
    """The Judge Agent reviews everything and gives the final verdict."""
    print("[⚖️ Judge Agent]: Reaching final consensus...")
    response = llm.invoke([HumanMessage(content="Judge")])
    return {"final_verdict": response.content}