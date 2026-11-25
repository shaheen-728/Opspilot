import argparse
import json
import runpy
from pathlib import Path

import pytest

from opspilot.config import Settings
from opspilot.engine import Engine
from opspilot.models import StartRequest
from opspilot.tools import DATA

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_resumable.py"
RUN = runpy.run_path(str(SCRIPT))["run"]


def args(tmp_path, **changes):
    options = dict(
        dataset=DATA / "eval_scenarios.json",
        output=tmp_path / "report.json",
        architecture="supervisor",
        limit=2,
        max_cases=2,
        max_provider_errors=3,
        pause=0,
        adopt=[],
    )
    options.update(changes)
    return argparse.Namespace(**options)


def settings(tmp_path):
    return Settings(_env_file=None, provider="offline", state_dir=tmp_path / "state")


def test_completed_results_reused_without_engine_calls(tmp_path, monkeypatch):
    s = settings(tmp_path)
    a = args(tmp_path)
    assert RUN(a, s) == 0
    first = json.loads(a.output.read_text())
    assert first["completed_scenarios"] == 2

    def unexpected(*args, **kwargs):
        raise AssertionError("Completed incident must not execute again")

    monkeypatch.setattr(Engine, "run", unexpected)
    assert RUN(a, s) == 0
    second = json.loads(a.output.read_text())
    assert [r["incident_id"] for r in first["rows"]] == [r["incident_id"] for r in second["rows"]]


def test_transport_deferred_then_same_incident_resumes(tmp_path, monkeypatch):
    s = settings(tmp_path)
    a = args(tmp_path, max_provider_errors=1)
    real = Engine.run

    def unavailable(self, iid, decision=None):
        self.store.finish(iid, "error", error="ReadTimeout")
        return self.store.get(iid)

    monkeypatch.setattr(Engine, "run", unavailable)
    assert RUN(a, s) == 2
    partial = json.loads((tmp_path / "report.partial.json").read_text())
    assert partial["completed_scenarios"] == 0
    assert partial["root_cause_accuracy"] is None
    state = json.loads((tmp_path / "report.progress.json").read_text())
    iid = state["runs"]["inc-001"]["incident_id"]
    monkeypatch.setattr(Engine, "run", real)
    assert RUN(a, s) == 0
    final = json.loads(a.output.read_text())
    assert final["rows"][0]["incident_id"] == iid
    assert len(final["attempt_history"]["inc-001"]) == 2
    e = Engine(s)
    with e.store.connect() as c:
        assert c.execute("SELECT count(*) FROM incidents").fetchone()[0] == 2


def test_adopts_existing_paused_incidents(tmp_path):
    s = settings(tmp_path)
    e = Engine(s)
    adopted = []
    for sid in ["inc-001", "inc-002"]:
        iid, _ = e.create(StartRequest(scenario_id=sid))
        assert e.run(iid)["status"] == "awaiting_approval"
        adopted.append(f"{sid}={iid}")
    assert RUN(args(tmp_path, adopt=adopted), s) == 0
    with e.store.connect() as c:
        assert c.execute("SELECT count(*) FROM incidents").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM tickets").fetchone()[0] == 2


def test_interrupt_saves_id_and_resumes(tmp_path, monkeypatch):
    s = settings(tmp_path)
    a = args(tmp_path)
    real = Engine.run

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Engine, "run", interrupt)
    assert RUN(a, s) == 130
    state = json.loads((tmp_path / "report.progress.json").read_text())
    saved_id = state["runs"]["inc-001"]["incident_id"]
    assert state["runs"]["inc-001"]["attempts"][-1]["status"] == "interrupted"
    monkeypatch.setattr(Engine, "run", real)
    assert RUN(a, s) == 0
    final = json.loads(a.output.read_text())
    assert final["rows"][0]["incident_id"] == saved_id


def test_runtime_changes_do_not_mix_results(tmp_path):
    s = settings(tmp_path)
    a = args(tmp_path)
    assert RUN(a, s) == 0
    s.max_tool_calls = 5
    with pytest.raises(ValueError, match="settings changed"):
        RUN(a, s)


def test_reject_mismatched_adoption(tmp_path):
    s = settings(tmp_path)
    e = Engine(s)
    iid, _ = e.create(StartRequest(scenario_id="inc-002"))
    with pytest.raises(ValueError, match="scenario or architecture"):
        RUN(args(tmp_path, adopt=[f"inc-001={iid}"]), s)


def test_limits_only_new_work_not_sample_identity(tmp_path):
    s = settings(tmp_path)
    a = args(tmp_path, max_cases=1)
    assert RUN(a, s) == 2
    partial = json.loads((tmp_path / "report.partial.json").read_text())
    assert partial["completed_scenarios"] == 1
    assert partial["provisional"] is True
    assert RUN(args(tmp_path, max_cases=2), s) == 0
