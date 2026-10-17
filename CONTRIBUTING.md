# Contributing to AMALDA

We appreciate your interest in contributing to the AMALDA engine. To maintain the integrity, security, and reliability of this legal intelligence tool, we observe a strict development workflow.

## Development Standards

1. **Fork & Branch:** Create a dedicated feature branch from `main` (`git checkout -b feature/architecture-update`).
2. **Strict Typing:** Python 3.12+ type hinting is mandatory for all new asynchronous functions and API endpoints.
3. **Code Quality:** All modifications must adhere to PEP 8 standards. Ensure clean separation of concerns between the vector store adapters and the LangGraph agent nodes.
4. **Zero-Hallucination Threshold:** If you are modifying the LLM reflection loops, your pull request must include proof that the `Verifier Agent` still blocks ungrounded claims.

## Pull Request Submission

When opening a Pull Request, please provide a clear summary of the architectural changes, potential performance impacts on the ASGI server, and any required environment variable updates.