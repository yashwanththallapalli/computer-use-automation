"""Build a CapabilityArtifact from a successful discovery transcript."""

from __future__ import annotations

import re
from typing import Any

from .schema import (
    ActionType,
    ApprovalState,
    ArtifactMetadata,
    BusinessOutcomeRule,
    CapabilityArtifact,
    CapabilityPolicy,
    Checkpoint,
    Condition,
    ConditionType,
    Confidence,
    InputParam,
    Locator,
    LocatorKind,
    LocatorStrategy,
    OutputField,
    ParamType,
    Step,
    TargetBinding,
    WaitCondition,
    WaitType,
)
from .textnorm import strip_ids as _strip_ids
from .transcript import CapturedElement, DiscoveryStep, DiscoveryTranscript

_ACTION_MAP = {
    "navigate": ActionType.NAVIGATE,
    "click": ActionType.CLICK,
    "type_text": ActionType.FILL,
    "select_option": ActionType.SELECT_OPTION,
    "set_checkbox": ActionType.CHECK,
    "extract_value": ActionType.EXTRACT,
}
_REAL_TOOLS = set(_ACTION_MAP)


def _slug(text: str, limit: int = 3) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    skip = {"the", "a", "an", "to", "on", "for", "of", "in", "into", "and"}
    words = [w for w in words if w not in skip][:limit]
    return "_".join(words) or "step"


def _parameterize(value: str, params: dict[str, Any]) -> str:
    """Replace any parameter value found in `value` with its {placeholder}."""
    out = value
    for name, val in sorted(params.items(), key=lambda kv: -len(str(kv[1]))):
        sval = str(val)
        if sval and sval in out:
            out = out.replace(sval, "{%s}" % name)
    return out


def _value_regex(sample: str) -> str:
    if re.fullmatch(r"[\d,]+\.\d{2}", sample):
        return r"^[\d,]+\.\d{2}$"
    if re.fullmatch(r"[\w]+-[\w]+", sample):
        return r"^[\w-]+$"
    return r"^.+$"


def _url_to_regex(url: str, base_url: str, params: dict[str, Any]) -> str:
    path = url[len(base_url):] if url.startswith(base_url) else url
    for val in sorted((str(v) for v in params.values()), key=len, reverse=True):
        if val:
            path = path.replace(val, "\x00")
    parts = []
    for seg in path.split("/"):
        if "\x00" in seg or re.fullmatch(r"[\w.\x00-]*\d[\w.\x00-]*", seg):
            parts.append(r"[\w.-]+" if seg else "")
        else:
            parts.append(re.escape(seg))
    # allow a trailing query string: a dismissed interstitial or similar
    # recoverable redirect can legitimately leave one (e.g. ?ack=1) that
    # wasn't there on the run this checkpoint was built from.
    return "/".join(parts) + r"(\?.*)?$"


# --- parameters ---------------------------------------------------------

def _infer_type(value: str, enum_values: list[str] | None) -> tuple[ParamType, dict]:
    v = str(value)
    if enum_values:
        return ParamType.ENUM, {}
    if v.lower() in {"yes", "no", "true", "false"}:
        return ParamType.BOOLEAN, {}
    if re.fullmatch(r"\$\d+(\.\d{1,2})?|\d+\.\d{2}", v):  # needs $ or cents
        return ParamType.MONEY, {}
    if v.isdigit():
        return ParamType.STRING, {"regex": r"^\d{%d}$" % len(v)}
    return ParamType.STRING, {}


def _build_params(params: dict[str, Any], select_options: dict[str, list[str]]) -> list[InputParam]:
    out = []
    for name, value in params.items():
        enum_values = select_options.get(name)
        ptype, constraints = _infer_type(value, enum_values)
        out.append(InputParam(
            name=name,
            type=ptype,
            required=True,
            description=f"The {name.replace('_', ' ')} to use.",
            example=str(value),
            enum_values=enum_values,
            constraints=constraints,
        ))
    return out


