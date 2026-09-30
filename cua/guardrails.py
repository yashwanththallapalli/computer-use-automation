"""Guardrails enforced before every action, in both the discovery loop and the
replay engine.

Two layers:

* an **allowlist** -- configurable, per capability/run (URL patterns + action
  types). This is what "the agent may operate under 127.0.0.1:5001/members/*"
  means in practice.
* a **denylist** -- a small hardcoded set of destructive routes (the
  ``/admin/reset`` test fixture) that no allowlist configuration can open back
  up. Defense in depth: a too-broad allow pattern is a config mistake; a
  denylist hit is a bug worth failing loudly on.

The risky-action gate (``allow_risky``) is the seam Phase 8 hangs its
human-approval flow off of: a ``CONFIRM`` step, or any step id the artifact
lists in ``policy.risky_step_ids``, is blocked unless the caller explicitly
sets ``allow_risky=True``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RISKY_ACTIONS = {"confirm"}

# Never reachable regardless of allowlist config.
DEFAULT_DENYLIST_PATTERNS = [r"/admin(/|$)"]


@dataclass
class GuardrailDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # so callers can `if decision:`
        return self.allowed


@dataclass
class Guardrail:
    allowed_url_patterns: list[str] = field(default_factory=list)
    allowed_actions: set[str] = field(default_factory=set)
    denylist_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_DENYLIST_PATTERNS)
    )
    # discovery runs and explicitly-approved replays may take irreversible steps
    allow_risky: bool = False
    # step ids the artifact marks irreversible (replay); names for discovery
    risky_step_ids: set[str] = field(default_factory=set)

    _allow: list[re.Pattern[str]] = field(default_factory=list, init=False)
    _deny: list[re.Pattern[str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._allow = [re.compile(p) for p in self.allowed_url_patterns]
        self._deny = [re.compile(p) for p in self.denylist_patterns]

    def check_navigation(self, url: str) -> GuardrailDecision:
        hit = next((rx for rx in self._deny if rx.search(url)), None)
        if hit is not None:
            return GuardrailDecision(
                False, f"URL {url!r} matches the denylist ({hit.pattern!r}); "
                "this route can never be reached by automation")
        if not self._allow:
            return GuardrailDecision(True)
        if any(rx.search(url) for rx in self._allow):
            return GuardrailDecision(True)
        return GuardrailDecision(
            False,
            f"URL {url!r} is outside the allowlist "
            f"({', '.join(self.allowed_url_patterns)})",
        )

    def check_action(self, action_type: str, *, step_id: str | None = None) -> GuardrailDecision:
        if self.allowed_actions and action_type not in self.allowed_actions:
            return GuardrailDecision(
                False, f"action {action_type!r} is not in the allowed set"
            )
        is_risky = action_type in RISKY_ACTIONS or (
            step_id is not None and step_id in self.risky_step_ids
        )
        if is_risky and not self.allow_risky:
            return GuardrailDecision(
                False,
                f"action {action_type!r}"
                + (f" (step {step_id!r})" if step_id else "")
                + " is irreversible and requires explicit approval",
            )
        return GuardrailDecision(True)
