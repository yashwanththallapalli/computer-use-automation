"""Typed record of a discovery run.

``DiscoveryTranscript`` is the bridge between the LLM loop and Phase 4's
artifact synthesis. It is *not* the artifact: it keeps everything the model saw
and did, including per-acted-element metadata (``CapturedElement``) rich enough
to synthesise a locator fallback chain, plus the raw Anthropic message list
(``raw_messages``) so the artifact stays decoupled from the transcript while
still pointing back to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class CapturedElement(BaseModel):
    """Everything known about the element a step acted on -- the raw material
    Phase 4 turns into an ordered ``Locator`` strategy chain."""

    ref: int
    role: str
    name: str = ""
    near: str = ""
    column: str = ""
    tag: str = ""
    type: str | None = None
    name_attr: str | None = None
    id: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    href: str | None = None
    options: list[str] | None = None
    css_path: str = ""
    row_text: str | None = None


class DiscoveryStep(BaseModel):
    index: int
    url: str
    observation: str = Field(description="Redacted text observation shown this "
                             "turn (persisted copy).")
    thinking: str = Field(default="", description="Summarised model reasoning.")
    assistant_text: str = ""
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    guardrail_allowed: bool = True
    guardrail_reason: str = ""
    ok: bool = True
    result: str = ""
    captured_element: CapturedElement | None = None
    extracted: dict[str, str] = Field(default_factory=dict)
    screenshot: str | None = Field(default=None, description="Path relative to "
                                   "the evidence root.")


class DiscoveryTranscript(BaseModel):
    run_id: str
    goal: str
    goal_params: dict[str, Any] = Field(
        default_factory=dict,
        description="The concrete values used this run; Phase 4 matches these "
        "back to steps to parameterise the artifact.",
    )
    target_app_id: str
    target_app_version: str | None = None
    base_url: str
    entry_path: str
    model: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    final_status: Literal[
        "goal_reached", "business_outcome", "stuck", "max_steps", "error"
    ] = "error"
    final_summary: str = ""
    final_outputs: dict[str, Any] = Field(default_factory=dict)
    final_url: str = ""
    final_headings: list[str] = Field(default_factory=list)
    final_banners: list[str] = Field(default_factory=list)
    steps: list[DiscoveryStep] = Field(default_factory=list)
    raw_messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The full Anthropic messages array (redacted). The artifact "
        "points to this file but does not embed it.",
    )
    token_usage: dict[str, int] = Field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.final_status == "goal_reached"
