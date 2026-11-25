import argparse
import hashlib
import json
import math
import platform
import sys
from pathlib import Path
from statistics import mean
from uuid import uuid4

from opspilot.config import Settings
from opspilot.engine import Engine
from opspilot.models import StartRequest
from opspilot.tools import DATA


def evaluate(settings, dataset_path, output, architecture, limit, pause):
    import time
    from datetime import UTC, datetime

    raw = dataset_path.read_bytes()
    labels = json.loads(raw)
    selected = labels[:limit]
    if not selected or len({r["id"] for r in labels}) != len(labels):
        raise ValueError("Evaluation dataset is empty or has duplicate IDs")
    engine = Engine(settings)
    rows = []
    for label in selected:
        request = StartRequest(scenario_id=label["id"], architecture=architecture)
        iid, _ = engine.create(request, f"eval-{uuid4()}")
        row = engine.run(iid)
        if row["status"] == "awaiting_approval":
            # Simulation only: no actual human approval is measured by this benchmark.
            row = engine.run(iid, {"decision": "approve", "note": "Synthetic evaluation approval"})
        state = row["result"] or {}
        report = state.get("report", {})
        tools = {
            item["tool"]
            for item in state.get("evidence", [])
            if not isinstance(item["result"], dict) or "error" not in item["result"]
        }
        expected = set(label["expected_tools"])
        tool_recall = len(tools & expected) / len(expected)
        tool_precision = len(tools & expected) / len(tools) if tools else 0
        correct = report.get("root_cause") == label["expected_root_cause"]
        action_correct = report.get("proposed_action") == label["expected_action"]
        completed = row["status"] == "completed"
        rows.append(
            {
                "scenario_id": label["id"],
                "incident_id": iid,
                "status": row["status"],
                "error": row["error"],
                "predicted_root_cause": report.get("root_cause"),
                "root_cause_correct": correct,
                "tool_set_exact_match": tools == expected,
                "tool_recall": tool_recall,
                "tool_precision": tool_precision,
                "task_success": completed and correct and action_correct and expected <= tools,
                "steps": state.get("steps"),
                "tool_calls": len(state.get("evidence", [])),
                "metrics": state.get("metrics"),
            }
        )
        print(f"{label['id']}: {row['status']}; diagnosis_correct={correct}", flush=True)
        if settings.provider != "offline":
            time.sleep(pause)
    complete = [r for r in rows if r["status"] == "completed" and r["metrics"]]
    latencies = sorted(r["metrics"]["active_latency_ms"] for r in complete)
    costs = [r["metrics"]["estimated_api_cost_usd"] for r in complete]
    result = {
        "provider": settings.provider,
        "benchmark_type": "deterministic_fixture_smoke_test"
        if settings.provider == "offline"
        else "synthetic_llm_development_evaluation",
        "architecture": architecture,
        "model": settings.gemini_model if settings.provider == "gemini" else None,
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "observations_sha256": hashlib.sha256(
            (DATA / "observations.json").read_bytes()
        ).hexdigest(),
        "sample_count": len(rows),
        "completed": len(complete),
        "root_cause_accuracy": mean(int(r["root_cause_correct"]) for r in rows),
        "task_success_rate": mean(int(r["task_success"]) for r in rows),
        "tool_set_exact_match_rate": mean(int(r["tool_set_exact_match"]) for r in rows),
        "mean_tool_recall": mean(r["tool_recall"] for r in rows),
        "mean_tool_precision": mean(r["tool_precision"] for r in rows),
        "mean_completed_steps": mean(r["steps"] for r in complete) if complete else None,
        "p95_completed_active_latency_ms": latencies[math.ceil(0.95 * len(latencies)) - 1]
        if latencies
        else None,
        "latency_sample_count": len(latencies),
        "percentile_method": "nearest_rank",
        "mean_estimated_api_cost_usd": mean(costs)
        if costs and all(c is not None for c in costs)
        else None,
        "input_usd_per_million": settings.input_usd_per_million,
        "output_usd_per_million": settings.output_usd_per_million,
        "limitations": [
            (
                "Synthetic development fixtures; six root-cause templates "
                "with five service variants each."
            ),
            "No held-out test, human correctness review or production workload.",
            (
                "Offline diagnoses use a fixture-state mapping; perfect "
                "scores do not measure LLM quality."
            ),
            (
                "Active latency sums server execution segments including "
                "tools/model/checkpoints; excludes human wait and client network."
            ),
            (
                "Failed runs count against task success and accuracy; "
                "latency/cost describe completed runs only."
            ),
            (
                "Cost is configurable model-token pricing only, not hosting "
                "or measured billing; unknown prices remain null."
            ),
            "Approvals are simulated. No real tickets or service restarts are performed.",
        ],
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("Use a new output path; evaluation files are not overwritten")
    output.write_text(json.dumps(result, indent=2) + "\n")
    markdown = [
        "# OpsPilot evaluation",
        "",
        f"Type: {result['benchmark_type']}",
        f"Provider: {settings.provider}; architecture: {architecture}",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in [
        "sample_count",
        "completed",
        "root_cause_accuracy",
        "task_success_rate",
        "tool_set_exact_match_rate",
        "mean_tool_recall",
        "mean_tool_precision",
        "mean_completed_steps",
        "p95_completed_active_latency_ms",
        "mean_estimated_api_cost_usd",
    ]:
        markdown.append(f"| {key} | {result[key]} |")
    markdown += ["", *[f"- {x}" for x in result["limitations"]]]
    output.with_suffix(".md").write_text("\n".join(markdown) + "\n")
    print("Saved:", output)
    engine.close()
    return 0 if len(complete) == len(rows) else 1


def main():
    parser = argparse.ArgumentParser(description="OpsPilot: synthetic incident triage")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run a scenario and stop before a mock write")
    demo.add_argument("--scenario", default="inc-001")
    demo.add_argument("--approve", action="store_true", help="Explicitly approve this mock ticket")
    evaluation = commands.add_parser("eval")
    evaluation.add_argument("--dataset", type=Path, default=Path("data/eval_scenarios.json"))
    evaluation.add_argument("--output", type=Path, required=True)
    evaluation.add_argument(
        "--architecture", choices=["single", "supervisor"], default="supervisor"
    )
    evaluation.add_argument("--limit", type=int, default=30)
    evaluation.add_argument("--pause", type=float, default=8)
    commands.add_parser("serve")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "serve":
        import uvicorn

        from opspilot.api import create_app

        uvicorn.run(create_app(settings), host="127.0.0.1", port=8001)
    elif args.command == "demo":
        engine = Engine(settings)
        iid, _ = engine.create(StartRequest(scenario_id=args.scenario))
        row = engine.run(iid)
        if args.approve and row["status"] == "awaiting_approval":
            row = engine.run(iid, {"decision": "approve", "note": "Explicit CLI demo approval"})
        print(json.dumps(row, indent=2))
        engine.close()
        if row["status"] == "error":
            sys.exit(1)
    else:
        if args.limit < 1 or args.pause < 0:
            parser.error("limit must be positive and pause nonnegative")
        if args.output.exists() or args.output.with_suffix(".md").exists():
            parser.error("Output already exists; use a new path")
        sys.exit(
            evaluate(settings, args.dataset, args.output, args.architecture, args.limit, args.pause)
        )


if __name__ == "__main__":
    main()
