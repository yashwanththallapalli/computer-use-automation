"""Schema tests: the example artifacts validate, round-trip, and the
cross-reference / discriminated-union / overlay logic holds."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from cua.schema import (
    ActionType,
    ArtifactMetadata,
    BusinessOutcomeRule,
    CapabilityArtifact,
    Checkpoint,
    Condition,
    ConditionType,
    HardFailureRule,
    InputParam,
    Locator,
    LocatorKind,
    LocatorStrategy,
    OutcomeRule,
    OutputField,
    ParamType,
    RecoverableRule,
    Step,
    TargetBinding,
    resolve_template,
)
from cua.overlay import TenantOverlay, resolve_effective_artifact
from cua.results import BusinessOutcomeResult, FailureResult, ReplayResult, SuccessResult

CAP_DIR = Path(__file__).resolve().parent.parent / "cua" / "capabilities"
ARTIFACTS = sorted(CAP_DIR.glob("*.json"))


@pytest.fixture(scope="module", autouse=True)
def _ensure_artifacts():
    if not ARTIFACTS:
        pytest.skip("run scripts/build_example_artifacts.py first")


@pytest.mark.parametrize("path", ARTIFACTS, ids=lambda p: p.name)
def test_example_artifact_validates_and_roundtrips(path):
    art = CapabilityArtifact.load(path)
    again = CapabilityArtifact.model_validate_json(art.to_json())
    assert again.to_json() == art.to_json()
    # every EXTRACT step feeds a declared output and vice versa
    extract_ids = {s.id for s in art.steps if s.action is ActionType.EXTRACT}
    assert {o.source_step_id for o in art.outputs} <= extract_ids


def test_open_subaccount_shape():
    art = CapabilityArtifact.load(CAP_DIR / "open_subaccount.v1.json")
    assert art.ref == "open_subaccount@v1"
    assert art.metadata.source == "discovery"
    # a risky step is declared and gated by an explicit CONFIRM step just before it
    assert art.policy.risky_step_ids
    risky = art.policy.risky_step_ids[0]
    idx = [s.id for s in art.steps].index(risky)
    assert art.steps[idx - 1].action is ActionType.CONFIRM
    codes = {r.outcome_code for r in art.outcome_taxonomy
             if isinstance(r, BusinessOutcomeRule)}
    assert {"MEMBER_NOT_FOUND", "MEMBER_NOT_ELIGIBLE"} <= codes
    assert any(r.escalate for r in art.outcome_taxonomy
               if isinstance(r, BusinessOutcomeRule) and r.outcome_code == "MEMBER_NOT_ELIGIBLE")
    # every {param} referenced anywhere is declared
    declared = {p.name for p in art.input_params}
    for s in art.steps:
        assert s.referenced_params() <= declared


def test_read_balance_shape():
    art = CapabilityArtifact.load(CAP_DIR / "read_member_balance.v1.json")
    bal = next(o for o in art.outputs if o.name == "balance")
    assert bal.sensitive is True
    assert ActionType.CONFIRM not in {s.action for s in art.steps}  # read-only
    assert len([s for s in art.steps if s.action is ActionType.EXTRACT]) == 3
    atype = next(p for p in art.input_params if p.name == "account_type")
    assert atype.type.value == "enum" and "Savings" in atype.enum_values


# --- templating -----------------------------------------------------------

def test_resolve_template_and_missing():
    assert resolve_template("/members/{member_id}", {"member_id": "101445"}) == \
        "/members/101445"
    assert resolve_template("250.00", {}) == "250.00"
    with pytest.raises(KeyError):
        resolve_template("{member_id}", {})


def test_locator_resolved_substitutes_in_options():
    loc = Locator(
        intent="row for {member_id}",
        strategies=[LocatorStrategy(
            kind=LocatorKind.NEAR_TEXT, value="Open",
            options={"row_contains": "{member_id}"},
        )],
    )
    assert loc.referenced_params() == {"member_id"}
    out = loc.resolved({"member_id": "100234"})
    assert out.strategies[0].options["row_contains"] == "100234"


# --- validators reject bad artifacts ------------------------------------

def _minimal_kwargs(**over):
    base = dict(
        capability_id="c", version=1, name="c", description="d",
        target=TargetBinding(app_id="a", base_url="http://x"),
        steps=[Step(id="s1", action=ActionType.NAVIGATE, description="go",
                    value="/")],
        success_checkpoint=Checkpoint(
            description="ok",
            conditions=[Condition(type=ConditionType.TEXT_PRESENT, value="x")]),
        metadata=ArtifactMetadata(goal="g"),
    )
    base.update(over)
    return base


def test_rejects_duplicate_step_ids():
    with pytest.raises(ValidationError, match="duplicate step ids"):
        CapabilityArtifact(**_minimal_kwargs(steps=[
            Step(id="s1", action=ActionType.NAVIGATE, description="a", value="/"),
            Step(id="s1", action=ActionType.NAVIGATE, description="b", value="/"),
        ]))


def test_rejects_undeclared_param():
    with pytest.raises(ValidationError, match="undeclared params"):
        CapabilityArtifact(**_minimal_kwargs(steps=[
            Step(id="s1", action=ActionType.NAVIGATE, description="a",
                 value="/members/{member_id}"),
        ]))


def test_rejects_output_not_from_extract_step():
    with pytest.raises(ValidationError, match="not an EXTRACT step"):
        CapabilityArtifact(**_minimal_kwargs(
            outputs=[OutputField(name="x", type=ParamType.STRING,
                                 description="d", source_step_id="s1")],
        ))


def test_rejects_unknown_risky_step():
    from cua.schema import CapabilityPolicy
    with pytest.raises(ValidationError, match="unknown step"):
        CapabilityArtifact(**_minimal_kwargs(
            policy=CapabilityPolicy(risky_step_ids=["nope"])))


# --- discriminated unions ---------------------------------------------

def test_outcome_rule_union_discriminates():
    ta = TypeAdapter(OutcomeRule)
    common = dict(id="r", description="d",
                  detect=[{"type": "text_present", "value": "x"}])
    assert isinstance(ta.validate_python(
        {**common, "outcome_class": "business_outcome",
         "outcome_code": "X", "message_template": "m"}), BusinessOutcomeRule)
    assert isinstance(ta.validate_python(
        {**common, "outcome_class": "recoverable",
         "strategy": "wait_and_retry"}), RecoverableRule)
    assert isinstance(ta.validate_python(
        {**common, "outcome_class": "hard_failure",
         "failure_code": "X"}), HardFailureRule)


def test_replay_result_union_discriminates():
    ta = TypeAdapter(ReplayResult)
    ctx = {"capability_ref": "c@v1"}
    assert isinstance(ta.validate_python(
        {"status": "success", "context": ctx,
         "checkpoint_description": "ok"}), SuccessResult)
    assert isinstance(ta.validate_python(
        {"status": "business_outcome", "context": ctx, "outcome_code": "X",
         "message": "m", "detected_after_step": "s1"}), BusinessOutcomeResult)
    assert isinstance(ta.validate_python(
        {"status": "failure", "context": ctx, "failure_code": "X",
         "expected": "e", "observed": "o"}), FailureResult)


# --- tenant overlay --------------------------------------------------

def test_overlay_merges_and_guards_version():
    base = CapabilityArtifact.load(CAP_DIR / "read_member_balance.v1.json")
    fill_id = next(s.id for s in base.steps if s.id.startswith("enter_"))
    status_step = next(s.id for s in base.steps if s.output_name == "account_status")
    overlay = TenantOverlay(
        capability_id="read_member_balance", base_version=1, tenant_id="cu_west",
        target_overrides={"base_url": "https://cu-west.example",
                          "app_version": "3.12.0"},
        locator_overrides={fill_id: [LocatorStrategy(
            kind=LocatorKind.TEST_ID, value="member-search",
            rationale="cu_west runs a themed build that adds data-testids")]},
        disabled_step_ids=[status_step],
    )
    eff = resolve_effective_artifact(base, overlay)
    assert eff.target.base_url == "https://cu-west.example"
    assert eff.target.tenant_id == "cu_west"
    assert status_step not in {s.id for s in eff.steps}
    assert "account_status" not in {o.name for o in eff.outputs}  # output dropped too
    assert eff.step(fill_id).target.primary.kind is LocatorKind.TEST_ID
    # base strategy retained as fallback
    assert len(eff.step(fill_id).target.strategies) == \
        len(base.step(fill_id).target.strategies) + 1

    bad = overlay.model_copy(update={"base_version": 2})
    with pytest.raises(ValueError, match="re-review"):
        resolve_effective_artifact(base, bad)


def test_overlay_none_is_identity():
    base = CapabilityArtifact.load(CAP_DIR / "open_subaccount.v1.json")
    assert resolve_effective_artifact(base, None) is base
