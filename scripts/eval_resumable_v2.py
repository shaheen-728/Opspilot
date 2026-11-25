"""Resumable evaluation using the existing OpsPilot engine and saved checkpoints.

Run from your project root. This does not replace providers.py or its prompts.
"""

import argparse
import copy
import fcntl
import hashlib
import json
import math
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from uuid import uuid4

from opspilot.config import Settings
from opspilot.engine import Engine, checkpoint
from opspilot.models import StartRequest

POLICY_VERSION = 2
TRANSIENT_NAMES = {
    "ServerError",
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "ConnectError",
    "ReadError",
    "WriteError",
    "RemoteProtocolError",
    "NetworkError",
    "TransportError",
    "APIConnectionError",
    "APITimeoutError",
    "TimeoutError",
}
APPROVAL = {"decision": "approve", "note": "Synthetic evaluation approval"}


def stamp():
    return datetime.now(UTC).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
        f.flush()
        import os

        os.fsync(f.fileno())
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_digest():
    import opspilot

    base = Path(opspilot.__file__).parent
    h = hashlib.sha256()
    for path in sorted(base.rglob("*.py")):
        h.update(path.relative_to(base).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def verify_row(row, sid, architecture, engine):
    request = row["request"]
    if request["scenario_id"] != sid or request["architecture"] != architecture:
        raise ValueError("Incident scenario or architecture does not match evaluation")
    if request["description"] != StartRequest(scenario_id=sid).description:
        raise ValueError("Incident used another investigation description")
    if request.get("_runtime") != engine.runtime_identity():
        raise ValueError("Incident runtime differs; restore original settings before resuming")


def score(label, row):
    state = row["result"] or {}
    report = state["report"]
    tools = {
        e["tool"]
        for e in state.get("evidence", [])
        if not isinstance(e["result"], dict) or "error" not in e["result"]
    }
    expected = set(label["expected_tools"])
    correct = report["root_cause"] == label["expected_root_cause"]
    action_correct = report["proposed_action"] == label["expected_action"]
    return {
        "scenario_id": label["id"],
        "incident_id": row["id"],
        "root_cause_correct": correct,
        "predicted_root_cause": report["root_cause"],
        "tool_set_exact_match": tools == expected,
        "tool_precision": len(tools & expected) / len(tools) if tools else 0,
        "tool_recall": len(tools & expected) / len(expected),
        "task_success": correct and action_correct and expected <= tools,
        "steps": state.get("steps"),
        "tool_calls": len(state.get("evidence", [])),
        "metrics": state["metrics"],
    }


def finalize_tool_failure(engine, row, label):
    """Record only a confirmed model tool-compliance failure; never a generic ValueError."""
    if row["status"] != "error" or row["error"] != "ValueError":
        return None
    with checkpoint(engine.settings) as saver:
        graph = engine.graph(saver)
        snapshot = graph.get_state({"configurable": {"thread_id": row["id"]}})
    errors = [str(t.error) for t in snapshot.tasks if t.error]
    reason = "Investigation did not inspect required evidence tools"
    if not any(reason in error for error in errors):
        return None
    events = engine.store.events(row["id"])
    ends = [i for i, e in enumerate(events) if e["type"] == "error"]
    end = ends[-1] if ends else len(events)
    start = ends[-2] + 1 if len(ends) > 1 else 0
    tools = {
        e["data"]["tool"]
        for e in events[start:end]
        if e["type"] == "tool_result"
        and (not isinstance(e["data"]["result"], dict) or "error" not in e["data"]["result"])
    }
    expected = set(label["expected_tools"])
    # A required tool that failed is different from the model omitting it.
    if any(
        e["type"] == "tool_result"
        and e["data"]["tool"] in expected
        and isinstance(e["data"]["result"], dict)
        and "error" in e["data"]["result"]
        for e in events[start:end]
    ):
        return None
    return {
        "scenario_id": label["id"],
        "incident_id": row["id"],
        "outcome": "agent_failure",
        "failure_reason": reason,
        "checkpoint_errors": errors,
        "predicted_root_cause": None,
        "root_cause_correct": False,
        "task_success": False,
        "tool_set_exact_match": tools == expected,
        "tool_precision": len(tools & expected) / len(tools) if tools else 0,
        "tool_recall": len(tools & expected) / len(expected),
        "actual_successful_tools": sorted(tools),
        "steps": None,
        "tool_calls": sum(e["type"] == "tool_result" for e in events[start:end]),
        "metrics": None,
    }


def report(state, labels, output):
    rows = [e["score"] for e in state["runs"].values() if e.get("score")]
    finished = [r for r in rows if r.get("outcome") != "agent_failure"]
    failures = [r for r in rows if r.get("outcome") == "agent_failure"]
    pending = [q["id"] for q in labels if not state["runs"].get(q["id"], {}).get("score")]
    complete = not pending
    timed = [r for r in finished if r.get("metrics")]
    latencies = sorted(r["metrics"]["active_latency_ms"] for r in timed)
    costs = [r["metrics"]["estimated_api_cost_usd"] for r in timed]

    def avg(key, collection=None):
        collection = rows if collection is None else collection
        return mean(r[key] for r in collection) if collection else None

    result = {
        "complete": complete,
        "provisional": not complete,
        "updated_at": stamp(),
        "identity": state["identity"],
        "migration": state.get("migration"),
        "selected_scenarios": len(labels),
        "resolved_scenarios": len(rows),
        "completed_scenarios": len(finished),
        "agent_failure_scenarios": len(failures),
        "metric_denominator": len(rows),
        "pending_scenarios": pending,
        "root_cause_accuracy": avg("root_cause_correct"),
        "completed_only_diagnosis_accuracy": avg("root_cause_correct", finished),
        "task_success_rate": avg("task_success"),
        "tool_set_exact_match_rate": avg("tool_set_exact_match"),
        "mean_tool_precision": avg("tool_precision"),
        "mean_tool_recall": avg("tool_recall"),
        "mean_completed_steps": avg("steps", finished),
        "p95_completed_active_latency_ms": latencies[math.ceil(0.95 * len(latencies)) - 1]
        if timed
        else None,
        "latency_sample_count": len(timed),
        "percentile_method": "nearest_rank",
        "mean_estimated_api_cost_usd": mean(costs)
        if costs and all(c is not None for c in costs)
        else None,
        "limitations": [
            "Synthetic development fixtures with six templates; not held-out production data.",
            "Missing required evidence tools is an agent failure; it is not retried until passing.",
            "No accepted diagnosis counts as failure; completed-only accuracy is separate.",
            "Provider failures are unresolved, not assigned an incorrect diagnosis.",
            "Partial averages use resolved cases only and may be biased.",
            "Latency/cost cover completed cases, excluding human wait and failed segments.",
            "Legacy manual retries lack HTTP details; first-attempt reliability is not measured.",
            "Imported prompt/code identity is verified; only the evaluation policy changes.",
            "Mock approvals do not execute remediation or create real tickets.",
        ],
        "rows": rows,
        "attempt_history": {sid: e.get("attempts", []) for sid, e in state["runs"].items()},
    }
    target = output if complete else output.with_name(output.stem + ".partial.json")
    save(target, result)
    lines = [
        "# OpsPilot evaluation (agent failures included)",
        "",
        "Status: complete" if complete else "Status: PARTIAL — provisional",
        f"Resolved: {len(rows)}/{len(labels)}",
        f"Completed: {len(finished)}; agent failures: {len(failures)}",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in [
        "root_cause_accuracy",
        "completed_only_diagnosis_accuracy",
        "task_success_rate",
        "tool_set_exact_match_rate",
        "mean_tool_precision",
        "mean_tool_recall",
        "mean_completed_steps",
        "p95_completed_active_latency_ms",
        "latency_sample_count",
        "mean_estimated_api_cost_usd",
    ]:
        value = result[key]
        lines.append(f"| {key} | {'N/A' if value is None else f'{value:.6f}'} |")
    lines += ["", *[f"- {x}" for x in result["limitations"]]]
    if pending:
        lines += ["", "Unresolved: " + ", ".join(pending)]
    target.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result, target


def run(args, settings=None):
    settings = settings or Settings()
    raw = args.dataset.read_bytes()
    labels = json.loads(raw)[: args.limit]
    if not labels or len({q["id"] for q in labels}) != len(labels):
        raise ValueError("Dataset is empty or contains duplicate selected IDs")
    selected = {q["id"] for q in labels}
    for label in labels:
        if not label["expected_tools"]:
            raise ValueError("Expected tool set must not be empty")
    engine = Engine(settings)
    progress = args.output.with_name(args.output.stem + ".progress.json")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    identity = {
        "policy_version": POLICY_VERSION,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "selected_ids": [q["id"] for q in labels],
        "architecture": args.architecture,
        "runtime": engine.runtime_identity(),
        "package_code_sha256": code_digest(),
        "runner_sha256": digest(Path(__file__)),
        "state_directory": str(settings.state_dir.resolve()),
    }
    # One writer per evaluation. Also stop any API/other runner touching these same incidents.
    lock_path = progress.with_suffix(".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            engine.close()
            raise RuntimeError("Another evaluator is using this output") from None
        if progress.exists():
            state = json.loads(progress.read_text())
            if state["identity"] != identity:
                engine.close()
                raise ValueError("Inputs, code or settings changed; use a new output identity")
        else:
            if args.output.exists():
                engine.close()
                raise ValueError("Output already exists without resumable progress; use a new path")
            state = {
                "identity": identity,
                "run_id": str(uuid4()),
                "runs": {},
                "created_at": stamp(),
                "platform": platform.platform(),
            }
            if args.resume_from:
                legacy = json.loads(args.resume_from.read_text())
                for key in [
                    "dataset_sha256",
                    "selected_ids",
                    "architecture",
                    "runtime",
                    "package_code_sha256",
                    "state_directory",
                ]:
                    if legacy["identity"].get(key) != identity.get(key):
                        raise ValueError(f"Migration refused: {key} differs from original run")
                if not set(legacy["runs"]) <= selected:
                    raise ValueError("Migration contains unexpected scenario IDs")
                state["run_id"] = legacy["run_id"]
                state["runs"] = copy.deepcopy(legacy["runs"])
                state["migration"] = {
                    "source_path": str(args.resume_from),
                    "source_sha256": digest(args.resume_from),
                    "source_identity": legacy["identity"],
                    "reason": "Record tool-compliance failures without model retries",
                }
                for sid, entry in state["runs"].items():
                    verify_row(
                        engine.store.get(entry["incident_id"]), sid, args.architecture, engine
                    )
            save(progress, state)

        # Adoption is explicit: never cherry-pick a different successful pilot automatically.
        for value in args.adopt:
            sid, separator, iid = value.partition("=")
            if not separator or sid not in selected:
                raise ValueError("Use --adopt SELECTED_SCENARIO=EXISTING_INCIDENT_ID")
            row = engine.store.get(iid)
            verify_row(row, sid, args.architecture, engine)
            existing = state["runs"].get(sid)
            if existing and existing["incident_id"] != iid:
                raise ValueError("A different incident is already registered for this scenario")
            if not existing:
                state["runs"][sid] = {
                    "incident_id": iid,
                    "adopted": True,
                    "score": None,
                    "attempts": [
                        {
                            "time": stamp(),
                            "status": row["status"],
                            "error": row["error"],
                            "origin": "legacy_before_adoption",
                        }
                    ],
                }
        save(progress, state)
        report(state, labels, args.output)
        attempted = 0
        consecutive_provider_errors = 0
        interrupted = False
        try:
            for label in labels:
                sid = label["id"]
                entry = state["runs"].get(sid)
                if entry is None:
                    if attempted >= args.max_cases:
                        break
                    key = f"eval:{state['run_id']}:{sid}"
                    iid, _ = engine.create(
                        StartRequest(scenario_id=sid, architecture=args.architecture),
                        key,
                    )
                    entry = {
                        "incident_id": iid,
                        "adopted": False,
                        "attempts": [],
                        "score": None,
                    }
                    state["runs"][sid] = entry
                    save(progress, state)  # Persist ID BEFORE any model call.
                row = engine.store.get(entry["incident_id"])
                verify_row(row, sid, args.architecture, engine)
                if entry.get("finalized_agent_failure"):
                    if row["status"] != "error":
                        raise ValueError("A finalized failure was changed outside the evaluation")
                    print(f"{sid}: recorded agent failure (not retried)", flush=True)
                    continue
                failure = finalize_tool_failure(engine, row, label)
                if failure:
                    entry["score"] = failure
                    entry["finalized_agent_failure"] = {
                        "time": stamp(),
                        "reason": failure["failure_reason"],
                    }
                    save(progress, state)
                    report(state, labels, args.output)
                    print(
                        f"{sid}: recorded agent failure — missing required evidence tool",
                        flush=True,
                    )
                    continue
                if row["status"] == "completed":
                    entry["score"] = score(label, row)
                    save(progress, state)
                    print(f"{sid}: reused", flush=True)
                    continue
                if attempted >= args.max_cases:
                    break
                if row["status"] == "error" and row["error"] not in TRANSIENT_NAMES:
                    print(
                        f"{sid}: blocked by {row['error']}; fix before resuming",
                        flush=True,
                    )
                    break
                if row["status"] not in {"running", "error", "awaiting_approval"}:
                    print(
                        f"{sid}: unexpected saved status {row['status']}; not replaced",
                        flush=True,
                    )
                    break
                print(
                    f"{sid}: resuming {row['id']}"
                    if entry["attempts"]
                    else f"{sid}: investigating",
                    flush=True,
                )
                attempted += 1
                entry["attempts"].append({"time": stamp(), "status": "started"})
                save(progress, state)
                try:
                    if row["status"] != "awaiting_approval":
                        row = engine.run(row["id"])
                    if row["status"] == "awaiting_approval":
                        row = engine.run(row["id"], APPROVAL)
                except KeyboardInterrupt:
                    entry["attempts"][-1].update(status="interrupted", error="KeyboardInterrupt")
                    interrupted = True
                    break
                entry["attempts"][-1].update(status=row["status"], error=row["error"])
                failure = finalize_tool_failure(engine, row, label)
                if failure:
                    entry["score"] = failure
                    entry["finalized_agent_failure"] = {
                        "time": stamp(),
                        "reason": failure["failure_reason"],
                    }
                    consecutive_provider_errors = 0
                    save(progress, state)
                    report(state, labels, args.output)
                    print(f"  recorded agent failure: {failure['failure_reason']}", flush=True)
                    continue
                if row["status"] == "completed":
                    entry["score"] = score(label, row)
                    consecutive_provider_errors = 0
                    print(
                        f"  saved; diagnosis_correct={entry['score']['root_cause_correct']}",
                        flush=True,
                    )
                elif row["status"] == "error" and row["error"] in TRANSIENT_NAMES:
                    entry["score"] = None
                    consecutive_provider_errors += 1
                    print(
                        f"  deferred: {row['error']}; no quality score saved",
                        flush=True,
                    )
                else:
                    print(
                        f"  blocked: {row['error'] or row['status']}; no quality score saved",
                        flush=True,
                    )
                save(progress, state)
                report(state, labels, args.output)
                if row["status"] != "completed" and row["error"] not in TRANSIENT_NAMES:
                    break
                if consecutive_provider_errors >= args.max_provider_errors:
                    print(
                        "Provider circuit breaker: stopping this pass; rerun later to resume.",
                        flush=True,
                    )
                    break
                if settings.provider != "offline":
                    time.sleep(args.pause)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            state["updated_at"] = stamp()
            save(progress, state)
            summary, target = report(state, labels, args.output)
            engine.close()
            print(
                f"Resolved: {summary['resolved_scenarios']}/{len(labels)}; "
                f"completed={summary['completed_scenarios']}; "
                f"agent_failures={summary['agent_failure_scenarios']}"
            )
            print(f"Progress: {progress}")
            print(f"Report: {target.with_suffix('.md')}")
        if interrupted:
            print("Interrupted safely. Rerun the same command to resume.")
            return 130
        return 0 if summary["complete"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/eval_scenarios.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=["single", "supervisor"], default="supervisor")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--max-cases", type=int, default=30, help="Unresolved cases attempted this pass"
    )
    parser.add_argument("--max-provider-errors", type=int, default=3)
    parser.add_argument("--pause", type=float, default=8)
    parser.add_argument("--adopt", action="append", default=[])
    parser.add_argument(
        "--resume-from",
        type=Path,
        default=None,
        help="Import a matching prior progress manifest into a NEW output",
    )
    args = parser.parse_args()
    if min(args.limit, args.max_cases, args.max_provider_errors) < 1 or args.pause < 0:
        parser.error("Counts must be positive and pause nonnegative")
    try:
        return run(args)
    except (ValueError, KeyError, RuntimeError) as error:
        print(f"Cannot continue: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
