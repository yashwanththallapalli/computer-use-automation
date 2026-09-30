"""The tool surface the discovery agent acts through, and the executor that
applies a tool call to the live page.

Tools (one action per turn): ``navigate``, ``click``, ``type_text``,
``select_option``, ``set_checkbox``, ``extract_value``, ``finish``. Every acting
tool takes a ``ref`` (from the observation's element list) and a ``note`` -- the
model's own statement of *what* the control is and *why* it's acting -- which
becomes the step description and the locator ``intent`` in Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PWError

from .guardrails import Guardrail
from .perception import Observation
from .transcript import CapturedElement

_REF = {"type": "integer", "description": "The [n] ref of the target element "
        "from the current observation."}
_NOTE = {"type": "string", "description": "One sentence: what this element is "
         "(by its visible label) and why you are acting on it."}


def _tool(name: str, desc: str, props: dict, required: list[str],
          strict: bool = True) -> dict:
    schema: dict[str, Any] = {
        "name": name,
        "description": desc,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }
    if strict:
        schema["strict"] = True
    return schema


TOOLS: list[dict] = [
    _tool("navigate", "Load a URL or path in the browser.",
          {"url": {"type": "string"}, "note": _NOTE}, ["url", "note"]),
    _tool("click", "Click a link or button.",
          {"ref": _REF, "note": _NOTE}, ["ref", "note"]),
    _tool("type_text", "Type text into a text field (replaces existing value).",
          {"ref": _REF, "text": {"type": "string"}, "note": _NOTE},
          ["ref", "text", "note"]),
    _tool("select_option", "Choose an option in a dropdown by its visible label.",
          {"ref": _REF, "option": {"type": "string"}, "note": _NOTE},
          ["ref", "option", "note"]),
    _tool("set_checkbox", "Check or uncheck a checkbox.",
          {"ref": _REF, "checked": {"type": "boolean"}, "note": _NOTE},
          ["ref", "checked", "note"]),
    _tool("extract_value", "Read a value off the page and record it as a named "
          "output of the capability.",
          {"ref": _REF, "output_name": {"type": "string",
           "description": "snake_case name for this output."}, "note": _NOTE},
          ["ref", "output_name", "note"]),
    _tool("finish", "End the run. Use status 'goal_reached' when the goal's "
          "success condition is visibly met, 'business_outcome' when the app "
          "gave a legitimate terminal result (member not found, permission "
          "denied, validation error), or 'stuck' when you cannot proceed.",
          {"status": {"type": "string",
                      "enum": ["goal_reached", "business_outcome", "stuck"]},
           "summary": {"type": "string"},
           "outputs": {"type": "string", "description": "JSON object of the "
                       "declared outputs and their values, or '{}'."}},
          ["status", "summary", "outputs"], strict=False),
]


@dataclass
class ActionOutcome:
    ok: bool
    detail: str
    captured_element: CapturedElement | None = None
    extracted: dict[str, str] = field(default_factory=dict)
    is_finish: bool = False
    finish_status: str | None = None
    finish_summary: str = ""
    finish_outputs: dict[str, Any] = field(default_factory=dict)


class ActionExecutor:
    def __init__(self, session, guardrail: Guardrail, evidence_dir: Path):
        self.session = session
        self.guard = guardrail
        self.evidence_dir = evidence_dir

    # -- helpers ---------------------------------------------------------

    def _locator(self, ref: int):
        return self.session.page.locator(f'[data-cua-ref="{ref}"]')

    @staticmethod
    def _capture(obs: Observation, ref: int) -> CapturedElement:
        info = obs.any_ref(ref)
        data = info.model_dump()
        return CapturedElement(**{k: v for k, v in data.items()
                                  if k in CapturedElement.model_fields})

    def _settle(self) -> None:
        try:
            self.session.page.wait_for_load_state("domcontentloaded", timeout=6000)
        except PWError:
            pass

    # -- dispatch ------------------------------------------------------

    def execute(self, name: str, args: dict, obs: Observation) -> ActionOutcome:
        try:
            handler = getattr(self, f"_do_{name}")
        except AttributeError:
            return ActionOutcome(False, f"unknown tool {name!r}")
        try:
            return handler(args, obs)
        except KeyError as ex:
            return ActionOutcome(False, f"bad element ref: {ex}")
        except PWError as ex:
            return ActionOutcome(False, f"playwright error: {str(ex).splitlines()[0]}")

    def _do_navigate(self, args, obs) -> ActionOutcome:
        url = args["url"]
        target = url if "://" in url else self.session.base_url.rstrip("/") + "/" + url.lstrip("/")
        d = self.guard.check_navigation(target)
        if not d:
            return ActionOutcome(False, f"BLOCKED: {d.reason}")
        self.session.goto(url)
        self._settle()
        return ActionOutcome(True, f"navigated to {self.session.page.url}")

    def _do_click(self, args, obs) -> ActionOutcome:
        d = self.guard.check_action("click")
        if not d:
            return ActionOutcome(False, f"BLOCKED: {d.reason}")
        cap = self._capture(obs, args["ref"])
        self._locator(args["ref"]).click()
        self._settle()
        nav = self.guard.check_navigation(self.session.page.url)
        if not nav:
            return ActionOutcome(False, f"BLOCKED after navigation: {nav.reason}",
                                 captured_element=cap)
        return ActionOutcome(True, f"clicked; now at {self.session.page.url}",
                             captured_element=cap)

    def _do_type_text(self, args, obs) -> ActionOutcome:
        d = self.guard.check_action("type_text")
        if not d:
            return ActionOutcome(False, f"BLOCKED: {d.reason}")
        cap = self._capture(obs, args["ref"])
        loc = self._locator(args["ref"])
        loc.fill(args["text"])
        return ActionOutcome(True, f"typed into {cap.role} (near {cap.near!r})",
                             captured_element=cap)

    def _do_select_option(self, args, obs) -> ActionOutcome:
        d = self.guard.check_action("select_option")
        if not d:
            return ActionOutcome(False, f"BLOCKED: {d.reason}")
        cap = self._capture(obs, args["ref"])
        self._locator(args["ref"]).select_option(label=args["option"])
        return ActionOutcome(True, f"selected {args['option']!r}",
                             captured_element=cap)

    def _do_set_checkbox(self, args, obs) -> ActionOutcome:
        d = self.guard.check_action("set_checkbox")
        if not d:
            return ActionOutcome(False, f"BLOCKED: {d.reason}")
        cap = self._capture(obs, args["ref"])
        loc = self._locator(args["ref"])
        loc.set_checked(bool(args["checked"]))
        return ActionOutcome(True, f"set checkbox to {bool(args['checked'])}",
                             captured_element=cap)

    def _do_extract_value(self, args, obs) -> ActionOutcome:
        d = self.guard.check_action("extract_value")
        if not d:
            return ActionOutcome(False, f"BLOCKED: {d.reason}")
        cap = self._capture(obs, args["ref"])
        text = (self._locator(args["ref"]).inner_text() or "").strip()
        name = args["output_name"]
        return ActionOutcome(True, f"extracted {name}={text!r}",
                             captured_element=cap, extracted={name: text})

    def _do_finish(self, args, obs) -> ActionOutcome:
        import json
        raw = args.get("outputs") or "{}"
        try:
            outputs = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            outputs = {"_raw": raw}
        return ActionOutcome(
            True, f"finished: {args['status']}",
            is_finish=True, finish_status=args["status"],
            finish_summary=args.get("summary", ""), finish_outputs=outputs,
        )
