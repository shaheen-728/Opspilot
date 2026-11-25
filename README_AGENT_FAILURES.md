# Record an agent failure and continue evaluation

The inc-019 attempt omitted run_sql. Its investigator node rejected the result with
"Investigation did not inspect required evidence tools". This is a tool-compliance
failure under the existing benchmark contract, not a provider outage. It should not
be retried until correct, deleted or reported as a successful investigation.

This additive patch provides a new evaluator. It does not change prompts, tools,
required-tool checks, model settings, the original evaluator or the saved application DB.

## Install

Stop any other evaluator/API processing these incidents. From ~/Documents/opspilot:

```bash
unzip ~/Downloads/OpsPilot-agent-failure-fix.zip -d .
uv run ruff check scripts/eval_resumable_v2.py tests/test_eval_agent_failures.py
uv run pytest -q
```

## Import the existing single-agent evaluation and continue

```bash
uv run python scripts/eval_resumable_v2.py \
  --architecture single \
  --limit 30 \
  --max-cases 5 \
  --pause 8 \
  --resume-from results/gemini-single-v1.progress.json \
  --output results/gemini-single-v2.json
```

Expected behavior:

- Reuse the completed inc-001 through inc-018 incident IDs without model calls.
- Confirm the exact missing-tool error in the inc-019 checkpoint.
- Save inc-019 as an agent failure with no accepted diagnosis. Do not retry it.
- Continue investigating up to five unresolved scenarios, starting at inc-020.
- Keep provider errors pending. Unknown validation/configuration errors still stop for inspection.

The migration verifies dataset, selected IDs, architecture, model/runtime, application code
hash and state directory. It refuses a changed identity. It imports the whole prior manifest,
not a cherry-picked subset; original progress files are preserved and their hash is recorded.

Do not edit code or .env while this run is active. Rerun with the same new output to resume:

```bash
uv run python scripts/eval_resumable_v2.py \
  --architecture single --limit 30 --max-cases 5 --pause 8 \
  --output results/gemini-single-v2.json
```

## Report interpretation

"Resolved" includes completed investigations plus confirmed agent failures. A report can be
complete with 29 completed and one agent failure; do not try to make that failure disappear.
Overall task success and accepted-diagnosis accuracy include the failed case in their denominator.
Completed-only diagnosis accuracy is also provided. Tool precision/recall use actual logged tools.
A failure with no saved usage has no fabricated latency/cost; timing denominator is explicit.

Files use the same .progress.json / .partial.json / .partial.md pattern. Once all 30 scenarios
are resolved, final results are results/gemini-single-v2.json and .md.

## Align the supervisor report under this scoring policy (no new model calls)

After completing the single-agent run, import the already completed supervisor run with:

```bash
uv run python scripts/eval_resumable_v2.py \
  --architecture supervisor --limit 30 --max-cases 1 --pause 8 \
  --resume-from results/gemini-supervisor-v2.progress.json \
  --output results/gemini-supervisor-v3.json
```

All 30 completed supervisor cases will be reused, including any incorrect outcomes. This only
aligns the report/scoring policy; it does not retrospectively measure first-attempt reliability.
Earlier manually recovered ClientErrors lack complete HTTP/cost history. Both benchmarks
must be described as synthetic development results after recovery, not production reliability.

## Verification

Added four regression tests cover migration with ID preservation, finalizing a genuine
missing-tool failure without re-execution, continuation to later cases, refusal of changed
runtime, retaining unknown validation errors for inspection, and provider failures remaining
pending. Combined suite: 37 passed, one real PostgreSQL test skipped. No model API calls
were used for these tests. The patch does not claim a fix to the model's tool-selection behavior.
