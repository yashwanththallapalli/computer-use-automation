"""The replay result contract.

What a deterministic replay returns to its caller (an AI agent, or the CLI). A
discriminated union on ``status`` with exactly three arms, mirroring the outcome
taxonomy:

* ``SUCCESS``          -- goal reached, checkpoint verified, outputs populated.
* ``BUSINESS_OUTCOME`` -- a legitimate result the caller must handle
                          ("no such member"); *not* an error.
* ``FAILURE``          -- a dead-end, with enough detail to debug: which step,
                          what was expected, what was observed.

This lives in its own module because it is a different concern from the stored
artifact: the artifact describes the flow, this describes one execution of it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StepTrace(_Base):
    """One executed step, for the evidence log and for debugging a failure."""

    step_id: str
    action: str
    description: str
    started_at: datetime
    duration_ms: int
    locator_used: str | None = Field(
        default=None,
        description="Which strategy in the fallback chain resolved, e.g. "
        "'role#0' or 'css#2'. A value other than #0 is a drift signal.",
    )
    recovered_from: str | None = Field(
        default=None, description="Outcome-rule id if a recovery ran here."
    )
    note: str | None = None


class ReplayContext(_Base):
    capability_ref: str = Field(description="e.g. 'open_subaccount@v1'.")
    tenant_id: str | None = None
    inputs_redacted: dict[str, Any] = Field(
        default_factory=dict,
        description="Input params with sensitive values masked.",
    )
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    steps_executed: int = 0
    trace: list[StepTrace] = Field(default_factory=list)
    evidence_dir: str | None = None
    escalations: list[str] = Field(
        default_factory=list,
        description="Ids of any HandoffTicket raised during this run, in "
        "order. Present whether or not resolution let the run recover -- an "
        "empty run either never needed a human or (when escalation is off) "
        "failed fast instead of pausing.",
    )


class SuccessResult(_Base):
    status: Literal[ReplayStatus.SUCCESS] = ReplayStatus.SUCCESS
    context: ReplayContext
    outputs: dict[str, Any] = Field(
        default_factory=dict, description="Declared outputs, typed per the schema."
    )
    checkpoint_description: str


class BusinessOutcomeResult(_Base):
    status: Literal[ReplayStatus.BUSINESS_OUTCOME] = ReplayStatus.BUSINESS_OUTCOME
    context: ReplayContext
    outcome_code: str = Field(description="e.g. 'MEMBER_NOT_FOUND'.")
    message: str
    detected_after_step: str
    escalated: bool = False
    partial_outputs: dict[str, Any] = Field(default_factory=dict)


class FailureResult(_Base):
    status: Literal[ReplayStatus.FAILURE] = ReplayStatus.FAILURE
    context: ReplayContext
    failure_code: str = Field(
        description="'STEP_TIMEOUT' | 'LOCATOR_UNRESOLVED' | 'CHECKPOINT_FAILED' "
        "| 'OUTCOME_HARD_FAILURE' | 'GUARDRAIL_BLOCKED' | 'MAX_STEPS' | "
        "'MISSING_REQUIRED_OUTPUT' | 'ESCALATION_UNRESOLVED' | ..."
    )
    failed_step_id: str | None = None
    step_description: str | None = None
    expected: str = Field(description="The wait/condition/checkpoint that did not hold.")
    observed: str = Field(description="What was actually true: URL, visible text "
                          "snippet, missing selector, ...")
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None
    recoveries_attempted: int = 0


ReplayResult = Annotated[
    Union[SuccessResult, BusinessOutcomeResult, FailureResult],
    Field(discriminator="status"),
]
