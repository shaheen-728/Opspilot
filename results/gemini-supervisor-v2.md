# OpsPilot resumable evaluation

Status: complete
Completed: 30/30

| Metric | Value |
| --- | ---: |
| root_cause_accuracy | 1.000000 |
| task_success_rate | 1.000000 |
| tool_set_exact_match_rate | 1.000000 |
| mean_tool_precision | 1.000000 |
| mean_tool_recall | 1.000000 |
| mean_completed_steps | 6.000000 |
| p95_completed_active_latency_ms | 47773.621000 |
| mean_estimated_api_cost_usd | N/A |

- Synthetic development fixtures with six templates; not held-out production data.
- Unresolved runs have no score; partial completed-case averages may be biased.
- Wrong completed diagnoses are incorrect; provider absence is not a diagnosis.
- Latency sums successful segments; excludes human wait, network and failed attempts.
- Costs exclude failed calls with missing usage and repeated failed-node work.
- Legacy runs lack original prompt/code hashes; identities are pinned at adoption.
- Approvals are simulated for local mock tickets, never real remediation.
