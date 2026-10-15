import os
import pytest
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

def test_groq_api_authentication():
    """
    Strictly asserts that the GROQ_API_KEY is valid and the upstream
    provider successfully authenticates and routes a live inference request.
    It dynamically selects an available model to prevent test failures due to deprecations.
    """
    api_key = os.getenv("GROQ_API_KEY")
    assert api_key, "GROQ_API_KEY is missing from environment variables."
    
    client = Groq(api_key=api_key)
    
    # Dynamically fetch available models
    models_response = client.models.list()
    assert models_response.data, "No models available for this API key."
    
    # Prefer a fast Llama 3 model if available, otherwise pick the first active one
    target_model = models_response.data[0].id
    for model in models_response.data:
        if "llama-3" in model.id.lower() and "tool" not in model.id.lower():
            target_model = model.id
            break
            
    # Perform a minimal, live inference call
    response = client.chat.completions.create(
        messages=[{"role": "user", "content": "Ping. Reply with exactly 'Pong'."}],
        model=target_model,
        max_tokens=5,
        temperature=0.0
    )
    
    assert response is not None
    assert len(response.choices) > 0
    assert "pong" in response.choices[0].message.content.strip().lower()
