"""Deterministic replay: run a saved CapabilityArtifact with no LLM.

For each step: resolve its locator chain, perform the action, then check the
outcome taxonomy (recoverable -> apply recovery and retry the check;
business outcome / hard failure -> stop). At the end, verify the success
checkpoint and collect outputs. Returns Success / BusinessOutcome / Failure --
a condition the artifact itself describes is never an unhandled exception.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from playwright.sync_api import Error as PWError

from .browser import BrowserSession
from .escalation import HandoffTicket, create_ticket, expire_ticket, wait_for_resolution
from .evidence import StepLog
from .guardrails import Guardrail
from .locate import Resolver
from .overlay import TenantOverlay, resolve_effective_artifact
from .redaction import redact_mapping
from .results import (
    BusinessOutcomeResult,
    FailureResult,
    ReplayContext,
    ReplayResult,
    StepTrace,
    SuccessResult,
)
from .textnorm import strip_ids
from .schema import (
    ActionType,
    BusinessOutcomeRule,
    CapabilityArtifact,
    Condition,
    ConditionType,
    ParamType,
    RecoverableRule,
    RecoveryStrategy,
    Step,
    WaitType,
    resolve_template,
)


@dataclass
class ReplayOptions:
    approve_risky: bool = False
    headless: bool = True
    tenant_overlay: TenantOverlay | None = None
    evidence_root: Path = Path("evidence")
    # when True, a blocked confirm/risky step or an unresolved locator pauses
    # the run and raises a HandoffTicket instead of failing immediately --
    # see cua.escalation. When False (the default), both fail fast exactly as
    # before this option existed.
    escalate: bool = False
    escalation_timeout_s: float = 120.0
    escalation_poll_s: float = 0.5
    # fires the instant a ticket is raised, before the (possibly long) wait --
    # replay() itself blocks, so this is how a caller finds out the ticket id
    # in time to act on it (e.g. print "run scripts/operator.py <id>")
    on_escalate: Callable[[HandoffTicket], None] | None = None


class _StopFailure(Exception):
    def __init__(self, **kw):
        self.kw = kw


class _StopBusiness(Exception):
    def __init__(self, rule: BusinessOutcomeRule, step_id: str):
        self.rule = rule
        self.step_id = step_id


def _find_free_port() -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _EscalationContext:
    """Bundles what raising a HandoffTicket needs so the call sites in the
    main loop stay readable. `raise_ticket` alone (no wait) is for the
    non-blocking "review" case; `raise_and_wait` is the full pause."""

    def __init__(self, art, session, ev_dir, ctx, run_id, cdp_port, steplog,
                secrets, opts: ReplayOptions):
        self.art, self.session, self.ev_dir = art, session, ev_dir
        self.ctx, self.run_id, self.cdp_port = ctx, run_id, cdp_port
        self.steplog, self.secrets, self.opts = steplog, secrets, opts

    def raise_ticket(self, *, reason: str, trigger: str, step_id: str,
                     step_description: str) -> HandoffTicket:
        ticket = create_ticket(
            run_id=self.run_id, capability_ref=self.art.ref,
            goal=self.art.metadata.goal, reason=reason, trigger=trigger,
            step_id=step_id, step_description=step_description,
            base_url=self.art.target.base_url,
            cdp_endpoint=f"http://127.0.0.1:{self.cdp_port}",
            screenshot_path=_screenshot(self.session, self.ev_dir),
            dom_snapshot_path=_dom_snapshot(self.session, self.ev_dir),
            inputs_redacted=self.ctx.inputs_redacted,
        )
        self.ctx.escalations.append(ticket.ticket_id)
        self.steplog.write({
            "loop": "replay", "run_id": self.run_id, "event": "escalation_raised",
            "ticket_id": ticket.ticket_id, "reason": reason, "trigger": trigger,
            "cdp_endpoint": ticket.cdp_endpoint,
        }, self.secrets)
        if self.opts.on_escalate:
            self.opts.on_escalate(ticket)
        return ticket

    def raise_and_wait(self, *, reason: str, trigger: str, step) -> HandoffTicket:
        ticket = self.raise_ticket(reason=reason, trigger=trigger,
                                   step_id=step.id, step_description=step.description)
        resolved = wait_for_resolution(
            ticket.ticket_id, timeout_s=self.opts.escalation_timeout_s,
            poll_s=self.opts.escalation_poll_s)
        if resolved is None:
            expire_ticket(ticket.ticket_id)
            raise _StopFailure(
                failure_code="ESCALATION_UNRESOLVED", failed_step_id=step.id,
                step_description=step.description,
                expected="human resolution within the timeout",
                observed=f"ticket {ticket.ticket_id} still open after "
                f"{self.opts.escalation_timeout_s}s")
        self.steplog.write({
            "loop": "replay", "run_id": self.run_id, "event": "escalation_resolved",
            "ticket_id": resolved.ticket_id, "resolution": resolved.resolution,
            "operator_notes": resolved.operator_notes,
            "operator_actions": resolved.operator_actions,
        }, self.secrets)
        return resolved


def replay(artifact: CapabilityArtifact, inputs: dict,
          options: ReplayOptions | None = None) -> ReplayResult:
    opts = options or ReplayOptions()
    art = resolve_effective_artifact(artifact, opts.tenant_overlay)

    params, errors = _validate_inputs(art, inputs)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ev_dir = opts.evidence_root / "replay" / run_id
    ev_dir.mkdir(parents=True, exist_ok=True)

    secrets = [str(inputs[p.name]) for p in art.input_params
              if p.sensitive and p.name in inputs]
    ctx = ReplayContext(
        capability_ref=art.ref,
        tenant_id=art.target.tenant_id,
        inputs_redacted=redact_mapping(dict(inputs), secrets),
        evidence_dir=str(ev_dir),
    )

    if errors:
        return _finish(ctx, ev_dir, FailureResult(
            context=ctx, failure_code="INVALID_INPUT",
            expected="inputs satisfying the declared param types and constraints",
            observed="; ".join(errors)), secrets)

    guard = Guardrail(
        allowed_url_patterns=art.policy.allowed_url_patterns,
        allowed_actions={a.value for a in art.policy.allowed_actions},
        allow_risky=opts.approve_risky,
        risky_step_ids=set(art.policy.risky_step_ids),
    )

    cdp_port = _find_free_port() if opts.escalate else 0
    with BrowserSession(base_url=art.target.base_url, headless=opts.headless,
                        cdp_port=cdp_port) as session, \
         StepLog(ev_dir / "steps.jsonl") as steplog:
        resolver = Resolver(session.page)
        outputs: dict[str, str] = {}
        esc_ctx = _EscalationContext(art, session, ev_dir, ctx, run_id, cdp_port,
                                     steplog, secrets, opts)
        try:
            i = 0
            while i < len(art.steps):
                step = art.steps[i]
                if ctx.steps_executed >= art.policy.max_steps:
                    raise _StopFailure(
                        failure_code="MAX_STEPS", failed_step_id=step.id,
                        step_description=step.description,
                        expected=f"under {art.policy.max_steps} steps",
                        observed="step limit reached")

                gate = guard.check_action(step.action.value, step_id=step.id)
                if not gate:
                    steplog.write({"loop": "replay", "run_id": run_id,
                                  "step_id": step.id, "action": step.action.value,
                                  "ok": False, "result": f"BLOCKED: {gate.reason}"}, secrets)
                    if opts.escalate:
                        ticket = esc_ctx.raise_and_wait(
                            reason="approval_required", trigger="confirm_gate", step=step)
                        if ticket.resolution == "approved":
                            guard.allow_risky = True
                            continue  # re-check this same step; it now passes
                        raise _StopFailure(
                            failure_code="ESCALATION_REJECTED", failed_step_id=step.id,
                            step_description=step.description,
                            expected="operator approval", observed=ticket.operator_notes)
                    raise _StopFailure(
                        failure_code="APPROVAL_REQUIRED", failed_step_id=step.id,
                        step_description=step.description,
                        expected="approval to proceed past an irreversible step",
                        observed=gate.reason)

                started = datetime.now(timezone.utc)
                try:
                    tag, recovered = _execute_step(
                        art, step, session, resolver, params, outputs, guard)
                except _StopFailure as stop:
                    if opts.escalate and stop.kw.get("failure_code") == "LOCATOR_UNRESOLVED":
                        ticket = esc_ctx.raise_and_wait(
                            reason="stuck", trigger="LOCATOR_UNRESOLVED", step=step)
                        if ticket.resolution == "recovered":
                            continue  # nothing was ever acted on -- safe to retry in full
                        raise _StopFailure(
                            failure_code="ESCALATION_REJECTED", failed_step_id=step.id,
                            step_description=step.description,
                            expected="operator recovery", observed=ticket.operator_notes)
                    raise

                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                ctx.trace.append(StepTrace(
                    step_id=step.id, action=step.action.value,
                    description=step.description, started_at=started,
                    duration_ms=duration_ms, locator_used=tag,
                    recovered_from=recovered,
                ))
                steplog.write({"loop": "replay", "run_id": run_id, "step_id": step.id,
                              "action": step.action.value, "description": step.description,
                              "duration_ms": duration_ms, "locator_used": tag,
                              "recovered_from": recovered, "ok": True}, secrets)
                ctx.steps_executed += 1
                i += 1

            result = _check_success(art, session, resolver, params, outputs, ctx)
            steplog.write({"loop": "replay", "run_id": run_id,
                          "event": "checkpoint", "status": result.status.value,
                          "failure_code": getattr(result, "failure_code", None)}, secrets)

        except _StopBusiness as stop:
            result = BusinessOutcomeResult(
                context=ctx, outcome_code=stop.rule.outcome_code,
                message=resolve_template(stop.rule.message_template, params),
                detected_after_step=stop.step_id, escalated=stop.rule.escalate,
                partial_outputs=dict(outputs),
            )
            if opts.escalate and stop.rule.escalate:
                # informational: nothing on the live page fixes "this member
                # isn't eligible" -- record it for a human to review out of
                # band, but don't block the caller on a typed, actionable result
                esc_ctx.raise_ticket(reason="review", trigger=stop.rule.outcome_code,
                                     step_id=stop.step_id, step_description="")
            steplog.write({"loop": "replay", "run_id": run_id, "event": "business_outcome",
                          "outcome_code": result.outcome_code,
                          "detected_after_step": stop.step_id}, secrets)
        except _StopFailure as stop:
            kw = dict(stop.kw)
            kw.setdefault("screenshot_path", _screenshot(session, ev_dir))
            kw.setdefault("dom_snapshot_path", _dom_snapshot(session, ev_dir))
            result = FailureResult(context=ctx, **kw)
            steplog.write({"loop": "replay", "run_id": run_id, "event": "failure",
                          "failure_code": result.failure_code,
                          "failed_step_id": result.failed_step_id}, secrets)
        except PWError as ex:
            result = FailureResult(
                context=ctx, failure_code="ENGINE_ERROR",
                expected="the browser action to complete",
                observed=str(ex).splitlines()[0],
                screenshot_path=_screenshot(session, ev_dir),
                dom_snapshot_path=_dom_snapshot(session, ev_dir))
            steplog.write({"loop": "replay", "run_id": run_id, "event": "failure",
                          "failure_code": "ENGINE_ERROR"}, secrets)

    # a sensitive output's *value* is only known now -- mask it in the
    # persisted copy of this run's evidence, never in what's returned to the
    # caller (that's the whole point of a read capability)
    sensitive_names = {o.name for o in art.outputs if o.sensitive}
    result_outputs = getattr(result, "outputs", None) or \
        getattr(result, "partial_outputs", None) or {}
    secrets += [str(v) for k, v in result_outputs.items() if k in sensitive_names]

    return _finish(ctx, ev_dir, result, secrets)


def _finish(ctx: ReplayContext, ev_dir: Path, result: ReplayResult,
           secrets: list[str]) -> ReplayResult:
    ctx.finished_at = datetime.now(timezone.utc)
    persisted = redact_mapping(json.loads(result.model_dump_json()), secrets)
    (ev_dir / "result.json").write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    return result


# --------------------------------------------------------------------------- #
# one step: resolve -> act -> wait -> check outcomes, retrying recoverable ones
# --------------------------------------------------------------------------- #

def _execute_step(art, step: Step, session, resolver, params, outputs,
                  guard) -> tuple[str | None, str | None]:
    """Perform the step's action once, then check the outcome taxonomy in a
    loop: a recoverable match applies its recovery and re-checks (without
    repeating the action -- the recovery itself, e.g. dismissing an
    interstitial, is what gets the page to the state the step was after).
    Returns (locator tag, ids of any recovery rules that ran)."""
    page = session.page
    tag = _act(step, session, resolver, params, outputs, guard)
    attempts: dict[str, int] = {}

    while True:
        waited = _wait(page, step.wait_after, params) if step.wait_after else True
        rule = _match_outcome(art, page, outputs, step, params)

        if rule is None:
            if not waited:
                raise _StopFailure(
                    failure_code="STEP_TIMEOUT", failed_step_id=step.id,
                    step_description=step.description,
                    expected=f"{step.wait_after.type.value} = {step.wait_after.value!r}",
                    observed=f"url={page.url}")
            return tag, ",".join(attempts) or None

        if isinstance(rule, RecoverableRule):
            n = attempts.get(rule.id, 0)
            if n >= rule.max_attempts:
                raise _StopFailure(
                    failure_code="RECOVERY_EXHAUSTED", failed_step_id=step.id,
                    step_description=step.description,
                    expected=f"{rule.id!r} resolved within {rule.max_attempts} attempts",
                    observed=f"condition still present after {n} attempts")
            attempts[rule.id] = n + 1
            _apply_recovery(rule, step, session, resolver, params)
            continue

        if isinstance(rule, BusinessOutcomeRule):
            raise _StopBusiness(rule, step.id)
        raise _StopFailure(  # HardFailureRule
            failure_code=rule.failure_code, failed_step_id=step.id,
            step_description=step.description,
            expected="no hard-failure condition", observed=rule.description)


def _apply_recovery(rule: RecoverableRule, step: Step, session, resolver, params) -> None:
    page = session.page
    if rule.strategy is RecoveryStrategy.CLICK_AND_CONTINUE:
        loc, _ = resolver.resolve(rule.recovery_target, params)
        if loc is None:
            # its own code, not LOCATOR_UNRESOLVED: the step's action already
            # ran, so escalation's retry-from-scratch must not pick this up
            raise _StopFailure(
                failure_code="RECOVERY_TARGET_UNRESOLVED", failed_step_id=step.id,
                step_description=step.description,
                expected=f"{rule.id!r}: exactly one match for {rule.recovery_target.intent!r}",
                observed=f"{len(rule.recovery_target.strategies)} strategies tried, none "
                f"resolved to a single element (url={page.url})")
        loc.click()
        page.wait_for_load_state("domcontentloaded")
    elif rule.strategy is RecoveryStrategy.WAIT_AND_RETRY:
        page.wait_for_timeout(rule.backoff_ms)
    elif rule.strategy is RecoveryStrategy.RELOAD_AND_RETRY:
        page.reload()
        page.wait_for_load_state("domcontentloaded")


def _act(step: Step, session, resolver, params, outputs, guard) -> str | None:
    page = session.page

    if step.action is ActionType.NAVIGATE:
        target = session.resolve_url(resolve_template(step.value, params))
        gate = guard.check_navigation(target)
        if not gate:
            raise _StopFailure(
                failure_code="BLOCKED_URL", failed_step_id=step.id,
                step_description=step.description,
                expected="a navigation target inside the configured allowlist",
                observed=gate.reason)
        session.goto(target)
        return None
    if step.action is ActionType.CONFIRM:
        return None

    loc, tag = resolver.resolve(step.target, params)
    if loc is None:
        raise _StopFailure(
            failure_code="LOCATOR_UNRESOLVED", failed_step_id=step.id,
            step_description=step.description,
            expected=f"exactly one match for {step.target.intent!r}",
            observed=f"{len(step.target.strategies)} strategies tried, none "
            f"resolved to a single element (url={page.url})")

    if step.action is ActionType.CLICK:
        loc.click()
    elif step.action is ActionType.FILL:
        loc.fill(resolve_template(step.value, params))
    elif step.action is ActionType.SELECT_OPTION:
        loc.select_option(label=resolve_template(step.value, params))
    elif step.action is ActionType.CHECK:
        val = resolve_template(step.value, params).strip().lower()
        loc.set_checked(val in {"yes", "true", "1", "on"})
    elif step.action is ActionType.EXTRACT:
        outputs[step.output_name] = _extract(loc, step)

    page.wait_for_load_state("domcontentloaded")

    if step.action is ActionType.CLICK:
        # a click can navigate (a link, a form submit) to somewhere the
        # allowlist never cleared -- checked here, same as discovery's
        # _do_click, since the destination isn't known until after it happens
        gate = guard.check_navigation(page.url)
        if not gate:
            raise _StopFailure(
                failure_code="BLOCKED_URL", failed_step_id=step.id,
                step_description=step.description,
                expected="the click to leave the page inside the configured allowlist",
                observed=gate.reason)

    return tag


def _extract(loc, step: Step) -> str:
    if step.extract_attribute == "text":
        raw = (loc.inner_text() or "").strip()
    elif step.extract_attribute == "value":
        raw = loc.input_value() or ""
    else:
        raw = loc.get_attribute(step.extract_attribute) or ""
    if step.extract_regex:
        m = re.search(step.extract_regex, raw)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return raw


def _wait(page, wc, params) -> bool:
    if wc.type is WaitType.LOAD:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=wc.timeout_ms)
            return True
        except PWError:
            return False
    if wc.type is WaitType.NETWORK_IDLE:
        try:
            page.wait_for_load_state("networkidle", timeout=wc.timeout_ms)
            return True
        except PWError:
            return False
    if wc.type is WaitType.SELECTOR_VISIBLE:
        try:
            page.locator(wc.value).first.wait_for(state="visible", timeout=wc.timeout_ms)
            return True
        except PWError:
            return False
    if wc.type is WaitType.SELECTOR_HIDDEN:
        try:
            page.locator(wc.value).first.wait_for(state="hidden", timeout=wc.timeout_ms)
            return True
        except PWError:
            return False

    deadline = time.monotonic() + wc.timeout_ms / 1000
    while time.monotonic() < deadline:
        if wc.type is WaitType.URL_MATCHES and re.search(
                resolve_template(wc.value, params), page.url):
            return True
        if wc.type is WaitType.TEXT_PRESENT and \
                resolve_template(wc.value, params) in _visible_text(page):
            return True
        if wc.type is WaitType.TEXT_ABSENT and \
                resolve_template(wc.value, params) not in _visible_text(page):
            return True
        time.sleep(0.1)
    return False


def _visible_text(page) -> str:
    # collapse whitespace the same way perception.py does when it captures
    # banner text during discovery, so the two sides compare consistently
    try:
        return re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
    except PWError:
        return ""


# --- outcome matching / checkpoint ------------------------------------ #

def _check_condition(cond: Condition, page, outputs, resolver, params) -> bool:
    if cond.type is ConditionType.URL_MATCHES:
        return bool(re.search(resolve_template(cond.value, params), page.url))
    if cond.type is ConditionType.URL_CONTAINS:
        return resolve_template(cond.value, params) in page.url
    if cond.type is ConditionType.TEXT_PRESENT:
        want = resolve_template(cond.value, params)
        text = _visible_text(page)
        return want in text or strip_ids(want) in strip_ids(text)
    if cond.type is ConditionType.TEXT_ABSENT:
        want = resolve_template(cond.value, params)
        text = _visible_text(page)
        return want not in text and strip_ids(want) not in strip_ids(text)
    if cond.type is ConditionType.SELECTOR_PRESENT:
        loc, _ = resolver.resolve(cond.target, params)
        return loc is not None
    if cond.type is ConditionType.SELECTOR_ABSENT:
        loc, _ = resolver.resolve(cond.target, params)
        return loc is None
    if cond.type is ConditionType.EXTRACTED_EQUALS:
        return str(outputs.get(cond.output_name)) == resolve_template(cond.value, params)
    if cond.type is ConditionType.EXTRACTED_MATCHES:
        return bool(re.search(cond.value, str(outputs.get(cond.output_name, ""))))
    return False


def _match_outcome(art, page, outputs, step: Step, params):
    # recoverable rules are checked first: a page that superficially matches a
    # business/hard-failure condition (e.g. an interstitial lacks whatever
    # text a "not found" rule looks for too) should be dismissed and
    # re-evaluated before being treated as a terminal outcome.
    resolver = Resolver(page)
    ordered = sorted(art.outcome_taxonomy,
                     key=lambda r: 0 if isinstance(r, RecoverableRule) else 1)
    for rule in ordered:
        scoped = rule.after_steps == "any" or step.id in rule.after_steps
        if not scoped:
            continue
        if all(_check_condition(c, page, outputs, resolver, params) for c in rule.detect):
            return rule
    return None


def _check_success(art, session, resolver, params, outputs, ctx) -> ReplayResult:
    page = session.page
    unmet = [c for c in art.success_checkpoint.conditions
             if not _check_condition(c, page, outputs, resolver, params)]
    if unmet:
        c = unmet[0]
        return FailureResult(
            context=ctx, failure_code="CHECKPOINT_FAILED",
            expected=f"{c.type.value} {c.value!r}"
            + (f" (output {c.output_name})" if c.output_name else ""),
            observed=f"url={page.url}",
            screenshot_path=_screenshot(session, Path(ctx.evidence_dir)),
            dom_snapshot_path=_dom_snapshot(session, Path(ctx.evidence_dir)),
        )

    missing = [o.name for o in art.outputs if o.required and not outputs.get(o.name)]
    if missing:
        return FailureResult(
            context=ctx, failure_code="MISSING_REQUIRED_OUTPUT",
            expected=f"values for {missing}", observed=f"outputs={outputs}")

    return SuccessResult(
        context=ctx, outputs=outputs,
        checkpoint_description=art.success_checkpoint.description,
    )


def _screenshot(session, ev_dir: Path) -> str | None:
    try:
        return str(session.screenshot(ev_dir / "failure.png"))
    except PWError:
        return None


def _dom_snapshot(session, ev_dir: Path) -> str | None:
    try:
        return str(session.dom_snapshot(ev_dir / "failure.html"))
    except PWError:
        return None


# --- input validation ---------------------------------------------- #

def _validate_inputs(art: CapabilityArtifact, inputs: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    cleaned: dict = {}
    for p in art.input_params:
        if p.name not in inputs:
            if p.required:
                errors.append(f"missing required param {p.name!r}")
            continue
        v = inputs[p.name]
        if p.type is ParamType.ENUM and str(v) not in (p.enum_values or []):
            errors.append(f"{p.name}={v!r} not one of {p.enum_values}")
        if p.type is ParamType.MONEY:
            try:
                dv = Decimal(str(v).replace("$", "").replace(",", ""))
            except InvalidOperation:
                errors.append(f"{p.name}={v!r} is not a valid amount")
            else:
                min_amt = p.constraints.get("min_amount")
                if min_amt is not None and dv < Decimal(str(min_amt)):
                    errors.append(f"{p.name}={v!r} is below minimum {min_amt}")
        regex = p.constraints.get("regex")
        if regex and not re.fullmatch(regex, str(v)):
            errors.append(f"{p.name}={v!r} does not match required pattern {regex!r}")
        cleaned[p.name] = v
    return cleaned, errors
