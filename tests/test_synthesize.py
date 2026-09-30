"""Tests for transcript -> artifact synthesis."""

from pathlib import Path

import pytest

from cua.schema import ActionType, CapabilityArtifact, ParamType
from cua.synthesize import (
    _infer_type,
    _parameterize,
    _strip_ids,
    _url_to_regex,
    _value_regex,
    business_rule_from_run,
    transcript_to_artifact,
)
from cua.transcript import DiscoveryTranscript

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def happy():
    return DiscoveryTranscript.model_validate_json(
        (FIX / "discovery_open_subaccount.json").read_text(encoding="utf-8"))


@pytest.fixture
def not_found():
    return DiscoveryTranscript.model_validate_json(
        (FIX / "discovery_member_not_found.json").read_text(encoding="utf-8"))


# --- helpers ----------------------------------------------------------

def test_parameterize_swaps_values_longest_first():
    params = {"member_id": "101445", "account_type": "Savings"}
    assert _parameterize("search for 101445", params) == "search for {member_id}"
    assert _parameterize("no match here", params) == "no match here"


def test_infer_type():
    assert _infer_type("101445", None)[0] is ParamType.STRING
    assert _infer_type("101445", None)[1] == {"regex": r"^\d{6}$"}
    assert _infer_type("250.00", None)[0] is ParamType.MONEY
    assert _infer_type("yes", None)[0] is ParamType.BOOLEAN
    assert _infer_type("Savings", ["Savings", "Checking"])[0] is ParamType.ENUM


def test_url_to_regex_generalises_ids():
    rx = _url_to_regex(
        "http://h/members/101445/sub-accounts/101445-01/confirmation",
        "http://h", {"member_id": "101445"})
    import re
    assert re.match(rx, "/members/999999/sub-accounts/999999-07/confirmation")
    assert not re.match(rx, "/members/1/other")


def test_strip_ids_and_value_regex():
    assert _strip_ids("Sub-account 101445-01 was opened successfully") == \
        "Sub-account was opened successfully"
    assert _value_regex("4210.55") == r"^[\d,]+\.\d{2}$"


# --- end to end -----------------------------------------------------

def test_synthesis_produces_valid_parameterised_artifact(happy, not_found):
    art = transcript_to_artifact(
        happy,
        capability_id="open_subaccount",
        name="Open a member sub-account",
        description="test",
        extra_rules=[business_rule_from_run(
            not_found, rule_id="member_not_found", code="MEMBER_NOT_FOUND")],
        allowed_url_patterns=[r"^http://127\.0\.0\.1:5001/members"],
        overrides={
            "step_ids": {"submit_open_sub": "submit_new_subaccount"},
            "risky_step_ids": ["submit_new_subaccount"],
        },
    )
    # it validates against the schema (all cross-refs hold)
    reparsed = CapabilityArtifact.model_validate_json(art.to_json())
    assert reparsed.capability_id == "open_subaccount"

    # values that came from goal_params are parameterised, not baked in
    fill_steps = [s for s in art.steps if s.action is ActionType.FILL]
    assert any(s.value == "{member_id}" for s in fill_steps)
    assert all("101445" not in (s.value or "") for s in art.steps)

    # provenance points back to the real run
    assert art.metadata.source == "discovery"
    assert art.metadata.discovery_run_id == happy.run_id

    # the risky step is gated by a CONFIRM immediately before it
    ids = [s.id for s in art.steps]
    i = ids.index("submit_new_subaccount")
    assert art.steps[i - 1].action is ActionType.CONFIRM

    # locator chains are ordered and non-empty
    for s in art.steps:
        if s.target:
            assert len(s.target.strategies) >= 1
            assert s.target.strategies[0].confidence.value in {"high", "medium", "low"}


def test_synthesis_rejects_unsuccessful_transcript(not_found):
    with pytest.raises(ValueError, match="goal_reached"):
        transcript_to_artifact(not_found, capability_id="x", name="x", description="x")


def test_business_rule_from_run_uses_real_banner(not_found):
    rule = business_rule_from_run(not_found, rule_id="member_not_found",
                                  code="MEMBER_NOT_FOUND")
    assert rule.outcome_class.value == "business_outcome"
    assert "No members match your search" in rule.detect[0].value
