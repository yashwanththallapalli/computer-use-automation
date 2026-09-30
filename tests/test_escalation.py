"""Human escalation tests.

The ticket round-trip is tested directly. The two blocking paths (approval
gate, stuck) are tested end-to-end: replay runs in a background thread inside
this process, paused and polling its ticket file; scripts/operator.py runs as
a genuinely separate OS process and resolves it by connecting to the same live
browser over CDP. That's the real mechanism, not a stand-in for it -- a
production operator console would be a different service making the exact
same two calls (connect_over_cdp, then write the resolution).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cua._appserver import running_bankapp
from cua.escalation import (
    create_ticket,
    expire_ticket,
    load_ticket,
    resolve_ticket,
    wait_for_resolution,
)
from cua.replay import ReplayOptions, replay
from cua.schema import CapabilityArtifact

CAP_DIR = Path(__file__).parent.parent / "cua" / "capabilities"


def _ticket_kwargs(**over):
    base = dict(
        run_id="r1", capability_ref="open_subaccount@v1", goal="test goal",
        reason="stuck", trigger="LOCATOR_UNRESOLVED", step_id="s1",
        base_url="http://127.0.0.1:5001", cdp_endpoint="http://127.0.0.1:9222",
    )
    base.update(over)
    return base


# --- ticket round-trip (no browser) ----------------------------------

def test_create_load_resolve_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("cua.escalation.TICKETS_DIR", tmp_path)
    ticket = create_ticket(**_ticket_kwargs())
    assert ticket.status == "open"

    loaded = load_ticket(ticket.ticket_id)
    assert loaded.reason == "stuck"

    resolved = resolve_ticket(ticket.ticket_id, "recovered",
                              notes="cleared a popup", actions=["clicked X"])
    assert resolved.status == "resolved"
    assert resolved.resolution == "recovered"
    assert resolved.operator_actions == ["clicked X"]
    assert load_ticket(ticket.ticket_id).status == "resolved"


def test_wait_for_resolution_returns_once_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr("cua.escalation.TICKETS_DIR", tmp_path)
    ticket = create_ticket(**_ticket_kwargs())

    def _resolve_soon():
        time.sleep(0.3)
        resolve_ticket(ticket.ticket_id, "approved")

    threading.Thread(target=_resolve_soon, daemon=True).start()
    resolved = wait_for_resolution(ticket.ticket_id, timeout_s=5, poll_s=0.05)
    assert resolved is not None and resolved.resolution == "approved"


def test_wait_for_resolution_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr("cua.escalation.TICKETS_DIR", tmp_path)
    ticket = create_ticket(**_ticket_kwargs())
    result = wait_for_resolution(ticket.ticket_id, timeout_s=0.3, poll_s=0.05)
    assert result is None
    expire_ticket(ticket.ticket_id)
    assert load_ticket(ticket.ticket_id).status == "expired"


# --- non-blocking "review" path (business outcome, escalate=True) ------

@pytest.fixture(scope="module")
def app_base():
    with running_bankapp() as base:
        yield base


def test_business_outcome_escalation_is_non_blocking(app_base, tmp_path):
    art = CapabilityArtifact.load(CAP_DIR / "open_subaccount.v1.json")
    result = replay(
        art, {"member_id": "100987", "account_type": "Savings",
             "initial_deposit": "250.00", "member_authorized": "yes"},
        ReplayOptions(approve_risky=True, escalate=True, evidence_root=tmp_path),
    )
    assert result.status.value == "business_outcome"
    assert result.outcome_code == "MEMBER_NOT_ELIGIBLE"
    assert result.escalated is True
    assert len(result.context.escalations) == 1  # a ticket was raised...
    # ...but replay did not wait on it
    ticket_id = result.context.escalations[0]
    ticket_path = Path("evidence/escalations") / f"{ticket_id}.json"
    assert ticket_path.exists()
    assert '"status": "open"' in ticket_path.read_text(encoding="utf-8")
    ticket_path.unlink()


def test_escalate_false_is_unchanged_fail_fast(app_base, tmp_path):
    art = CapabilityArtifact.load(CAP_DIR / "open_subaccount.v1.json")
    result = replay(
        art, {"member_id": "101445", "account_type": "Savings",
             "initial_deposit": "250.00", "member_authorized": "yes"},
        ReplayOptions(approve_risky=False, escalate=False, evidence_root=tmp_path),
    )
    assert result.status.value == "failure"
    assert result.failure_code == "APPROVAL_REQUIRED"
    assert result.context.escalations == []


# --- true end-to-end: a second, separate OS process resolves the ticket --

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_operator(ticket_id: str, *extra_args: str, timeout: int = 30) -> str:
    proc = subprocess.run(
        [sys.executable, "scripts/operator.py", ticket_id, *extra_args],
        capture_output=True, text=True, timeout=timeout, cwd=_REPO_ROOT,
    )
    return proc.stdout + proc.stderr


def _wait_for_ticket(escalations: list, timeout_s: float = 10) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if escalations:
            return escalations[0]
        time.sleep(0.05)
    raise TimeoutError("no ticket was raised in time")


def test_end_to_end_approval_gate_resolved_by_separate_process(app_base, tmp_path):
    art = CapabilityArtifact.load(CAP_DIR / "open_subaccount.v1.json")
    seen_tickets: list[str] = []
    result_box: dict = {}

    def _run():
        result_box["result"] = replay(
            art, {"member_id": "101445", "account_type": "Savings",
                 "initial_deposit": "250.00", "member_authorized": "yes"},
            ReplayOptions(escalate=True, evidence_root=tmp_path,
                         on_escalate=lambda t: seen_tickets.append(t.ticket_id)),
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ticket_id = _wait_for_ticket(seen_tickets)

    output = _run_operator(ticket_id)
    assert "resolved" in output and "approved" in output

    t.join(timeout=30)
    result = result_box["result"]
    assert result.status.value == "success"
    assert result.outputs["sub_account_number"].startswith("101445-")


def test_end_to_end_stuck_recovered_by_separate_process(app_base, tmp_path):
    with running_bankapp(base_url="http://127.0.0.1:5098",
                         env_extra={"BANKAPP_UNEXPECTED_NOTICE": "1"}):
        art = CapabilityArtifact.load(CAP_DIR / "read_member_balance.v1.json")
        art = art.model_copy(deep=True)
        art.target.base_url = "http://127.0.0.1:5098"
        art.policy.allowed_url_patterns = [r"^http://127\.0\.0\.1:5098/members(/|$|\?)"]

        seen_tickets: list[str] = []
        result_box: dict = {}

        def _run():
            result_box["result"] = replay(
                art, {"member_id": "100234", "account_type": "Savings"},
                ReplayOptions(escalate=True, evidence_root=tmp_path,
                             on_escalate=lambda t: seen_tickets.append(t.ticket_id)),
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        ticket_id = _wait_for_ticket(seen_tickets)

        output = _run_operator(ticket_id, "--dismiss-notice")
        assert "dismissed notice" in output
        assert "resolved" in output and "recovered" in output

        t.join(timeout=30)
        result = result_box["result"]
        assert result.status.value == "success"
        assert result.outputs["balance"] == "4210.55"
