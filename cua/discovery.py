"""The discovery agent loop: observe -> LLM tool call -> act -> log.

This is one genuine LLM-driven run against the live mock app. It produces a
``DiscoveryTranscript`` and writes evidence under
``evidence/discovery/<run_id>/``.

The transcript handed back to the caller keeps its real values the whole way
through, since synthesis (see ``cua.synthesize``) needs them to parameterise
the artifact and match table rows. Redaction only happens once, at write
time, on whatever actually gets persisted to disk -- same pattern
``cua.replay`` uses for its own evidence.

The model makes the decisions here; this module just perceives, enforces
guardrails, acts, and writes it all down. Turning a transcript into an
artifact is a separate step, handled in ``cua.synthesize``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .agent_tools import TOOLS, ActionExecutor
from .browser import BrowserSession
from .evidence import StepLog
from .guardrails import Guardrail
from .perception import perceive
from .redaction import redact_mapping
from .transcript import CapturedElement, DiscoveryStep, DiscoveryTranscript

load_dotenv()

SYSTEM_PROMPT = """\
You are an automation agent operating a legacy bank back-office web application \
to accomplish one goal. You are driving a real browser used by a human teller.

You do NOT see images. Each turn you get a structured text OBSERVATION: the URL, \
page headings, any banner/alert messages, a digest of the page content, and a \
numbered list of ACTIONABLE ELEMENTS. Act only on elements in that list, by \
their [n] ref.

Rules:
- Exactly ONE tool call per turn. After acting, wait for the next observation \
to confirm the effect before continuing.
- In every action's `note`, name the element by the VISIBLE LABEL a human would \
read next to it, and say why you are acting. This is recorded and reused.
- Use only the parameter values given in the goal. Never invent member data, \
amounts, or names.
- If the app shows a legitimate terminal result that is not the goal -- "no \
members match", a permission/eligibility denial, a validation error you cannot \
fix with the given inputs -- call `finish` with status "business_outcome" and \
explain it. Do not fight it.
- If you cannot make progress, call `finish` with status "stuck".
- When the goal's success condition is visibly satisfied on the page, call \
`finish` with status "goal_reached" and put the requested outputs in `outputs` \
as a JSON object.
- Before any irreversible step (submitting a form that creates or changes a \
record), make sure every field is set correctly -- you will not get to redo it.

GOAL: {goal}

CONCRETE VALUES FOR THIS RUN:
{params}

