# Start OpsPilot

1. Extract the ZIP and open a terminal in `opspilot/`.
2. Run `bash run_local.sh`.
3. Open http://127.0.0.1:8001.
4. Click **Investigate**, review the report, then **Approve mock ticket**.

This first run is offline: no Gemini key, no Docker and no model API calls.
Internet is needed once to install the locked Python dependencies. You already use `uv` with
DocuRAG; if Python 3.12 is missing, run `uv python install 3.12` before syncing.

Stop with Ctrl+C. State remains in `var/`, so a saved incident can resume after restart.

To measure the offline pipeline:

```bash
uv run opspilot eval --output results/my-offline-run.json
```

To use actual Gemini agents, read **Gemini mode** in README.md, configure your private `.env`
and restart. To compare one agent with the specialist workflow, use the evaluation commands
in the README. Offline fixture scores are not LLM quality metrics.

The README also covers API commands, Postgres checkpoints, Docker, DocuRAG runbook integration,
Langfuse, tests and what the metrics do and do not measure.
