from types import SimpleNamespace

from google import genai
from google.genai import types

from opspilot.config import Settings
from opspilot.engine import Engine
from opspilot.models import StartRequest
from opspilot.providers import estimate
from opspilot.tools import Tools, load_scenario


def test_native_tool_loop_without_network(tmp_path, monkeypatch):
    requests = []
    closed = []
    response_number = 0

    def generate_content(**kwargs):
        nonlocal response_number
        requests.append(kwargs)
        response_number += 1
        if response_number == 1:
            calls = [
                types.FunctionCall(
                    name="get_service_health", args={"service": "payments"}, id="c1"
                ),
                types.FunctionCall(name="search_logs", args={"service": "payments"}, id="c2"),
                types.FunctionCall(
                    name="run_sql", args={"query": "SELECT status FROM incident_state"}, id="c3"
                ),
            ]
        else:
            # Verify the SDK continuation retained the original model content and call IDs.
            previous = kwargs["contents"][-1]
            assert [p.function_response.id for p in previous.parts] == ["c1", "c2", "c3"]
            calls = [
                types.FunctionCall(
                    name="submit_findings",
                    args={
                        "root_cause": "database_lock",
                        "summary": "Blocked transaction observed.",
                        "evidence_ids": ["ev-1", "ev-2", "ev-3"],
                        "severity": "high",
                        "next_steps": ["Ask database owner to inspect blocking transaction."],
                    },
                )
            ]
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=types.Content(
                        role="model", parts=[types.Part(function_call=c) for c in calls]
                    )
                )
            ],
            usage_metadata=SimpleNamespace(
                prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5
            ),
        )

    monkeypatch.setattr(
        genai,
        "Client",
        lambda **kwargs: SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content),
            close=lambda: closed.append(True),
        ),
    )
    settings = Settings(
        _env_file=None,
        provider="gemini",
        gemini_api_key="fake-not-a-secret",
        state_dir=tmp_path,
        input_usd_per_million=1,
        output_usd_per_million=2,
    )
    engine = Engine(settings)
    iid, _ = engine.create(StartRequest(scenario_id="inc-001", architecture="single"))
    row = engine.run(iid)
    assert row["status"] == "awaiting_approval", row
    assert row["result"]["report"]["root_cause"] == "database_lock"
    metrics = row["result"]["metrics"]
    assert metrics["input_tokens"] == 200
    assert metrics["output_tokens"] == 50
    assert metrics["estimated_api_cost_usd"] == 0.0003
    assert closed == [True]
    assert len(requests) == 2


def test_provider_failure_never_falls_back_to_offline(tmp_path, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(
        genai,
        "Client",
        lambda **kwargs: SimpleNamespace(
            models=SimpleNamespace(generate_content=fail), close=lambda: None
        ),
    )
    e = Engine(
        Settings(_env_file=None, provider="gemini", gemini_api_key="fake", state_dir=tmp_path)
    )
    iid, _ = e.create(StartRequest(scenario_id="inc-001"))
    row = e.run(iid)
    assert row["status"] == "error"
    assert row["result"] is None
    assert row["error"] == "RuntimeError"


def test_unknown_pricing_stays_unknown(tmp_path):
    s = Settings(_env_file=None, provider="gemini", gemini_api_key="fake", state_dir=tmp_path)
    assert estimate(s, {"input_tokens": 100, "output_tokens": 10}) is None


def test_optional_docurag_tool(tmp_path, monkeypatch):
    import httpx

    calls = []
    for name in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        monkeypatch.delenv(name, raising=False)

    def post(self, url, json):
        calls.append((url, json))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "results": [{"chunk_id": "rb1", "filename": "ops.md", "content": "Runbook"}]
            },
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    s = Settings(_env_file=None, state_dir=tmp_path, docurag_url="http://localhost:8000")
    s.prepare()
    tool = Tools(s, load_scenario("inc-001"), "external")
    assert tool.call("search_runbooks", {"query": "lock"})[0]["id"] == "rb1"
    assert calls[0][0] == "http://localhost:8000/search"


