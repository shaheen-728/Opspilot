import argparse
import json
import runpy
from pathlib import Path

import pytest

from opspilot.config import Settings
from opspilot.engine import Engine
from opspilot.models import Finding, StartRequest
from opspilot.tools import DATA

ROOT = Path(__file__).resolve().parents[1]
OLD = runpy.run_path(str(ROOT / "scripts" / "eval_resumable.py"))["run"]
NEW = runpy.run_path(str(ROOT / "scripts" / "eval_resumable_v2.py"))["run"]


def options(tmp_path, name="old", **changes):
    values = {
        "dataset": DATA / "eval_scenarios.json",
        "output": tmp_path / f"{name}.json",
        "architecture": "single",
        "limit": 3,
        "max_cases": 3,
        "max_provider_errors": 3,
        "pause": 0,
        "adopt": [],
        "resume_from": None,
    }
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.fixture
def model_fixture(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        provider="gemini",
        gemini_api_key="fake-test-key",
        state_dir=tmp_path / "state",
    )
    calls = []

    def investigator(settings, tools, prompt, execute, include_runbooks=False):
        calls.append(tools.scenario["id"])
        service = tools.scenario["service"]
        ids = []
        ids.append(execute("get_service_health", {"service": service})["evidence_id"])
        ids.append(execute("search_logs", {"service": service})["evidence_id"])
        # Exactly the observed error: omit SQL, but still submit findings.
        if service != "orders":
            ids.append(
                execute("run_sql", {"query": "SELECT status FROM incident_state"})["evidence_id"]
            )
        if include_runbooks:
            execute("search_runbooks", {"query": "database lock"})
        return Finding(
            root_cause="database_lock",
            summary="Blocked transaction.",
            evidence_ids=ids,
            severity="high",
            next_steps=["Review with owner."],
        ), {"model_calls": 1, "input_tokens": 10, "output_tokens": 10}

    monkeypatch.setattr("opspilot.engine.model_investigate", investigator)
    return settings, calls


def test_migration_preserves_completed_and_records_failure_then_continues(tmp_path, model_fixture):
    settings, calls = model_fixture
    assert OLD(options(tmp_path), settings) == 2
    assert calls == ["inc-001", "inc-002"]
    source = tmp_path / "old.progress.json"
    original = source.read_bytes()
    old = json.loads(original)
    first_iid = old["runs"]["inc-001"]["incident_id"]
    failed_iid = old["runs"]["inc-002"]["incident_id"]
    args = options(tmp_path, "new", resume_from=source)
    assert NEW(args, settings) == 0
    # The failed model attempt was not repeated; only the next scenario executes.
    assert calls == ["inc-001", "inc-002", "inc-003"]
    assert source.read_bytes() == original
    final = json.loads(args.output.read_text())
    assert final["resolved_scenarios"] == 3
    assert final["completed_scenarios"] == 2
    assert final["agent_failure_scenarios"] == 1
    assert final["root_cause_accuracy"] == pytest.approx(2 / 3)
    assert final["completed_only_diagnosis_accuracy"] == 1
    assert final["latency_sample_count"] == 2
    assert (
        next(r for r in final["rows"] if r["scenario_id"] == "inc-001")["incident_id"] == first_iid
    )
    failure = next(r for r in final["rows"] if r["scenario_id"] == "inc-002")
    assert failure["incident_id"] == failed_iid
    assert failure["tool_recall"] == 0.75
    assert failure["metrics"] is None
    assert Engine(settings).store.get(failed_iid)["status"] == "error"
    assert NEW(options(tmp_path, "new"), settings) == 0
    assert calls == ["inc-001", "inc-002", "inc-003"]


def test_migration_refuses_changed_runtime(tmp_path, model_fixture):
    settings, calls = model_fixture
    OLD(options(tmp_path), settings)
    settings.max_tool_calls = 5
    with pytest.raises(ValueError, match="Migration refused"):
        NEW(options(tmp_path, "new", resume_from=tmp_path / "old.progress.json"), settings)
    assert calls == ["inc-001", "inc-002"]


def test_unknown_validation_error_is_not_assumed_agent_failure(tmp_path):
    settings = Settings(_env_file=None, state_dir=tmp_path / "state")
    e = Engine(settings)
    iid, _ = e.create(StartRequest(scenario_id="inc-001", architecture="single"))
    e.store.finish(iid, "error", error="ValueError")
    assert NEW(options(tmp_path, "new", limit=1, adopt=[f"inc-001={iid}"]), settings) == 2
    partial = json.loads((tmp_path / "new.partial.json").read_text())
    assert partial["resolved_scenarios"] == 0
    assert partial["agent_failure_scenarios"] == 0


def test_provider_outage_remains_pending(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, state_dir=tmp_path / "state")

    def outage(self, iid, decision=None):
        self.store.finish(iid, "error", error="ReadTimeout")
        return self.store.get(iid)

    monkeypatch.setattr(Engine, "run", outage)
    args = options(tmp_path, "new", limit=1)
    assert NEW(args, settings) == 2
    partial = json.loads((tmp_path / "new.partial.json").read_text())
    assert partial["resolved_scenarios"] == 0
    assert partial["root_cause_accuracy"] is None
