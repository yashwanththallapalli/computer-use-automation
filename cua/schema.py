"""The capability artifact schema.

A ``CapabilityArtifact`` is what a successful discovery run is frozen into: a
typed, versioned, reviewable description of a UI flow that a deterministic
engine can replay with no LLM in the loop. Steps are surface-neutral (see
``TargetBinding.surface``); locators are ordered fallback chains, each strategy
carrying a confidence and a rationale; branching is data (``outcome_taxonomy``),
not exception handling. Design rationale: REPORT.md.

Pydantic v2 throughout; ``CapabilityArtifact`` serialises to / from JSON.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class SurfaceType(str, Enum):
    """How the replay engine perceives and acts on the target. Only ``web_dom``
    is implemented; the others are declared so the schema does not assume a
    browser (see REPORT: Heterogeneity)."""

    WEB_DOM = "web_dom"
    WEB_A11Y = "web_a11y"
    DESKTOP_A11Y = "desktop_a11y"
    SCREENSHOT_COORDS = "screenshot_coords"


class ActionType(str, Enum):
    NAVIGATE = "navigate"          # go to a (parameterised) path or URL
    CLICK = "click"
    FILL = "fill"                  # type text into a field
    SELECT_OPTION = "select_option"
    CHECK = "check"                # tick a checkbox / radio
    EXTRACT = "extract"            # read a value off the page into an output
    WAIT = "wait"                  # stand-alone wait for a condition
    ASSERT_CONDITION = "assert_condition"  # mid-flow checkpoint
    CONFIRM = "confirm"            # risky/irreversible boundary -- see policy


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    DATE = "date"
    MONEY = "money"


class LocatorKind(str, Enum):
    ROLE = "role"            # accessibility role + accessible name -- preferred
    LABEL = "label"          # form control associated with a <label>
    PLACEHOLDER = "placeholder"
    TEXT = "text"            # visible text content
    ALT_TEXT = "alt_text"
    TITLE = "title"
    TEST_ID = "test_id"      # essentially never present in legacy apps
    NEAR_TEXT = "near_text"  # control positioned relative to a stable label
    TABLE_CELL = "table_cell"  # a cell located by row content + column header
    CSS = "css"
    XPATH = "xpath"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WaitType(str, Enum):
    LOAD = "load"                       # document load state
    URL_MATCHES = "url_matches"         # regex against the current URL
    SELECTOR_VISIBLE = "selector_visible"
    SELECTOR_HIDDEN = "selector_hidden"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    NETWORK_IDLE = "network_idle"


class ConditionType(str, Enum):
    URL_MATCHES = "url_matches"
    URL_CONTAINS = "url_contains"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    SELECTOR_PRESENT = "selector_present"
    SELECTOR_ABSENT = "selector_absent"
    EXTRACTED_EQUALS = "extracted_equals"
    EXTRACTED_MATCHES = "extracted_matches"


class OutcomeClass(str, Enum):
    """The three-way split the replay result contract is built on."""

    BUSINESS_OUTCOME = "business_outcome"  # legitimate result the caller wants
    RECOVERABLE = "recoverable"            # dismiss/retry, then carry on
    HARD_FAILURE = "hard_failure"          # stop, surface a debuggable error


class RecoveryStrategy(str, Enum):
    CLICK_AND_CONTINUE = "click_and_continue"  # dismiss a known interstitial
    WAIT_AND_RETRY = "wait_and_retry"          # transient slowness
    RELOAD_AND_RETRY = "reload_and_retry"


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


# --------------------------------------------------------------------------- #
# Locators, waits, conditions
# --------------------------------------------------------------------------- #


class LocatorStrategy(_Base):
    """One way to find a control. Strategies are tried in order; the first that
    resolves to exactly one element wins."""

    kind: LocatorKind
    value: str = Field(
        description="The selector / text / role name, interpreted per `kind`."
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Kind-specific extras, e.g. {'role': 'button', 'exact': false} for "
            "ROLE, {'anchor': 'Account Type:', 'direction': 'right'} for "
            "NEAR_TEXT, {'row_contains': ['Savings'], 'column': 'Balance'} for "
            "TABLE_CELL."
        ),
    )
    confidence: Confidence = Confidence.MEDIUM
    rationale: str = Field(
        default="",
        description="Why this strategy is (or is not) robust to change.",
    )


class Locator(_Base):
    """A named control plus its ordered fallback chain."""

    intent: str = Field(
        description="Human-readable description of the control, e.g. "
        "'the Account Type dropdown'. Used for review and for bounded "
        "assisted-fallback recovery."
    )
    strategies: list[LocatorStrategy] = Field(min_length=1)

    @property
    def primary(self) -> LocatorStrategy:
        return self.strategies[0]

    def referenced_params(self) -> set[str]:
        out: set[str] = set()
        for s in self.strategies:
            out |= set(_PLACEHOLDER_RE.findall(s.value))
            out |= set(_PLACEHOLDER_RE.findall(json.dumps(s.options)))
        return out

    def resolved(self, params: dict[str, Any]) -> "Locator":
        """Return a copy with ``{param}`` placeholders substituted in strategy
        values and string-valued options (used for parameterised locators like
        'the table row containing {member_id}')."""

        def _opt(v: Any) -> Any:
            if isinstance(v, str):
                return resolve_template(v, params)
            if isinstance(v, list):
                return [_opt(x) for x in v]
            if isinstance(v, dict):
                return {k: _opt(x) for k, x in v.items()}
            return v

        return Locator(
            intent=self.intent,
            strategies=[
                s.model_copy(
                    update={
                        "value": resolve_template(s.value, params),
                        "options": _opt(s.options),
                    }
                )
                for s in self.strategies
            ],
        )


class WaitCondition(_Base):
    """An explicit wait. Replay never sleeps blindly -- every wait is a typed
    condition with a timeout, which is what keeps timing deterministic."""

    type: WaitType
    value: str | None = Field(
        default=None,
        description="Regex, selector, or text depending on `type`.",
    )
    timeout_ms: int = Field(default=5000, ge=0, le=120_000)


class Condition(_Base):
    """A single boolean check against page state. Reused by checkpoints and by
    outcome-detection rules."""

    type: ConditionType
    value: str = Field(
        description="Regex / substring / selector / expected value per `type`."
    )
    target: Locator | None = Field(
        default=None,
        description="Required for SELECTOR_* conditions and for EXTRACTED_* "
        "when checking a specific control.",
    )
    output_name: str | None = Field(
        default=None,
        description="For EXTRACTED_* conditions: which declared output to test.",
    )
    description: str = ""


class Checkpoint(_Base):
    """A set of conditions that must *all* hold. Used for the final
    ``success_checkpoint`` and for mid-flow ``assert_condition`` steps."""

    description: str
    conditions: list[Condition] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Inputs / outputs -- the capability's typed contract
# --------------------------------------------------------------------------- #


class InputParam(_Base):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: Any = None
    enum_values: list[str] | None = Field(
        default=None, description="Allowed values when type == ENUM."
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict,
        description="Validation hints the caller/agent applies before invoking: "
        "min, max, min_amount, regex, max_length, ...",
    )
    sensitive: bool = Field(
        default=False,
        description="If true, the resolved value is redacted from all logs and "
        "evidence and is never written into a saved artifact.",
    )

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(f"param name {v!r} must be snake_case")
        return v

    @model_validator(mode="after")
    def _enum_has_values(self) -> "InputParam":
        if self.type is ParamType.ENUM and not self.enum_values:
            raise ValueError(f"enum param {self.name!r} needs enum_values")
        return self


class OutputField(_Base):
    name: str
    type: ParamType
    description: str
    source_step_id: str = Field(
        description="The id of the EXTRACT step that produces this value."
    )
    required: bool = Field(
        default=True,
        description="If true, a successful replay must have populated this; a "
        "missing required output is a hard failure, not a success.",
    )
    sensitive: bool = Field(
        default=False,
        description="Regulated / PII value. Returned to the authorised caller "
        "but masked in every log and evidence file, never persisted in clear.",
    )

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(f"output name {v!r} must be snake_case")
        return v


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


class Step(_Base):
    """One ordered action in the flow.

    ``value`` is always treated as a template: ``"{member_id}"`` resolves to that
    parameter, ``"250.00"`` is a literal, ``"{member_id}-x"`` interpolates. For
    NAVIGATE it is a path (resolved against ``target.base_url``) or a full URL.
    """

    id: str = Field(description="Stable, unique within the artifact (e.g. 's1', "
                    "'submit_search'). Referenced by outputs and results.")
    action: ActionType
    description: str = Field(
        description="What this step does and why -- carried from the discovery "
        "run's reasoning, for human review."
    )
    target: Locator | None = Field(
        default=None, description="The control to act on. Omitted for NAVIGATE "
        "and stand-alone WAIT."
    )
    value: str | None = Field(
        default=None,
        description="Template string; see class docstring. Used by NAVIGATE, "
        "FILL, SELECT_OPTION, CHECK.",
    )
    wait_before: WaitCondition | None = Field(
        default=None, description="Must hold before the action is attempted."
    )
    wait_after: WaitCondition | None = Field(
        default=None, description="Must hold before the step is considered done."
    )
    optional: bool = Field(
        default=False,
        description="If the target/condition is absent, skip without failing "
        "(e.g. a 'dismiss interstitial' step that only sometimes applies).",
    )
    check_outcomes: list[str] = Field(
        default_factory=list,
        description="Ids of outcome_taxonomy rules to evaluate immediately after "
        "this step (in addition to the always-on check). Empty == only the "
        "global check runs.",
    )

    # EXTRACT-only fields
    output_name: str | None = Field(
        default=None, description="EXTRACT: the output this step populates."
    )
    extract_attribute: str = Field(
        default="text",
        description="EXTRACT: 'text' | 'value' | 'href' | an attribute name.",
    )
    extract_regex: str | None = Field(
        default=None,
        description="EXTRACT: if set, the first capturing group is the value.",
    )

    # ASSERT_CONDITION-only
    checkpoint: Checkpoint | None = Field(
        default=None, description="ASSERT_CONDITION: the conditions to assert."
    )

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9_]*$", v):
            raise ValueError(f"step id {v!r} must be snake_case/alnum")
        return v

    @model_validator(mode="after")
    def _shape_per_action(self) -> "Step":
        a = self.action
        needs_target = {
            ActionType.CLICK, ActionType.FILL, ActionType.SELECT_OPTION,
            ActionType.CHECK, ActionType.EXTRACT,
        }
        if a in needs_target and self.target is None:
            raise ValueError(f"step {self.id!r}: action {a.value} requires a target")
        needs_value = {ActionType.NAVIGATE, ActionType.FILL, ActionType.SELECT_OPTION}
        if a in needs_value and not self.value:
            raise ValueError(f"step {self.id!r}: action {a.value} requires a value")
        if a is ActionType.EXTRACT and not self.output_name:
            raise ValueError(f"step {self.id!r}: EXTRACT requires output_name")
        if a is ActionType.ASSERT_CONDITION and self.checkpoint is None:
            raise ValueError(f"step {self.id!r}: ASSERT_CONDITION requires a checkpoint")
        return self

    def referenced_params(self) -> set[str]:
        out: set[str] = set()
        if self.value:
            out |= set(_PLACEHOLDER_RE.findall(self.value))
        if self.target is not None:
            out |= self.target.referenced_params()
        for wc in (self.wait_before, self.wait_after):
            if wc is not None and wc.value:
                out |= set(_PLACEHOLDER_RE.findall(wc.value))
        if self.checkpoint is not None:
            for c in self.checkpoint.conditions:
                out |= set(_PLACEHOLDER_RE.findall(c.value))
        return out


# --------------------------------------------------------------------------- #
# Outcome taxonomy -- declarative branching
# --------------------------------------------------------------------------- #


class _OutcomeRuleBase(_Base):
    id: str
    description: str
    detect: list[Condition] = Field(
        min_length=1,
        description="ALL conditions must hold for this outcome to be recognised.",
    )
    after_steps: list[str] | Literal["any"] = Field(
        default="any",
        description="Bound the check to specific step ids, or 'any' to evaluate "
        "after every step.",
    )

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(f"outcome id {v!r} must be snake_case")
        return v


class BusinessOutcomeRule(_OutcomeRuleBase):
    """A legitimate result the calling agent needs to know about. Terminates the
    replay and is returned as a typed ``BUSINESS_OUTCOME`` result -- not a
    failure."""

    outcome_class: Literal[OutcomeClass.BUSINESS_OUTCOME] = OutcomeClass.BUSINESS_OUTCOME
    outcome_code: str = Field(description="Machine code, e.g. 'MEMBER_NOT_FOUND'.")
    message_template: str = Field(
        description="Human message; may reference {params} and {outputs}."
    )
    terminal: bool = True
    escalate: bool = Field(
        default=False,
        description="Also raise a human escalation (e.g. a permission denial "
        "that a supervisor could override).",
    )


class RecoverableRule(_OutcomeRuleBase):
    """A transient / known-interstitial condition. Replay applies the recovery
    and then re-attempts the interrupted step."""

    outcome_class: Literal[OutcomeClass.RECOVERABLE] = OutcomeClass.RECOVERABLE
    strategy: RecoveryStrategy
    recovery_target: Locator | None = Field(
        default=None, description="The control to click for CLICK_AND_CONTINUE."
    )
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_ms: int = Field(default=1000, ge=0, le=30_000)


class HardFailureRule(_OutcomeRuleBase):
    """An explicitly recognised dead-end. Stops replay with a debuggable
    ``FAILURE`` result. (Unrecognised errors also fail -- this is for the ones
    worth naming, e.g. a session-timeout screen.)"""

    outcome_class: Literal[OutcomeClass.HARD_FAILURE] = OutcomeClass.HARD_FAILURE
    failure_code: str = Field(description="e.g. 'SESSION_TIMEOUT'.")


OutcomeRule = Annotated[
    Union[BusinessOutcomeRule, RecoverableRule, HardFailureRule],
    Field(discriminator="outcome_class"),
]


# --------------------------------------------------------------------------- #
# Target binding, policy, metadata
# --------------------------------------------------------------------------- #


class TargetBinding(_Base):
    """Identifies the app instance a flow runs against. The seam between the
    surface-neutral ``steps`` and a concrete running system."""

    app_id: str = Field(description="Vendor product identity, e.g. "
                        "'meridian-backoffice'. Shared across tenants running "
                        "the same product.")
    app_version: str | None = Field(
        default=None, description="e.g. '3.11.2'. Drives version-drift handling."
    )
    tenant_id: str | None = Field(
        default=None,
        description="None == vendor-generic base artifact. Set == specialised "
        "for one institution (usually via a TenantOverlay, not a re-recording).",
    )
    surface: SurfaceType = SurfaceType.WEB_DOM
    base_url: str = Field(description="Resolved at replay time; overridable per "
                          "tenant/environment.")
    entry_path: str = Field(
        default="/", description="Where a run of this capability begins."
    )


class CapabilityPolicy(_Base):
    """Per-capability guardrail configuration, enforced by ``cua.guardrails``
    before every action in both the discovery and replay loops."""

    allowed_url_patterns: list[str] = Field(
        default_factory=list,
        description="Regexes; every navigation and post-action URL must match "
        "at least one. Empty == inherit the engine-level allowlist only.",
    )
    allowed_actions: list[ActionType] = Field(
        default_factory=lambda: [
            ActionType.NAVIGATE, ActionType.CLICK, ActionType.FILL,
            ActionType.SELECT_OPTION, ActionType.CHECK, ActionType.EXTRACT,
            ActionType.WAIT, ActionType.ASSERT_CONDITION, ActionType.CONFIRM,
        ],
    )
    risky_step_ids: list[str] = Field(
        default_factory=list,
        description="Steps that are irreversible / write-effecting (e.g. the "
        "POST that actually opens the account). Replay stops and escalates at "
        "these unless invoked with explicit approval.",
    )
    max_steps: int = Field(default=40, ge=1, le=500)


class ArtifactMetadata(_Base):
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source: Literal["discovery", "hand_authored"] = "discovery"
    goal: str = Field(description="The natural-language goal this was recorded "
                      "from.")
    discovery_run_id: str | None = None
    model: str | None = Field(
        default=None, description="LLM that produced the discovery run."
    )
    transcript_path: str | None = Field(
        default=None,
        description="Relative path to the raw discovery transcript in "
        "/evidence/. The artifact is decoupled from it but points back to it.",
    )
    notes: str | None = None


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #


class CapabilityArtifact(_Base):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability_id: str = Field(description="Stable slug, e.g. 'open_subaccount'.")
    version: int = Field(ge=1, description="Monotonic per capability_id.")
    supersedes: int | None = Field(
        default=None, description="The version this one replaces, if any."
    )
    approval: ApprovalState = ApprovalState.DRAFT

    name: str
    description: str = Field(
        description="What the capability does, what it needs and what it "
        "returns -- written for both a human reviewer and a calling agent."
    )

    target: TargetBinding
    input_params: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success_checkpoint: Checkpoint
    outcome_taxonomy: list[OutcomeRule] = Field(default_factory=list)
    policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    metadata: ArtifactMetadata

    @field_validator("capability_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(f"capability_id {v!r} must be snake_case")
        return v

    @model_validator(mode="after")
    def _cross_references(self) -> "CapabilityArtifact":
        step_ids = [s.id for s in self.steps]
        dupes = {i for i in step_ids if step_ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        step_id_set = set(step_ids)

        param_names = {p.name for p in self.input_params}
        for s in self.steps:
            missing = s.referenced_params() - param_names
            if missing:
                raise ValueError(
                    f"step {s.id!r} references undeclared params: {sorted(missing)}"
                )

        extract_steps = {s.id for s in self.steps if s.action is ActionType.EXTRACT}
        for o in self.outputs:
            if o.source_step_id not in extract_steps:
                raise ValueError(
                    f"output {o.name!r} source_step_id {o.source_step_id!r} is "
                    f"not an EXTRACT step"
                )
        declared_outputs = {o.name for o in self.outputs}
        for s in self.steps:
            if s.action is ActionType.EXTRACT and s.output_name not in declared_outputs:
                raise ValueError(
                    f"step {s.id!r} populates undeclared output {s.output_name!r}"
                )

        for rule in self.outcome_taxonomy:
            if rule.after_steps == "any":
                continue
            unknown = set(rule.after_steps) - step_id_set
            if unknown:
                raise ValueError(
                    f"outcome rule {rule.id!r} after_steps unknown: {sorted(unknown)}"
                )

        for sid in self.policy.risky_step_ids:
            if sid not in step_id_set:
                raise ValueError(f"policy.risky_step_ids: unknown step {sid!r}")

        return self

    # -- convenience ------------------------------------------------------- #

    @property
    def ref(self) -> str:
        return f"{self.capability_id}@v{self.version}"

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def to_json(self, *, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "CapabilityArtifact":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


def resolve_template(template: str, params: dict[str, Any]) -> str:
    """Substitute ``{param}`` placeholders. Raises if a referenced param is
    missing so replay fails loudly rather than sending ``"{member_id}"`` to a
    form field."""

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in params:
            raise KeyError(f"template {template!r} needs missing param {key!r}")
        return str(params[key])

    return _PLACEHOLDER_RE.sub(_sub, template)
