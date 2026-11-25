import hashlib
import json
import threading
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from time import perf_counter
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from opspilot.models import Finding, Guidance, Report
from opspilot.providers import (
    add_usage,
    estimate,
    model_investigate,
    model_structured,
    offline_finding,
    prompt_for,
)
from opspilot.store import Conflict, Store
from opspilot.tools import DATA, Tools, load_scenario


class State(TypedDict, total=False):
    incident_id: str
    request: dict
    evidence: list[dict]
    finding: dict
    report: dict
    decision: dict
    ticket: dict | None
    usage: dict
    steps: int
    runbook_references: list[str]
    guidance: dict


@contextmanager
def checkpoint(settings):
    if settings.checkpoint_url:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(settings.checkpoint_url.get_secret_value()) as saver:
            saver.setup()
            yield saver
    else:
        with SqliteSaver.from_conn_string(str(settings.state_dir / "checkpoints.sqlite")) as saver:
            yield saver


class Engine:
    def __init__(self, settings):
        settings.prepare()
        self.settings = settings
        self.store = Store(settings.state_dir)
        # One API process only. SQLite app-state serializes side effects and run mutation.
        self.lock = threading.RLock()
        self.tracer = None
        if settings.langfuse_enabled:
            try:
                from langfuse import Langfuse

                if settings.langfuse_public_key and settings.langfuse_secret_key:
                    self.tracer = Langfuse(
                        public_key=settings.langfuse_public_key,
                        secret_key=settings.langfuse_secret_key.get_secret_value(),
                        base_url=settings.langfuse_base_url,
                    )
            except Exception:
                pass

    def close(self):
        if self.tracer:
            try:
                self.tracer.flush()
            except Exception:
                pass

    def event(self, iid, kind, payload):
        self.store.event(iid, kind, payload)
        with (self.settings.state_dir / "traces.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {
                        "time": datetime.now(UTC).isoformat(),
                        "incident_id": iid,
                        "type": kind,
                        "payload": payload,
                    }
                )
                + "\n"
            )

    @contextmanager
    def trace(self, iid):
        with ExitStack() as stack:
            if self.settings.langfuse_enabled:
                try:
                    if self.tracer is None:
                        raise ValueError("Tracing not configured")
                    stack.enter_context(
                        self.tracer.start_as_current_observation(
                            name="opspilot-triage", as_type="span", metadata={"incident_id": iid}
                        )
                    )
                except Exception:
                    self.event(iid, "tracing_unavailable", {})
            yield

    def graph(self, saver):
        settings = self.settings

        def supervisor(state):
            self.event(state["incident_id"], "supervisor", {"route": "investigator"})
            return {"steps": 1}

        def investigator(state):
            iid = state["incident_id"]
            scenario = load_scenario(state["request"]["scenario_id"])
            tools = Tools(settings, scenario, iid)
            evidence = []

            def execute(name, args):
                if len(evidence) >= settings.max_tool_calls:
                    raise RuntimeError("Tool budget exhausted")
                started = perf_counter()
                try:
                    result = tools.call(name, args)
                except Exception as error:
                    # Model gets a bounded diagnostic; database/credential values are not returned.
                    result = {
                        "error": type(error).__name__,
                        "message": "Invalid tool request or unavailable data",
                    }
                item = {
                    "id": f"ev-{len(evidence) + 1}",
                    "tool": name,
                    "args": args,
                    "result": result,
                    "latency_ms": round((perf_counter() - started) * 1000, 3),
                }
                evidence.append(item)
                self.event(iid, "tool_result", item)
                return {"evidence_id": item["id"], "data": result}

            if settings.provider == "offline":
                execute("get_service_health", {"service": scenario["service"]})
                execute("search_logs", {"service": scenario["service"]})
                execute(
                    "run_sql", {"query": "SELECT service,status,open_orders FROM incident_state"}
                )
                if state["request"]["architecture"] == "single":
                    execute("search_runbooks", {"query": json.dumps(evidence[-1]["result"])})
                finding = offline_finding(evidence)
                usage = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
            else:
                finding, usage = model_investigate(
                    settings,
                    tools,
                    prompt_for(scenario, state["request"]["description"]),
                    execute,
                    include_runbooks=state["request"]["architecture"] == "single",
                )
            valid = {
                e["id"]
                for e in evidence
                if not isinstance(e["result"], dict) or "error" not in e["result"]
            }
            if not set(finding.evidence_ids) <= valid:
                raise ValueError("Finding cites missing or failed evidence")
            required = {"get_service_health", "search_logs", "run_sql"}
            if not required <= {e["tool"] for e in evidence if e["id"] in valid}:
                raise ValueError("Investigation did not inspect required evidence tools")
            return {
                "finding": finding.model_dump(),
                "evidence": evidence,
                "usage": usage,
                "steps": state["steps"] + 1,
            }

        def knowledge(state):
            iid = state["incident_id"]
            if state["request"]["architecture"] == "single":
                refs = [
                    b["id"]
                    for e in state["evidence"]
                    if e["tool"] == "search_runbooks" and isinstance(e["result"], list)
                    for b in e["result"]
                ]
                return {"runbook_references": refs, "steps": state["steps"] + 1}
            tools = Tools(settings, load_scenario(state["request"]["scenario_id"]), iid)
            query = state["finding"]["root_cause"].replace("_", " ")
            evidence = list(state["evidence"])
            try:
                if len(evidence) >= settings.max_tool_calls:
                    raise RuntimeError("Tool budget exhausted before runbook search")
                books = tools.call("search_runbooks", {"query": query})
                result = books
            except Exception as error:
                books = []
                result = {"error": type(error).__name__, "message": "Runbook search failed"}
            item = {
                "id": f"ev-{len(evidence) + 1}",
                "tool": "search_runbooks",
                "args": {"query": query},
                "result": result,
            }
            self.event(iid, "tool_result", item)
            refs = [b["id"] for b in books]
            guidance = Guidance(
                summary="Review retrieved operational guidance.",
                runbook_ids=refs,
                next_steps=state["finding"]["next_steps"],
            )
            usage = state["usage"]
            if settings.provider == "gemini":
                guidance, new_usage = model_structured(
                    settings,
                    "knowledge",
                    Guidance,
                    {"finding": state["finding"], "runbooks": books},
                )
                if not set(guidance.runbook_ids) <= set(refs):
                    raise ValueError("Knowledge agent invented a runbook reference")
                usage = add_usage(settings, usage, new_usage)
            self.event(iid, "knowledge_guidance", guidance.model_dump())
            return {
                "evidence": evidence + [item],
                "runbook_references": guidance.runbook_ids,
                "guidance": guidance.model_dump(),
                "usage": usage,
                "steps": state["steps"] + 1,
            }

        def reporter(state):
            finding = Finding.model_validate(state["finding"])
            expected_action = "none" if finding.root_cause == "healthy" else "create_ticket"
            report = Report(
                **finding.model_dump(),
                runbook_references=state["runbook_references"],
                proposed_action=expected_action,
            )
            usage = state["usage"]
            if settings.provider == "gemini" and state["request"]["architecture"] == "supervisor":
                report, new_usage = model_structured(
                    settings,
                    "reporter",
                    Report,
                    {
                        "finding": state["finding"],
                        "guidance": state.get("guidance"),
                        "evidence": state["evidence"],
                        "required_action": expected_action,
                    },
                )
                if (
                    report.root_cause != finding.root_cause
                    or report.proposed_action != expected_action
                ):
                    raise ValueError("Reporter changed the diagnosis or action policy")
                valid_ids = {
                    e["id"]
                    for e in state["evidence"]
                    if not isinstance(e["result"], dict) or "error" not in e["result"]
                }
                if not set(report.evidence_ids) <= valid_ids:
                    raise ValueError("Reporter invented evidence")
                if not set(report.runbook_references) <= set(state["runbook_references"]):
                    raise ValueError("Reporter invented runbook references")
                usage = add_usage(settings, usage, new_usage)
            self.event(state["incident_id"], "report", report.model_dump())
            return {"report": report.model_dump(), "usage": usage, "steps": state["steps"] + 1}

        def approval(state):
            if state["report"]["proposed_action"] == "none":
                return {
                    "decision": {"decision": "reject", "note": "No write action proposed"},
                    "steps": state["steps"] + 1,
                }
            decision = interrupt(
                {"action": "create_ticket", "mock": True, "report": state["report"]}
            )
            from opspilot.models import ApprovalRequest

            decision = ApprovalRequest.model_validate(decision).model_dump()
            self.event(state["incident_id"], "approval", decision)
            return {"decision": decision, "steps": state["steps"] + 1}

        def action(state):
            ticket = None
            if state["decision"]["decision"] == "approve":
                ticket = self.store.ticket(
                    state["incident_id"], state["report"], state["decision"]["note"]
                )
            self.event(state["incident_id"], "action", {"mock_ticket": ticket})
            return {"ticket": ticket, "steps": state["steps"] + 1}

        builder = StateGraph(State)
        for name, node in [
            ("supervisor", supervisor),
            ("investigator", investigator),
            ("knowledge", knowledge),
            ("reporter", reporter),
            ("approval", approval),
            ("action", action),
        ]:
            builder.add_node(name, node)
        builder.add_edge(START, "supervisor")
        for a, b in zip(
            ["supervisor", "investigator", "knowledge", "reporter", "approval"],
            ["investigator", "knowledge", "reporter", "approval", "action"],
        ):
            builder.add_edge(a, b)
        builder.add_edge("action", END)
        return builder.compile(checkpointer=saver)

    def runtime_identity(self):
        return {
            "provider": self.settings.provider,
            "model": self.settings.gemini_model if self.settings.provider == "gemini" else None,
            "docurag_url": self.settings.docurag_url,
            "max_tool_calls": self.settings.max_tool_calls,
            "observations_sha256": hashlib.sha256(
                (DATA / "observations.json").read_bytes()
            ).hexdigest(),
            "runbooks_sha256": hashlib.sha256((DATA / "runbooks.json").read_bytes()).hexdigest(),
            "input_price": self.settings.input_usd_per_million,
            "output_price": self.settings.output_usd_per_million,
            "max_tokens": self.settings.max_tokens_per_incident,
            "workflow_version": 2,
        }

    def create(self, request, key=None):
        load_scenario(request.scenario_id)
        with self.lock:
            payload = request.model_dump()
            payload["_runtime"] = self.runtime_identity()
            row, created = self.store.create(key or str(uuid4()), payload)
            return row["id"], created

    def run(self, iid, decision=None):
        with self.lock, self.trace(iid):
            row = self.store.get(iid)
            if row["status"] in {"completed", "rejected"}:
                if decision is not None:
                    previous = row["result"].get("decision")
                    if previous != decision:
                        raise Conflict("Incident already has a different final decision")
                return row
            if row["request"]["_runtime"] != self.runtime_identity():
                raise Conflict("Runtime configuration changed; restore it or start a new incident")
            started = perf_counter()
            try:
                with checkpoint(self.settings) as saver:
                    graph = self.graph(saver)
                    config = {"configurable": {"thread_id": iid}, "recursion_limit": 30}
                    snap = graph.get_state(config)
                    if decision is not None:
                        if not snap.interrupts:
                            raise Conflict("No action awaits approval")
                        value = Command(resume=decision)
                    elif snap.values:
                        if snap.interrupts:
                            return row
                        value = None  # Retry failed node using its saved state.
                    else:
                        value = {"incident_id": iid, "request": row["request"]}
                    for _ in graph.stream(value, config, stream_mode="updates"):
                        pass
                    snap = graph.get_state(config)
                    state = dict(snap.values)
                    latency = (perf_counter() - started) * 1000
                    previous = row["result"] or {}
                    metrics = {
                        "active_latency_ms": round(
                            previous.get("metrics", {}).get("active_latency_ms", 0) + latency, 3
                        ),
                        "input_tokens": state.get("usage", {}).get("input_tokens", 0),
                        "output_tokens": state.get("usage", {}).get("output_tokens", 0),
                        "model_calls": state.get("usage", {}).get("model_calls", 0),
                    }
                    metrics["usage_complete"] = state.get("usage", {}).get("usage_complete", True)
                    metrics["estimated_api_cost_usd"] = estimate(self.settings, metrics)
                    state["metrics"] = metrics
                    state["provider"] = self.settings.provider
                    state["model"] = (
                        self.settings.gemini_model if self.settings.provider == "gemini" else None
                    )
                    status = (
                        "awaiting_approval"
                        if snap.interrupts
                        else (
                            "rejected"
                            if state.get("decision", {}).get("decision") == "reject"
                            and state.get("report", {}).get("proposed_action") != "none"
                            else "completed"
                        )
                    )
                    self.store.finish(iid, status, state)
                    self.event(iid, "status", {"status": status})
            except Conflict:
                raise
            except Exception as error:
                self.store.finish(iid, "error", row["result"], type(error).__name__)
                self.event(iid, "error", {"type": type(error).__name__})
            return self.store.get(iid)