# --- locators ----------------------------------------------------------

def _locator(cap: CapturedElement, intent: str, params: dict[str, Any]) -> Locator:
    name = _parameterize(cap.name, params) if cap.name else ""
    near = _parameterize(cap.near, params) if cap.near else ""
    row = _parameterize(cap.row_text, params) if cap.row_text else ""
    strategies: list[LocatorStrategy] = []

    # a link/button inside a short data row (a results row, not a page-layout
    # row): scope by the row's parameterised value
    if cap.role in {"link", "button"} and cap.name and "{" in row and len(row) < 120:
        anchor = next((p for p in re.findall(r"\{[a-z_]+\}", row)), "")
        strategies.append(LocatorStrategy(
            kind=LocatorKind.NEAR_TEXT, value=cap.name,
            options={"row_contains": anchor, "target_text": cap.name},
            confidence=Confidence.HIGH,
            rationale=f"The {cap.name!r} control in the row identified by {anchor}.",
        ))

    if name and cap.role in {"link", "button"}:
        strategies.append(LocatorStrategy(
            kind=LocatorKind.ROLE, value=cap.role, options={"name": name},
            confidence=Confidence.HIGH,
            rationale="ARIA role plus accessible name.",
        ))

    # the visible label anchor: reliable for form fields; for links/buttons only
    # when we have nothing better (their `near` is often an unrelated cell).
    if near and (cap.role in {"textbox", "combobox", "checkbox"} or not strategies):
        strategies.append(LocatorStrategy(
            kind=LocatorKind.NEAR_TEXT, value=near, options={"anchor": near},
            confidence=Confidence.HIGH if cap.role in
            {"textbox", "combobox", "checkbox"} else Confidence.LOW,
            rationale="Anchored on the control's visible label.",
        ))

    if cap.name_attr:
        strategies.append(LocatorStrategy(
            kind=LocatorKind.CSS,
            value=f"{cap.tag or '*'}[name='{cap.name_attr}']",
            confidence=Confidence.MEDIUM,
            rationale="Form field name attribute; an implementation detail.",
        ))

    if cap.css_path:
        strategies.append(LocatorStrategy(
            kind=LocatorKind.CSS, value=cap.css_path,
            confidence=Confidence.LOW,
            rationale="Generated DOM path; brittle, last resort.",
        ))

    return Locator(intent=intent, strategies=_dedupe(strategies)[:4] or [
        LocatorStrategy(kind=LocatorKind.TEXT, value=cap.name or intent,
                        confidence=Confidence.LOW, rationale="Fallback on text.")
    ])


def _extract_locator(cap: CapturedElement, intent: str, params: dict[str, Any]) -> Locator:
    column = cap.column or ""
    row = _parameterize(cap.row_text, params) if cap.row_text else ""
    row_hint = re.findall(r"\{[a-z_]+\}", row)
    strategies = [LocatorStrategy(
        kind=LocatorKind.TABLE_CELL, value=column or intent,
        options={"column": column, "row_contains": row_hint},
        confidence=Confidence.HIGH,
        rationale="Cell located by its column header and row content, both "
        "user-visible.",
    )]
    if column:
        strategies.append(LocatorStrategy(
            kind=LocatorKind.NEAR_TEXT, value=column, options={"anchor": column},
            confidence=Confidence.MEDIUM,
            rationale="For key/value grids the header sits next to the value.",
        ))
    if cap.css_path:
        strategies.append(LocatorStrategy(
            kind=LocatorKind.CSS, value=cap.css_path, confidence=Confidence.LOW,
            rationale="Generated DOM path; brittle.",
        ))
    return Locator(intent=intent, strategies=strategies)


def _dedupe(strategies: list[LocatorStrategy]) -> list[LocatorStrategy]:
    seen, out = set(), []
    for s in strategies:
        key = (s.kind, s.value)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


# --- steps -------------------------------------------------------------

