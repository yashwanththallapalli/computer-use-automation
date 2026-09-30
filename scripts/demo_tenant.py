"""Cross-tenant reuse demo: the base read_member_balance artifact, recorded
against Meridian, replayed against a second, differently-built tenant
(Harborview) three ways -- unmodified, re-authorized but without the locator
fix, and with the checked-in overlay. See REPORT.md's Heterogeneity section
and cua/capabilities/overlays/read_member_balance.harborview.v1.json.

    python scripts/demo_tenant.py

Starts the mock bank app a second time, configured as the Harborview tenant,
on port 5002 (the base instance on 5001, if any, is untouched). Writes each
stage's evidence under evidence/tenant_harborview/.
"""

from __future__ import annotations

import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from cua._appserver import running_bankapp
from cua.overlay import TenantOverlay
from cua.replay import ReplayOptions, replay
from cua.schema import CapabilityArtifact

CAP_DIR = Path("cua/capabilities")
OVERLAY_PATH = CAP_DIR / "overlays" / "read_member_balance.harborview.v1.json"
TENANT_BASE_URL = "http://127.0.0.1:5002"
EVIDENCE_ROOT = Path("evidence/tenant_harborview")
INPUTS = {"member_id": "100234", "account_type": "Savings"}


def _report(label: str, result) -> None:
    print(f"\n=== {label} ===")
    print(f"status  : {result.status.value}")
    if result.status.value == "success":
        print(f"outputs : {result.outputs}")
    else:
        print(f"code    : {result.failure_code}")
        print(f"step    : {result.failed_step_id}")
        print(f"observed: {result.observed}")
    print(f"evidence: {result.context.evidence_dir}")


def main() -> int:
    base_artifact = CapabilityArtifact.load(CAP_DIR / "read_member_balance.v1.json")
    overlay = TenantOverlay.load(OVERLAY_PATH)
    if overlay.target_overrides.base_url != TENANT_BASE_URL:
        print(f"overlay base_url {overlay.target_overrides.base_url!r} != "
              f"{TENANT_BASE_URL!r} -- fix one of them", file=sys.stderr)
        return 2

    with running_bankapp(base_url=TENANT_BASE_URL,
                         env_extra={"BANKAPP_TENANT": "harborview"}):
        # 1. unmodified base artifact -- still allowlists only Meridian's host
        unmodified = base_artifact.model_copy(deep=True)
        unmodified.target.base_url = TENANT_BASE_URL
        r1 = replay(unmodified, INPUTS, ReplayOptions(evidence_root=EVIDENCE_ROOT))
        _report("1. unmodified base -> blocked before it touches the tenant", r1)
        assert r1.status.value == "failure" and r1.failure_code == "BLOCKED_URL"

        # 2. re-authorize the host, withhold the locator fix
        partial = overlay.model_copy(update={"locator_overrides": {}})
        r2 = replay(base_artifact, INPUTS,
                    ReplayOptions(tenant_overlay=partial, evidence_root=EVIDENCE_ROOT))
        _report("2. re-authorized host -> real locator gap at click_open", r2)
        assert r2.status.value == "failure" and r2.failure_code == "LOCATOR_UNRESOLVED"

        # 3. the checked-in overlay
        r3 = replay(base_artifact, INPUTS,
                    ReplayOptions(tenant_overlay=overlay, evidence_root=EVIDENCE_ROOT))
        _report("3. overlay applied -> replays end to end", r3)
        assert r3.status.value == "success"

    print(f"\nAll three stages behaved as expected. Evidence under {EVIDENCE_ROOT}/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
