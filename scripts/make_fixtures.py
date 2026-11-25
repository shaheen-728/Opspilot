"""Generate synthetic observations separately from evaluation answer labels."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAUSES = [
    (
        "database_lock",
        "ERROR",
        "transaction waiting on lock; blocking_pid=412",
        "blocked",
        "high",
        "Inspect the blocking transaction; ask the database owner before cancelling it.",
    ),
    (
        "upstream_timeout",
        "ERROR",
        "upstream request deadline exceeded; dependency=bank",
        "timeout",
        "high",
        "Check dependency health and retry budgets; avoid a retry storm.",
    ),
    (
        "connection_pool_exhausted",
        "ERROR",
        "connection pool exhausted; active=20 max=20",
        "pool_full",
        "high",
        "Inspect long-running queries and connection leaks before changing pool limits.",
    ),
    (
        "disk_full",
        "ERROR",
        "write failed: no space left on device; usage_pct=99",
        "disk_full",
        "high",
        "Inspect disk usage and approved retention policies; do not delete files automatically.",
    ),
    (
        "bad_deployment",
        "ERROR",
        "startup failed: missing required configuration PAYMENT_REGION",
        "config_error",
        "high",
        (
            "Compare deployment configuration with the last healthy "
            "version; propose an approved rollback."
        ),
    ),
    (
        "healthy",
        "INFO",
        "request completed; status=200; latency_ms=42",
        "ok",
        "low",
        "Observe service metrics; no corrective write is indicated by this snapshot.",
    ),
]
observations = []
labels = []
runbooks = []
for n, (cause, level, message, state, severity, action) in enumerate(CAUSES):
    runbooks.append(
        {
            "id": f"rb-{n + 1}",
            "title": cause.replace("_", " "),
            "content": f"Symptoms: {message}. Investigation: {action}",
        }
    )
    for variation in range(5):
        i = n * 5 + variation + 1
        sid = f"inc-{i:03}"
        service = ["payments", "orders", "inventory", "gateway", "billing"][variation]
        observations.append(
            {
                "id": sid,
                "service": service,
                "alert": (
                    f"Investigate {service}: "
                    + ("latency or error alert" if cause != "healthy" else "health review")
                ),
                "health": {
                    "status": "healthy" if state == "ok" else "degraded",
                    "http_status": 200 if state == "ok" else 503,
                },
                "logs": [
                    {"timestamp": "2026-11-24T10:00:00Z", "level": level, "message": message},
                    {
                        "timestamp": "2026-11-24T09:59:00Z",
                        "level": "INFO",
                        "message": f"service={service} accepted request trace=synthetic-{i}",
                    },
                ],
                "db_state": state,
                "open_orders": i + 7,
            }
        )
        labels.append(
            {
                "id": sid,
                "expected_root_cause": cause,
                "expected_tools": [
                    "get_service_health",
                    "search_logs",
                    "run_sql",
                    "search_runbooks",
                ],
                "expected_action": "none" if cause == "healthy" else "create_ticket",
            }
        )
(ROOT / "data" / "observations.json").write_text(json.dumps(observations, indent=2) + "\n")
(ROOT / "data" / "eval_scenarios.json").write_text(json.dumps(labels, indent=2) + "\n")
(ROOT / "data" / "runbooks.json").write_text(json.dumps(runbooks, indent=2) + "\n")
print("Wrote 30 synthetic observations, 30 labels and 6 runbooks.")