def _step_from(ds: DiscoveryStep, step_id: str, params: dict[str, Any]) -> Step:
    action = _ACTION_MAP[ds.tool_name]
    cap = ds.captured_element
    note = ds.tool_input.get("note", "").strip() or ds.result
    common = dict(id=step_id, action=action, description=note)

    if action is ActionType.NAVIGATE:
        return Step(**common, value=_parameterize(ds.tool_input.get("url", "/"), params))

    if action is ActionType.EXTRACT:
        return Step(
            **common,
            target=_extract_locator(cap, note, params),
            output_name=ds.tool_input["output_name"],
        )

    loc = _locator(cap, note, params)
    if action is ActionType.FILL:
        return Step(**common, target=loc,
                    value=_parameterize(ds.tool_input["text"], params))
    if action is ActionType.SELECT_OPTION:
        return Step(**common, target=loc,
                    value=_parameterize(ds.tool_input["option"], params))
    if action is ActionType.CHECK:
        # bind to an authorization-style param if one exists, else literal
        auth = next((p for p in params if re.search(r"auth|consent|confirm", p)), None)
        return Step(**common, target=loc,
                    value=("{%s}" % auth) if auth else str(ds.tool_input.get("checked", True)).lower())
    return Step(**common, target=loc)  # click


# --- checkpoint ------------------------------------------------------

def _checkpoint(t: DiscoveryTranscript, params: dict[str, Any],
                required_outputs: list[OutputField]) -> Checkpoint:
    conds = [Condition(type=ConditionType.URL_MATCHES,
                       value=_url_to_regex(t.final_url, t.base_url, params))]
    phrase = ""
    if t.final_banners:
        phrase = _strip_ids(t.final_banners[0])
    elif t.final_headings:
        phrase = t.final_headings[-1]
    if phrase:
        conds.append(Condition(type=ConditionType.TEXT_PRESENT, value=phrase))
    for o in required_outputs:
        sample = str(t.final_outputs.get(o.name, ""))
        if sample:
            conds.append(Condition(type=ConditionType.EXTRACTED_MATCHES,
                                   value=_value_regex(sample), output_name=o.name))
    return Checkpoint(description="Final state of a successful run.", conditions=conds)


# --- outcome rules from other runs --------------------------------

def business_rule_from_run(t: DiscoveryTranscript, *, rule_id: str, code: str,
                           escalate: bool = False, message: str = "",
                           after_steps: list[str] | str = "any") -> BusinessOutcomeRule:
    banner = (t.final_banners or t.final_headings or [t.final_summary])[0]
    return BusinessOutcomeRule(
        id=rule_id,
        description=f"Observed terminal state: {banner[:120]}",
        detect=[Condition(type=ConditionType.TEXT_PRESENT, value=_strip_ids(banner))],
        after_steps=after_steps,
        outcome_code=code,
        message_template=message or banner,
        terminal=True,
        escalate=escalate,
    )


# --- top level -----------------------------------------------------