def test_runtime_change_rejected(tmp_path):
    import pytest

    from opspilot.store import Conflict

    e = Engine(Settings(_env_file=None, state_dir=tmp_path))
    iid, _ = e.create(StartRequest(scenario_id="inc-001"))
    e.run(iid)
    e.settings.max_tool_calls = 5
    with pytest.raises(Conflict):
        e.run(iid, {"decision": "approve", "note": "Changed runtime"})


def test_specialist_agents_and_usage(tmp_path, monkeypatch):
    from opspilot.models import Guidance, Report

    roles = []

    def specialist(settings, role, schema, payload):
        roles.append(role)
        if role == "knowledge":
            assert payload["runbooks"]
            return Guidance(
                summary="Inspect the blocking transaction.",
                runbook_ids=[payload["runbooks"][0]["id"]],
                next_steps=["Ask the database owner to inspect the lock."],
            ), {"input_tokens": 30, "output_tokens": 10, "model_calls": 1}
        f = payload["finding"]
        return Report(
            **f,
            runbook_references=payload["guidance"]["runbook_ids"],
            proposed_action="create_ticket",
        ), {"input_tokens": 40, "output_tokens": 20, "model_calls": 1}

    # Run real read-tools in place of just returning a canned report.
    def investigate(settings, tools, prompt, execute, include_runbooks=False):
        from opspilot.models import Finding

        execute("get_service_health", {"service": "payments"})
        execute("search_logs", {"service": "payments"})
        execute("run_sql", {"query": "SELECT status FROM incident_state"})
        return Finding(
            root_cause="database_lock",
            summary="Blocking transaction observed.",
            evidence_ids=["ev-3"],
            severity="high",
            next_steps=["Inspect lock."],
        ), {"input_tokens": 100, "output_tokens": 20, "model_calls": 2}

    monkeypatch.setattr("opspilot.engine.model_investigate", investigate)
    monkeypatch.setattr("opspilot.engine.model_structured", specialist)
    e = Engine(
        Settings(_env_file=None, provider="gemini", gemini_api_key="fake", state_dir=tmp_path)
    )
    iid, _ = e.create(StartRequest(scenario_id="inc-001"))
    row = e.run(iid)
    assert row["status"] == "awaiting_approval", row
    assert roles == ["knowledge", "reporter"]
    assert row["result"]["metrics"]["input_tokens"] == 170
    assert row["result"]["metrics"]["output_tokens"] == 50
    assert row["result"]["metrics"]["model_calls"] == 4


def test_native_structured_sdk_shape(tmp_path, monkeypatch):
    from opspilot.models import Guidance
    from opspilot.providers import model_structured

    captured = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def generate_content(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                text='{"summary":"Review lock","runbook_ids":["rb-1"],"next_steps":["Inspect"]}',
                usage_metadata=SimpleNamespace(
                    prompt_token_count=12, candidates_token_count=8, thoughts_token_count=2
                ),
            )

    monkeypatch.setattr(genai, "Client", FakeClient)
    s = Settings(_env_file=None, provider="gemini", gemini_api_key="fake", state_dir=tmp_path)
    result, usage = model_structured(s, "knowledge", Guidance, {"runbooks": []})
    assert result.runbook_ids == ["rb-1"]
    assert captured[0]["config"].response_mime_type == "application/json"
    assert usage["output_tokens"] == 10


def test_langfuse_uses_settings_without_export(tmp_path, monkeypatch):
    import sys
    from contextlib import contextmanager

    captured = []

    class Tracer:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        @contextmanager
        def start_as_current_observation(self, **kwargs):
            captured.append(kwargs)
            yield

        def flush(self):
            captured.append("flushed")

    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=Tracer))
    s = Settings(
        _env_file=None,
        state_dir=tmp_path,
        langfuse_enabled=True,
        langfuse_public_key="fake-public",
        langfuse_secret_key="fake-private",
        langfuse_base_url="https://example.invalid",
    )
    e = Engine(s)
    iid, _ = e.create(StartRequest(scenario_id="inc-026"))
    assert e.run(iid)["status"] == "completed"
    e.close()
    assert captured[0]["secret_key"] == "fake-private"
    assert captured[1]["metadata"] == {"incident_id": iid}
    assert captured[-1] == "flushed"
