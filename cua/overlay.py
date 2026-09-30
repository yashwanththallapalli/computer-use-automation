"""Per-tenant specialisation of a base capability artifact.

Hundreds of tenants run the same vendor products, configured and branded
differently. Re-recording a capability per tenant does not scale. Instead a
*base* artifact (``target.tenant_id is None``) is the vendor-generic recording,
and each tenant that genuinely differs carries a small ``TenantOverlay`` -- a
reviewable diff, not a fork.

``resolve_effective_artifact(base, overlay)`` produces the artifact the replay
engine actually runs. Only four things vary in practice:

* where the app lives and which version it is (``target_overrides``),
* which hosts replay is allowed to touch (``policy_overrides``) -- kept
  separate from ``target_overrides`` on purpose. A tenant on a new host needs
  its own allowlist entry regardless of whether anything else about it
  changed, and that entry has to be something a reviewer actually sees, not a
  pattern derived automatically from ``base_url`` and trusted blind. Getting
  an allowlist wrong is a security mistake, not a convenience gap,
* how a handful of controls are identified (``locator_overrides`` -- extra
  strategies are *prepended* so the base chain stays as fallback),
* the odd tenant-only or skipped step (``disabled_step_ids`` / ``extra_steps``).

Drift detection is the other half (see REPORT): replay records which strategy in
each chain resolved. If a tenant consistently falls through to a fallback, that
is the signal to author an overlay or re-record -- the base is not silently
rotting.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .schema import CapabilityArtifact, LocatorStrategy, Step


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetOverrides(_Base):
    base_url: str | None = None
    app_version: str | None = None
    entry_path: str | None = None


class PolicyOverrides(_Base):
    allowed_url_patterns: list[str] | None = Field(
        default=None,
        description="Full replacement for the base policy's allowed_url_patterns, "
        "not a merge. Required in practice whenever target_overrides.base_url "
        "points at a different host -- the base artifact's own patterns are "
        "anchored to its own host and won't match a new one.",
    )


class TenantOverlay(_Base):
    capability_id: str
    base_version: int = Field(description="The base artifact version this "
                              "overlay was written against.")
    tenant_id: str

    target_overrides: TargetOverrides = Field(default_factory=TargetOverrides)
    policy_overrides: PolicyOverrides = Field(default_factory=PolicyOverrides)
    locator_overrides: dict[str, list[LocatorStrategy]] = Field(
        default_factory=dict,
        description="step_id -> strategies to PREPEND to that step's locator "
        "chain. The base strategies remain as fallback.",
    )
    disabled_step_ids: list[str] = Field(default_factory=list)
    extra_steps: list[Step] = Field(
        default_factory=list,
        description="Tenant-only steps, each with `after_step_hint` in its "
        "description; inserted after the step whose id matches its own id "
        "prefix is out of scope -- kept minimal on purpose.",
    )
    notes: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> "TenantOverlay":
        return cls.model_validate(json.loads(Path(path).read_text("utf-8")))


def resolve_effective_artifact(
    base: CapabilityArtifact, overlay: TenantOverlay | None
) -> CapabilityArtifact:
    """Merge an overlay onto a base artifact. Returns a new artifact; inputs are
    not mutated."""

    if overlay is None:
        return base

    if overlay.capability_id != base.capability_id:
        raise ValueError(
            f"overlay is for {overlay.capability_id!r}, base is "
            f"{base.capability_id!r}"
        )
    if overlay.base_version != base.version:
        raise ValueError(
            f"overlay written against v{overlay.base_version}, base is "
            f"v{base.version} -- re-review the overlay before use"
        )

    data = base.model_dump()

    to = overlay.target_overrides
    if to.base_url is not None:
        data["target"]["base_url"] = to.base_url
    if to.app_version is not None:
        data["target"]["app_version"] = to.app_version
    if to.entry_path is not None:
        data["target"]["entry_path"] = to.entry_path
    data["target"]["tenant_id"] = overlay.tenant_id

    if overlay.policy_overrides.allowed_url_patterns is not None:
        data["policy"]["allowed_url_patterns"] = overlay.policy_overrides.allowed_url_patterns

    kept_steps = []
    for step in data["steps"]:
        if step["id"] in overlay.disabled_step_ids:
            continue
        prepend = overlay.locator_overrides.get(step["id"])
        if prepend:
            if step.get("target") is None:
                raise ValueError(
                    f"locator_override for step {step['id']!r} which has no target"
                )
            step["target"]["strategies"] = (
                [s.model_dump() for s in prepend] + step["target"]["strategies"]
            )
        kept_steps.append(step)
    for extra in overlay.extra_steps:
        kept_steps.append(extra.model_dump())
    data["steps"] = kept_steps

    # drop outputs whose producing step was disabled
    if overlay.disabled_step_ids:
        data["outputs"] = [
            o for o in data["outputs"]
            if o["source_step_id"] not in overlay.disabled_step_ids
        ]

    data.setdefault("metadata", {})
    note = f"[overlay:{overlay.tenant_id}] " + (overlay.notes or "applied")
    data["metadata"]["notes"] = (
        (data["metadata"].get("notes") or "") + " " + note
    ).strip()

    return CapabilityArtifact.model_validate(data)