def transcript_to_artifact(
    t: DiscoveryTranscript,
    *,
    capability_id: str,
    name: str,
    description: str,
    version: int = 1,
    output_specs: dict[str, dict] | None = None,
    extra_rules: list | None = None,
    allowed_url_patterns: list[str] | None = None,
    overrides: dict | None = None,
) -> CapabilityArtifact:
    if not t.succeeded:
        raise ValueError(f"transcript status is {t.final_status!r}, need goal_reached")

    overrides = overrides or {}
    output_specs = output_specs or {}
    params = t.goal_params

    real = [s for s in t.steps if s.tool_name in _REAL_TOOLS and s.ok]

    # select options seen per bound param (drives enum inference)
    select_options: dict[str, list[str]] = {}
    for s in real:
        if s.tool_name == "select_option" and s.captured_element and s.captured_element.options:
            opts = [o for o in s.captured_element.options if not o.startswith("--")]
            bound = _parameterize(s.tool_input.get("option", ""), params)
            m = re.fullmatch(r"\{([a-z_]+)\}", bound)
            if m:
                select_options[m.group(1)] = opts

    # step ids: verb + label slug, de-duplicated
    id_rename = overrides.get("step_ids", {})
    steps: list[Step] = [Step(
        id="open_entry", action=ActionType.NAVIGATE,
        description=f"Start at {t.entry_path}.", value=t.entry_path,
        wait_after=WaitCondition(type=WaitType.LOAD),
    )]
    used = {"open_entry"}
    for ds in real:
        verb = {"type_text": "enter", "click": "click", "select_option": "select",
                "set_checkbox": "confirm", "extract_value": "read",
                "navigate": "goto"}[ds.tool_name]
        if ds.tool_name == "click" and ds.captured_element and \
                ds.captured_element.role == "button":
            verb = "submit"
        hint = ds.tool_input.get("output_name") or (
            ds.captured_element.name if ds.captured_element and ds.captured_element.name
            else ds.captured_element.near if ds.captured_element else "")
        base = f"{verb}_{_slug(hint, 2)}"
        sid = base
        n = 2
        while sid in used:
            sid, n = f"{base}_{n}", n + 1
        used.add(sid)
        steps.append(_step_from(ds, id_rename.get(sid, sid), params))

    step_ids = {s.id for s in steps}

    # an explicit confirmation gate before each declared irreversible step
    risky = [s for s in overrides.get("risky_step_ids", []) if s in step_ids]
    if risky:
        gated = []
        for s in steps:
            if s.id in risky:
                gated.append(Step(
                    id=f"confirm_{s.id}", action=ActionType.CONFIRM,
                    description="Irreversible boundary: the next step commits a "
                    "change. Replay stops here unless invoked with approval.",
                ))
            gated.append(s)
        steps = gated

    outputs: list[OutputField] = []
    for s in steps:
        if s.action is not ActionType.EXTRACT:
            continue
        oname = s.output_name
        spec = output_specs.get(oname, {})
        sample = str(t.final_outputs.get(oname, ""))
        otype = spec.get("type") or (
            ParamType.MONEY if re.fullmatch(r"[\d,]+\.\d{2}", sample) else ParamType.STRING)
        outputs.append(OutputField(
            name=oname, type=otype,
            description=spec.get("description", f"The {oname.replace('_', ' ')}."),
            source_step_id=s.id,
            required=spec.get("required", True),
            sensitive=spec.get("sensitive", otype is ParamType.MONEY),
        ))

    input_params = _build_params(params, select_options)
    param_patches = overrides.get("params", {})
    input_params = [
        InputParam(**{**p.model_dump(), **param_patches.get(p.name, {})})
        for p in input_params
    ]

    required_outputs = [o for o in outputs if o.required]
    checkpoint = _checkpoint(t, params, required_outputs)

    rules = list(extra_rules or [])
    # CapabilityArtifact's own validator rejects a rule whose after_steps
    # names an unknown step id -- fails loudly rather than silently dropping it

    policy = CapabilityPolicy(
        allowed_url_patterns=allowed_url_patterns or [],
        risky_step_ids=[s for s in overrides.get("risky_step_ids", []) if s in step_ids],
        max_steps=len(steps) + 8,
    )

    return CapabilityArtifact(
        capability_id=capability_id,
        version=version,
        approval=ApprovalState.DRAFT,
        name=name,
        description=description,
        target=TargetBinding(
            app_id=t.target_app_id, app_version=t.target_app_version,
            base_url=t.base_url, entry_path=t.entry_path,
        ),
        input_params=input_params,
        outputs=outputs,
        steps=steps,
        success_checkpoint=checkpoint,
        outcome_taxonomy=rules,
        policy=policy,
        metadata=ArtifactMetadata(
            source="discovery",
            goal=t.goal,
            discovery_run_id=t.run_id,
            model=t.model,
            transcript_path=f"evidence/discovery/{t.run_id}/transcript.json",
        ),
    )
