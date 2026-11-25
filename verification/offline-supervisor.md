# OpsPilot evaluation

Type: deterministic_fixture_smoke_test
Provider: offline; architecture: supervisor

| Metric | Value |
| --- | ---: |
| sample_count | 30 |
| completed | 30 |
| root_cause_accuracy | 1 |
| task_success_rate | 1 |
| tool_set_exact_match_rate | 1 |
| mean_tool_recall | 1.0 |
| mean_tool_precision | 1.0 |
| mean_completed_steps | 6 |
| p95_completed_active_latency_ms | 66.181 |
| mean_estimated_api_cost_usd | 0.0 |

- Synthetic development fixtures; six root-cause templates with five service variants each.
- No held-out test, human correctness review or production workload.
- Offline diagnoses use a fixture-state mapping; perfect scores do not measure LLM quality.
- Active latency sums server execution segments including tools/model/checkpoints; excludes human wait and client network.
- Failed runs count against task success and accuracy; latency/cost describe completed runs only.
- Cost is configurable model-token pricing only, not hosting or measured billing; unknown prices remain null.
- Approvals are simulated. No real tickets or service restarts are performed.
