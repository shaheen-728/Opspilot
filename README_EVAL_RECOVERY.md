# Resume the interrupted Gemini evaluation

This patch ADDS a separate runner and seven regression tests. It does not replace
providers.py, engine.py, prompts, your .env, saved databases or the original evaluator.
Do not use `opspilot eval` for this recovered run; use the script below instead.

## Install

Stop any running OpsPilot API or evaluator first. From ~/Documents/opspilot:

```bash
unzip ~/Downloads/OpsPilot-evaluation-fix.zip -d .
uv run ruff check scripts/eval_resumable.py tests/test_eval_resumable.py
uv run pytest -q
```

## Recover the four incidents you reported

Keep the same .env/model/provider, prompt code and var/ databases. This command
adopts exactly the four original evaluation incident IDs, not either successful pilot.
It retries up to four unresolved cases this pass. It stops after three consecutive
provider failures to avoid spending requests throughout an outage.

```bash
uv run python scripts/eval_resumable.py \
  --architecture supervisor \
  --limit 30 \
  --max-cases 4 \
  --pause 8 \
  --output results/gemini-supervisor-v2.json \
  --adopt inc-001=c2c1bd6a-5e2e-4959-9d5f-26707b0863de \
  --adopt inc-002=f49fdfa7-944e-45df-8876-5d374057782f \
  --adopt inc-003=f087c0e4-bf76-445f-84c9-26eb4f2d2d57 \
  --adopt inc-004=ed655a2f-f41a-4032-bba2-df16541b20fb
```

A deferred run is not assigned a quality score. It retains the same incident ID and
LangGraph thread, so successful checkpointed nodes are reused when available.
A failed node may need to repeat its model call; exactly-once external API billing
cannot be guaranteed. Mock tickets remain idempotent through the application store.

## Continue later

Once provider calls are working, finish with the SAME output path:

```bash
uv run python scripts/eval_resumable.py \
  --architecture supervisor \
  --limit 30 \
  --pause 8 \
  --output results/gemini-supervisor-v2.json
```

Completed wrong diagnoses are reused too; this runner does not retry them until
correct. An incomplete run with a validation/configuration error is blocked for
inspection rather than silently dropped. A changed code/model/dataset identity is
rejected, so do not edit core code or pricing midway through this evaluation.
`--max-cases` limits work in one invocation and can change without changing the sample.

## Files

- `results/gemini-supervisor-v2.progress.json`: atomic per-incident recovery manifest.
- `results/gemini-supervisor-v2.partial.json` and `.partial.md`: provisional report.
- `results/gemini-supervisor-v2.json` and `.md`: produced when all selected cases complete.

Exit 0 means complete; exit 2 means unresolved cases remain; exit 130 means a safely
interrupted pass. Rerun after provider recovery to continue. Do not delete var/ or
progress files. Do not run two evaluators or an API worker against these incidents.

Partial quality averages include COMPLETED CASES ONLY and can be biased; they are
not final metrics for all 30 scenarios. Coverage and pending IDs are shown explicitly.
Latency/cost retain the original engine's limitations: successful segments only;
failed work without saved usage is excluded. No p95 or cost number is invented.
Original legacy runs did not pin their prompt hash; the recovery manifest pins
current source and runner hashes from adoption forward and states this limitation.

## Verification

Seven added offline tests verify completed-result reuse without execution, provider
error deferral, continuation with the same ID, explicit adoption, interrupt recovery,
configuration mismatch rejection and a per-pass case limit. Tested with the original
suite: 33 passed, 1 PostgreSQL integration test skipped. No model API calls were used
for patch verification. Live provider recovery still depends on Gemini availability.
