"""Human escalation tickets: the file-based hand-off between a paused replay
run and a separate operator process.

A ticket (evidence/escalations/<id>.json) carries everything a human needs to
act -- capability/goal, step, why it stopped, a screenshot, and a CDP endpoint
pointing at the same live browser page replay is paused on. Replay polls the
file for `status != "open"`; the operator process loads it, acts on the live
page, and writes the resolution back. No socket, no server.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EscalationReason = Literal["approval_required", "stuck", "review"]
Resolution = Literal["approved", "recovered", "rejected"]

TICKETS_DIR = Path("evidence/escalations")


class HandoffTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_id: str
    capability_ref: str
    goal: str
    reason: EscalationReason
    trigger: str = Field(description="failure_code / outcome_code / 'confirm_gate'")
    step_id: str
    step_description: str = ""
    base_url: str
    cdp_endpoint: str = Field(description="http://host:port -- connect_over_cdp target")
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None
    inputs_redacted: dict = Field(default_factory=dict)

    status: Literal["open", "resolved", "expired"] = "open"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolution: Resolution | None = None
    operator_notes: str = ""
    operator_actions: list[str] = Field(default_factory=list)

    def path(self) -> Path:
        return TICKETS_DIR / f"{self.ticket_id}.json"

    def save(self) -> Path:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return p


def create_ticket(**fields) -> HandoffTicket:
    ticket = HandoffTicket(**fields)
    ticket.save()
    return ticket


def load_ticket(ticket_id: str) -> HandoffTicket:
    p = TICKETS_DIR / f"{ticket_id}.json"
    return HandoffTicket.model_validate_json(p.read_text(encoding="utf-8"))


def resolve_ticket(ticket_id: str, resolution: Resolution, *,
                   notes: str = "", actions: list[str] | None = None) -> HandoffTicket:
    """Called by the operator process once they're done acting on the live
    page. This is the signal replay's wait loop is polling for."""
    ticket = load_ticket(ticket_id)
    ticket.status = "resolved"
    ticket.resolution = resolution
    ticket.resolved_at = datetime.now(timezone.utc)
    ticket.operator_notes = notes
    ticket.operator_actions = actions or []
    ticket.save()
    return ticket


def wait_for_resolution(ticket_id: str, *, timeout_s: float = 120.0,
                        poll_s: float = 0.5) -> HandoffTicket | None:
    """Block until the ticket is resolved or the timeout elapses. Returns the
    resolved ticket, or None on timeout (the caller marks it "expired")."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ticket = load_ticket(ticket_id)
        if ticket.status != "open":
            return ticket
        time.sleep(poll_s)
    return None


def expire_ticket(ticket_id: str) -> None:
    ticket = load_ticket(ticket_id)
    if ticket.status == "open":
        ticket.status = "expired"
        ticket.save()
