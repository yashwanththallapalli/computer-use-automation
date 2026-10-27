"""Replay engine tests: no LLM, drives the real mock app."""

import json
from pathlib import Path

import pytest

from cua._appserver import running_bankapp
from cua.replay import ReplayOptions, replay
from cua.schema import CapabilityArtifact

CAP_DIR = Path(__file__).parent.parent / "cua" / "capabilities"


@pytest.fixture(scope="module")
def app_base():
    with running_bankapp() as base:
        yield base


@pytest.fixture
def open_subaccount(app_base):
    return CapabilityArtifact.load(CAP_DIR / "open_subaccount.v1.json")


@pytest.fixture
def read_balance(app_base):
    return CapabilityArtifact.load(CAP_DIR / "read_member_balance.v1.json")


def test_open_subaccount_happy_path(open_subaccount, tmp_path):
    result = replay(
        open_subaccount,
        {"member_id": "101445", "account_type": "Savings",
         "initial_deposit": "250.00", "member_authorized": "yes"},
        ReplayOptions(approve_risky=True, evidence_root=tmp_path),
    )
    assert result.status.value == "success"
    assert result.outputs["sub_account_number"] == "101445-01"
    # every step resolved on its primary (highest-confidence) locator strategy
    assert all(t.locator_used is None or t.locator_used.endswith("#0")
              for t in result.context.trace)


def test_open_subaccount_without_approval_stops_at_confirm(open_subaccount, tmp_path):
    result = replay(
        open_subaccount,
        {"member_id": "101445", "account_type": "Savings",
         "initial_deposit": "250.00", "member_authorized": "yes"},
        ReplayOptions(approve_risky=False, evidence_root=tmp_path),
    )
    assert result.status.value == "failure"
    assert result.failure_code == "APPROVAL_REQUIRED"
    assert "confirm" in (result.failed_step_id or "")


def test_open_subaccount_member_not_found(open_subaccount, tmp_path):
    result = replay(
        open_subaccount,
        {"member_id": "999999", "account_type": "Savings",
         "initial_deposit": "250.00", "member_authorized": "yes"},
        ReplayOptions(approve_risky=True, evidence_root=tmp_path),
    )
    assert result.status.value == "business_outcome"
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert "999999" in result.message
    assert result.escalated is False


def test_open_subaccount_dormant_member_escalates(open_subaccount, tmp_path):
    result = replay(
        open_subaccount,
        {"member_id": "100987", "account_type": "Savings",
         "initial_deposit": "250.00", "member_authorized": "yes"},
        ReplayOptions(approve_risky=True, evidence_root=tmp_path),
    )
    assert result.status.value == "business_outcome"
    assert result.outcome_code == "MEMBER_NOT_ELIGIBLE"
    assert result.escalated is True


def test_open_subaccount_invalid_input_never_opens_a_browser(open_subaccount, tmp_path):
    result = replay(
        open_subaccount,
        {"member_id": "101445", "account_type": "Savings",
         "initial_deposit": "5.00", "member_authorized": "yes"},
        ReplayOptions(evidence_root=tmp_path),
    )
    assert result.status.value == "failure"
    assert result.failure_code == "INVALID_INPUT"
    assert "minimum" in result.observed
    assert result.context.steps_executed == 0


def test_read_balance_happy_path(read_balance, tmp_path):
    result = replay(
        read_balance, {"member_id": "100234", "account_type": "Savings"},
        ReplayOptions(evidence_root=tmp_path),
    )
    assert result.status.value == "success"
    # the caller gets the real, sensitive value...
    assert result.outputs["balance"] == "4210.55"
    assert result.outputs["sub_account_number"] == "100234-01"
    # ...but it is never written to disk in the clear
    persisted = Path(result.context.evidence_dir).joinpath("result.json") \
        .read_text(encoding="utf-8")
    assert "4210.55" not in persisted
    assert "[REDACTED]" in persisted
    assert "100234-01" in persisted  # non-sensitive output is untouched


def test_read_balance_type_not_found(read_balance, tmp_path):
    result = replay(
        read_balance, {"member_id": "100987", "account_type": "Savings"},
        ReplayOptions(evidence_root=tmp_path),
    )
    assert result.status.value == "business_outcome"
    assert result.outcome_code == "SUBACCOUNT_TYPE_NOT_FOUND"


def test_read_balance_recovers_from_interstitial(read_balance, tmp_path):
    # a different port: app_base (module-scoped) is already running on 5001
    with running_bankapp(base_url="http://127.0.0.1:5099",
                         env_extra={"BANKAPP_INTERSTITIAL": "1"}) as base:
        art = read_balance.model_copy(deep=True)
        art.target.base_url = base
        # the allowlist is anchored to the artifact's original port; a real
        # move to a new host needs its own entry too, same as a tenant overlay
        art.policy.allowed_url_patterns = [r"^http://127\.0\.0\.1:5099/members(/|$|\?)"]
        result = replay(
            art, {"member_id": "100234", "account_type": "Savings"},
            ReplayOptions(evidence_root=tmp_path),
        )
    assert result.status.value == "success"
    assert result.outputs["balance"] == "4210.55"
    # the interstitial gates member detail, so the recovery lands on click_open
    recovered = {t.step_id: t.recovered_from for t in result.context.trace}
    assert recovered["click_open"] == "maintenance_interstitial"
    assert [s for s, r in recovered.items() if r] == ["click_open"]
    steps = [json.loads(line) for line in
             (Path(result.context.evidence_dir) / "steps.jsonl").read_text().splitlines()]
    logged = {s["step_id"]: s["recovered_from"] for s in steps if "step_id" in s}
    assert logged["click_open"] == "maintenance_interstitial"


def test_unresolvable_recovery_target_fails_loudly(read_balance, tmp_path):
    with running_bankapp(base_url="http://127.0.0.1:5098",
                         env_extra={"BANKAPP_INTERSTITIAL": "1"}) as base:
        art = read_balance.model_copy(deep=True)
        art.target.base_url = base
        art.policy.allowed_url_patterns = [r"^http://127\.0\.0\.1:5098/members(/|$|\?)"]
        # the vendor renamed the link: no strategy can find it any more
        rule = next(r for r in art.outcome_taxonomy if r.id == "maintenance_interstitial")
        rule.recovery_target.strategies[0].options = {"name": "Proceed"}
        rule.recovery_target.strategies[1].value = "Proceed"
        result = replay(
            art, {"member_id": "100234", "account_type": "Savings"},
            ReplayOptions(evidence_root=tmp_path),
        )
    assert result.status.value == "failure"
    assert result.failure_code == "RECOVERY_TARGET_UNRESOLVED"
    assert result.failed_step_id == "click_open"
    assert "maintenance_interstitial" in result.expected


def test_result_and_evidence_are_saved(read_balance, tmp_path):
    result = replay(
        read_balance, {"member_id": "100234", "account_type": "Savings"},
        ReplayOptions(evidence_root=tmp_path),
    )
    saved = Path(result.context.evidence_dir) / "result.json"
    assert saved.exists()
    assert result.status.value in saved.read_text(encoding="utf-8")
