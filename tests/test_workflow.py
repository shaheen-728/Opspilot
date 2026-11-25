import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from opspilot.api import create_app
from opspilot.config import Settings
from opspilot.engine import Engine
from opspilot.models import StartRequest
from opspilot.store import Conflict
from opspilot.tools import DATA, Tools, load_scenario, safe_sql


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, state_dir=tmp_path, provider="offline")


def test_pause_restart_approve_and_idempotency(settings):
    engine = Engine(settings)
    request = StartRequest(scenario_id="inc-001")
    iid, created = engine.create(request, "same-request")
    assert created
    assert engine.create(request, "same-request") == (iid, False)
    with pytest.raises(Conflict):
        engine.create(StartRequest(scenario_id="inc-002"), "same-request")
    row = engine.run(iid)
    assert row["status"] == "awaiting_approval"
    assert "ticket" not in row["result"]
    # A new engine/checkpointer proves process-independent persistence.
    recovered = Engine(settings)
    decision = {"decision": "approve", "note": "Reviewed"}
    row = recovered.run(iid, decision)
    assert row["status"] == "completed"
    assert row["result"]["ticket"]["id"].startswith("MOCK-")
    assert recovered.run(iid, decision)["result"]["ticket"] == row["result"]["ticket"]
    with recovered.store.connect() as c:
        assert c.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    with pytest.raises(Conflict):
        recovered.run(iid, {"decision": "reject", "note": "Changed"})


def test_reject_has_no_ticket(settings):
    e = Engine(settings)
    iid, _ = e.create(StartRequest(scenario_id="inc-006"))
    e.run(iid)
    row = e.run(iid, {"decision": "reject", "note": "No change"})
    assert row["status"] == "rejected"
    assert row["result"]["ticket"] is None
    with e.store.connect() as c:
        assert c.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0


def test_healthy_needs_no_write(settings):
    e = Engine(settings)
    iid, _ = e.create(StartRequest(scenario_id="inc-026"))
    row = e.run(iid)
    assert row["status"] == "completed"
    assert row["result"]["report"]["root_cause"] == "healthy"
    assert row["result"]["report"]["proposed_action"] == "none"
    assert row["result"]["ticket"] is None


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM incident_state",
        "SELECT * FROM incident_state; DROP TABLE incident_state",
        "SELECT * FROM sqlite_master",
        "SELECT load_extension('evil') FROM incident_state",
        "WITH a AS (SELECT * FROM incident_state) SELECT * FROM a",
        "SELECT * FROM incident_state JOIN incident_state AS b ON 1=1",
        "PRAGMA writable_schema=ON",
        "SELECT * FROM incident_state UNION SELECT * FROM incident_state",
        "SELECT * FROM (SELECT * FROM incident_state)",
        "SELECT * FROM main.incident_state",
    ],
)
def test_sql_blocks_unsafe(settings, query):
    tools = Tools(settings, load_scenario("inc-001"), "sqltest")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        safe_sql(tools.db_path, query)
    assert safe_sql(tools.db_path, "SELECT status FROM incident_state")[0]["status"] == "blocked"


def test_tool_validation_and_scope(settings):
    t = Tools(settings, load_scenario("inc-001"), "tooltest")
    with pytest.raises(ValueError):
        t.call("get_service_health", {"service": "another-company"})
    with pytest.raises(ValueError):
        t.call("restart_service", {})
    with pytest.raises(ValueError):
        t.call("search_logs", {"service": "payments", "unexpected": True})
    assert t.call("search_logs", {"service": "payments", "level": "ERROR"})
    assert not t.call("search_logs", {"service": "payments", "since": "2027-01-01T00:00:00Z"})


def test_api_auth_sse_and_bad_approval(settings):
    settings.api_key = "local-test-key"
    with TestClient(create_app(settings)) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/scenarios").status_code == 401
        headers = {"X-API-Key": "local-test-key", "Idempotency-Key": "api-request"}
        assert (
            c.post("/incidents", json={"scenario_id": "inc-999"}, headers=headers).status_code
            == 400
        )
        r = c.post("/incidents", json={"scenario_id": "inc-001"}, headers=headers)
        assert r.status_code == 202
        iid = r.json()["id"]
        for _ in range(200):
            row = c.get(f"/incidents/{iid}", headers=headers).json()
            if row["status"] != "running":
                break
            time.sleep(0.01)
        assert row["status"] == "awaiting_approval", row
        stream = c.get(f"/incidents/{iid}/events", headers=headers)
        assert "event: tool_result" in stream.text
        assert "event: status" in stream.text
        approved = c.post(
            f"/incidents/{iid}/approve",
            headers=headers,
            json={"decision": "approve", "note": "test"},
        )
        assert approved.status_code == 200
        assert approved.json()["result"]["ticket"]
        assert c.get("/").status_code == 200


def test_all_fixture_causes_in_both_architectures(settings):
    labels = json.loads((DATA.parents[0] / "data" / "eval_scenarios.json").read_text())
    # Use repository dataset; labels are never passed to engine.
    e = Engine(settings)
    for architecture in ["single", "supervisor"]:
        for label in labels[::5]:
            iid, _ = e.create(StartRequest(scenario_id=label["id"], architecture=architecture))
            row = e.run(iid)
            assert row["result"]["report"]["root_cause"] == label["expected_root_cause"], row
            if row["status"] == "awaiting_approval":
                e.run(iid, {"decision": "reject", "note": "test"})


def test_ticket_replay_after_commit_before_checkpoint(settings, monkeypatch):
    e = Engine(settings)
    iid, _ = e.create(StartRequest(scenario_id="inc-001"))
    e.run(iid)
    original = e.store.ticket

    def commit_then_crash(*args):
        original(*args)
        raise RuntimeError("Simulated crash after database commit")

    monkeypatch.setattr(e.store, "ticket", commit_then_crash)
    failed = e.run(iid, {"decision": "approve", "note": "Approved"})
    assert failed["status"] == "error"
    recovered = Engine(settings)
    final = recovered.run(iid)
    assert final["status"] == "completed", final
    with recovered.store.connect() as c:
        assert c.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


def test_error_can_resume_from_last_checkpoint(settings, monkeypatch):
    e = Engine(settings)
    iid, _ = e.create(StartRequest(scenario_id="inc-001"))
    real = Tools.call
    attempts = []

    def fail_sql_once(self, name, args):
        if name == "run_sql" and not attempts:
            attempts.append(True)
            raise RuntimeError("Synthetic temporary tool failure")
        return real(self, name, args)

    monkeypatch.setattr(Tools, "call", fail_sql_once)
    assert e.run(iid)["status"] == "error"
    row = e.run(iid)
    assert row["status"] == "awaiting_approval", row
