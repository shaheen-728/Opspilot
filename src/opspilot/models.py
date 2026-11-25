from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRequest(StrictModel):
    scenario_id: str = Field(pattern=r"^inc-\d{3}$")
    description: str = Field(
        default="Investigate this incident and propose safe next steps.", max_length=2000
    )
    architecture: Literal["supervisor", "single"] = "supervisor"


class ApprovalRequest(StrictModel):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=1000)


class Finding(StrictModel):
    root_cause: Literal[
        "database_lock",
        "upstream_timeout",
        "connection_pool_exhausted",
        "disk_full",
        "bad_deployment",
        "healthy",
        "unknown",
    ]
    summary: str = Field(max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    severity: Literal["low", "medium", "high"]
    next_steps: list[str] = Field(min_length=1, max_length=8)


class Report(Finding):
    runbook_references: list[str]
    proposed_action: Literal["create_ticket", "none"]


class Guidance(StrictModel):
    summary: str = Field(max_length=2000)
    runbook_ids: list[str] = Field(max_length=10)
    next_steps: list[str] = Field(min_length=1, max_length=8)
