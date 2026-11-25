# Design decisions

- Use a native Gemini function-calling loop inside LangGraph so tool execution is explicit.
- Default to a deterministic offline fixture driver, clearly separated from live LLM results.
- Use specialist workflow nodes: supervisor, investigator, knowledge retrieval, schema reporter,
  approval and mock action. In Gemini supervisor mode the investigator,
  knowledge interpreter and reporter are separate LLM agents, controlled by deterministic routing.
  Single mode uses just the investigator LLM. No model can bypass the approval node.
- Use dynamic interrupt + Command(resume=...) and a durable checkpointer. SQLite is simplest
  locally; PostgreSQL is an optional supported checkpointer and the Compose configuration.
- Keep incident metadata, events and tickets in a separate persistent SQLite application database.
  A PostgreSQL checkpointer does not move those application tables to PostgreSQL.
- Serialize run mutation within one API process. Do not launch multiple Uvicorn workers.
- Create mock tickets only after approval. Use a unique incident_id in the ticket table so
  checkpoint replay cannot produce duplicate tickets. No actual service restarts or external writes.
- Constrain SQL with an AST allowlist, a read-only SQLite connection, query_only, an authorizer,
  a 100-row limit and a short execution deadline. This is a narrow fixture tool, not a general
  SQL security gateway. PostgreSQL read-only query roles are not implemented in this version.
- Use local lexical runbook retrieval by default. Optional DocuRAG integration is an actual HTTP
  tool; the default local tool is not embedding-based RAG. Index appropriate operational runbooks
  before measuring the external mode (the existing Kubernetes demo corpus is not these fixtures).
- Require health, log and SQL evidence before accepting Gemini findings. Validate cited evidence
  IDs, but do not pretend ID validity proves semantic support. Human review remains necessary.
- Keep evaluation truth in a separate file. Never send it to the model or tools.
- Six synthetic templates x five service variants = 30 development cases, not 30 independent
  failure mechanisms or a held-out test. Perfect deterministic smoke-test scores are expected.
- Pin provider/model/configuration and fixture identity per incident; refuse changed configuration
  on resume. Do not silently fall back from Gemini to the offline driver.
- Report exact tool-set match, tool precision and recall separately; average steps count workflow
  nodes, while tool_calls is a separate value. Active latency excludes the human approval wait.
- API-cost estimates require explicitly configured model prices; unknown prices remain null.
  Estimates exclude hosting, external tools and failed calls without usage metadata.
- JSONL audit traces always work locally. Langfuse is optional and fails open if unavailable.
- UI uses textContent for untrusted model/tool content, not HTML injection.