REQUESTED OUTPUTS: {outputs}
"""


@dataclass
class TargetSpec:
    app_id: str
    base_url: str
    entry_path: str = "/"
    app_version: str | None = None


@dataclass
class DiscoveryConfig:
    model: str = field(default_factory=lambda: os.environ.get("CUA_MODEL", "claude-opus-5"))
    max_steps: int = 18
    max_tokens: int = 8000
    headless: bool = True
    allow_risky: bool = True          # discovery is permitted to submit real forms
    allowed_url_patterns: list[str] = field(default_factory=list)
    evidence_root: Path = Path("evidence")


def _content_blocks_to_dicts(content) -> list[dict]:
    out = []
    for b in content:
        out.append(b.model_dump(mode="json") if hasattr(b, "model_dump") else dict(b))
    return out


def _text_of(content) -> str:
    return " ".join(
        b.text for b in content if getattr(b, "type", None) == "text"
    ).strip()


def _thinking_of(content) -> str:
    return " ".join(
        getattr(b, "thinking", "") for b in content
        if getattr(b, "type", None) == "thinking"
    ).strip()


def run_discovery(
    goal: str,
    goal_params: dict,
    target: TargetSpec,
    *,
    requested_outputs: list[str] | None = None,
    sensitive_params: list[str] | None = None,
    sensitive_outputs: list[str] | None = None,
    config: DiscoveryConfig | None = None,
) -> DiscoveryTranscript:
    cfg = config or DiscoveryConfig()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ev_dir = cfg.evidence_root / "discovery" / run_id
    ev_dir.mkdir(parents=True, exist_ok=True)

    # Known up front from the declared sensitive params. A sensitive *output*
    # (e.g. a balance) isn't known until it's extracted, so it's added to this
    # same list as the run goes -- the log is only ever written at the end,
    # redacted with the final, complete list. The email/phone/DOB/SSN regex
    # backstop in cua.redaction applies regardless.
    secrets = [str(goal_params[k]) for k in (sensitive_params or []) if k in goal_params]
    client = anthropic.Anthropic()
    guard = Guardrail(
        allowed_url_patterns=cfg.allowed_url_patterns,
        allowed_actions={t["name"] for t in TOOLS},
        allow_risky=cfg.allow_risky,
    )

    system = SYSTEM_PROMPT.format(
        goal=goal,
        params="\n".join(f"- {k} = {v}" for k, v in goal_params.items()) or "- (none)",
        outputs=", ".join(requested_outputs or []) or "(none)",
    )

    transcript = DiscoveryTranscript(
        run_id=run_id, goal=goal, goal_params=goal_params,
        target_app_id=target.app_id, target_app_version=target.app_version,
        base_url=target.base_url, entry_path=target.entry_path, model=cfg.model,
    )

    log_lines: list[str] = []

    def logline(msg: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log_lines.append(f"[{stamp}] {msg}")

    session = BrowserSession(base_url=target.base_url, headless=cfg.headless,
                             record_dir=ev_dir / "video")
    steplog = StepLog(ev_dir / "steps.jsonl")
    messages: list[dict] = []
    totals = {"input_tokens": 0, "output_tokens": 0}

    def _note_sensitive(extracted: dict) -> None:
        for name, value in extracted.items():
            if name in (sensitive_outputs or []) and str(value) not in secrets:
                secrets.append(str(value))

    try:
        session.start()
        session.goto(target.entry_path)
        logline(f"discovery start: model={cfg.model} goal={goal!r}")

        for i in range(cfg.max_steps):
            obs = perceive(session.page)
            shot = session.screenshot(ev_dir / f"step_{i:02d}.png")
            obs_text = obs.to_prompt()
            logline(f"step {i}: {obs.url} | elements={len(obs.elements)}"
                    + (f" | banners={obs.banners}" if obs.banners else ""))

            messages.append({"role": "user",
                             "content": f"OBSERVATION (step {i}):\n\n{obs_text}"})

            resp = client.messages.create(
                model=cfg.model,
                max_tokens=cfg.max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                thinking={"type": "adaptive", "display": "summarized"},
                tools=TOOLS,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=messages,
            )
            totals["input_tokens"] += resp.usage.input_tokens
            totals["output_tokens"] += resp.usage.output_tokens
            messages.append({"role": "assistant",
                             "content": _content_blocks_to_dicts(resp.content)})

            thinking = _thinking_of(resp.content)
            assistant_text = _text_of(resp.content)
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]

            if not tool_uses:
                logline(f"step {i}: model made no tool call; nudging")
                messages.append({"role": "user", "content":
                    "You did not call a tool. Call exactly one tool now."})
                transcript.steps.append(DiscoveryStep(
                    index=i, url=obs.url, observation=obs_text,
                    thinking=thinking, assistant_text=assistant_text,
                    tool_name="(none)", ok=False,
                    result="no tool call", screenshot=str(shot.relative_to(cfg.evidence_root)),
                ))
                steplog.write({"loop": "discovery", "run_id": run_id, "index": i,
                              "url": obs.url, "tool": "(none)", "ok": False,
                              "result": "no tool call"}, secrets)
                continue

            tu = tool_uses[0]
            args = dict(tu.input)
            logline(f"step {i}: {tu.name}({json.dumps(args)})")

            outcome = ActionExecutor(session, guard, ev_dir).execute(tu.name, args, obs)
            _note_sensitive(outcome.extracted)
            logline(f"step {i}: -> ok={outcome.ok} {outcome.detail}")

            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tu.id,
                "content": outcome.detail, "is_error": not outcome.ok,
            }]})

            transcript.steps.append(DiscoveryStep(
                index=i, url=obs.url, observation=obs_text,
                thinking=thinking, assistant_text=assistant_text,
                tool_name=tu.name, tool_input=args,
                guardrail_allowed=not outcome.detail.startswith("BLOCKED"),
                guardrail_reason=outcome.detail if outcome.detail.startswith("BLOCKED") else "",
                ok=outcome.ok, result=outcome.detail,
                captured_element=outcome.captured_element,
                extracted=dict(outcome.extracted),
                screenshot=str(shot.relative_to(cfg.evidence_root)),
            ))
            steplog.write({
                "loop": "discovery", "run_id": run_id, "index": i, "url": obs.url,
                "tool": tu.name, "args": args, "ok": outcome.ok,
                "result": outcome.detail, "reasoning": assistant_text or thinking,
                "extracted": dict(outcome.extracted),
                "screenshot": str(shot.relative_to(cfg.evidence_root)),
            }, secrets)

            if outcome.is_finish:
                transcript.final_status = (
                    outcome.finish_status if outcome.finish_status in
                    {"goal_reached", "business_outcome", "stuck"} else "stuck"
                )
                transcript.final_summary = outcome.finish_summary
                transcript.final_outputs = outcome.finish_outputs
                transcript.final_url = obs.url
                transcript.final_headings = obs.headings
                transcript.final_banners = obs.banners
                _note_sensitive(outcome.finish_outputs)
                logline(f"finished: {transcript.final_status} | {outcome.finish_summary}")
                break
        else:
            transcript.final_status = "max_steps"
            logline("stopped: max_steps reached")

    except Exception as ex:  # noqa: BLE001 -- record then re-raise-as-status
        transcript.final_status = "error"
        transcript.final_summary = f"{type(ex).__name__}: {ex}"
        logline(f"ERROR: {type(ex).__name__}: {ex}")
        steplog.write({"loop": "discovery", "run_id": run_id,
                      "event": "error", "error": f"{type(ex).__name__}: {ex}"}, secrets)
    finally:
        steplog.close()
        transcript.finished_at = datetime.now(timezone.utc)
        transcript.token_usage = totals
        transcript.raw_messages = messages

        # everything above is real; redaction happens once, here, over the
        # copies that actually get written to disk
        (ev_dir / "transcript.json").write_text(
            json.dumps(redact_mapping(json.loads(transcript.model_dump_json()), secrets),
                       indent=2),
            encoding="utf-8")
        (ev_dir / "messages.json").write_text(
            json.dumps(redact_mapping(messages, secrets), indent=2), encoding="utf-8")
        (ev_dir / "run.log").write_text(
            "\n".join(redact_mapping({"l": line}, secrets)["l"] for line in log_lines) + "\n",
            encoding="utf-8")

        # a full-fidelity copy for build tooling (e.g. scripts/build_artifact.py's
        # transcript reuse) that needs the real sample values a checkpoint is
        # built from. Gitignored, never evidence -- the redacted transcript.json
        # above is what ships.
        cache_dir = Path(".cache/discovery")
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{run_id}.json").write_text(
            transcript.model_dump_json(indent=2), encoding="utf-8")

        session.close()

    return transcript
