# Package verification

- Ruff lint and formatting: passed.
- Pytest: 26 passed, 1 skipped (real PostgreSQL checkpointer requires a test database).
- Both offline architectures: 30/30 synthetic fixture runs completed.
- Native Gemini tool-call messages and structured specialist agents: tested with mocked SDK responses.
- Restart at approval and crash after mock-ticket commit: tested, no duplicate ticket.
- Installable wheel: built; packaged fixtures and UI checked.
- Real Gemini requests, Langfuse account export, Docker build and PostgreSQL server integration were not run here.

The included offline reports are wiring checks, not evidence of LLM accuracy or deployment performance.
Run the live-provider evaluation on your own account before using LLM performance metrics in a resume.
