"""Optional real PostgreSQL checkpointer test; never falls back to SQLite."""

import os
from uuid import uuid4

import pytest

from opspilot.config import Settings
from opspilot.engine import Engine
from opspilot.models import StartRequest


@pytest.mark.skipif(
    not os.environ.get("OPSPILOT_TEST_POSTGRES"), reason="Postgres URL not supplied"
)
def test_postgres_resume(tmp_path):
    s = Settings(
        _env_file=None, state_dir=tmp_path, checkpoint_url=os.environ["OPSPILOT_TEST_POSTGRES"]
    )
    e = Engine(s)
    iid, _ = e.create(StartRequest(scenario_id="inc-001"), str(uuid4()))
    assert e.run(iid)["status"] == "awaiting_approval"
    recovered = Engine(s)
    assert (
        recovered.run(iid, {"decision": "approve", "note": "Integration test"})["status"]
        == "completed"
    )
