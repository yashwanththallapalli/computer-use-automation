"""Cross-tenant reuse, proven end to end rather than just designed.

The base read_member_balance artifact was recorded against Meridian. Harborview
runs the same underlying product -- same routes, same data -- but its own
build renames the row action and adds a leading column, which is enough to
break the base artifact's locator chain for one step. Three stages:

1. the base artifact, unmodified, doesn't even get to touch Harborview: its
   own allowlist is still anchored to Meridian's host, and guardrails catch
   that before a single request goes out;
2. re-authorize the host (what a tenant overlay's target/policy overrides are
   for) but withhold the locator fix -- the run reaches Harborview's search
   results just fine and then fails cleanly at the one step that actually
   differs;
3. the checked-in overlay (cua/capabilities/overlays/read_member_balance.
   harborview.v1.json) adds one prepended locator strategy for that step and
   the whole thing replays end to end, with every other step still resolving
   on the base chain's own primary strategy.

See REPORT.md's Heterogeneity section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua._appserver import running_bankapp
from cua.overlay import TenantOverlay
from cua.replay import ReplayOptions, replay
from cua.schema import CapabilityArtifact

CAP_DIR = Path(__file__).parent.parent / "cua" / "capabilities"
OVERLAY_PATH = CAP_DIR / "overlays" / "read_member_balance.harborview.v1.json"
TENANT_BASE_URL = "http://127.0.0.1:5002"  # matches the checked-in overlay and the README demo
INPUTS = {"member_id": "100234", "account_type": "Savings"}


@pytest.fixture(scope="module")
def harborview_app():
    with running_bankapp(base_url=TENANT_BASE_URL,
                         env_extra={"BANKAPP_TENANT": "harborview"}) as base:
        yield base


@pytest.fixture
def base_artifact():
    return CapabilityArtifact.load(CAP_DIR / "read_member_balance.v1.json")


@pytest.fixture
def overlay():
    ov = TenantOverlay.load(OVERLAY_PATH)
    assert ov.target_overrides.base_url == TENANT_BASE_URL, \
        "test and checked-in overlay have drifted apart on the tenant's port"
    return ov


def test_unmodified_base_is_blocked_before_it_touches_the_tenant(
    harborview_app, base_artifact, tmp_path
):
    art = base_artifact.model_copy(deep=True)
    art.target.base_url = TENANT_BASE_URL  # policy.allowed_url_patterns not updated
    result = replay(art, INPUTS, ReplayOptions(evidence_root=tmp_path))
    assert result.status.value == "failure"
    assert result.failure_code == "BLOCKED_URL"
    assert result.context.steps_executed == 0


def test_reauthorized_host_still_hits_the_real_locator_gap(
    harborview_app, base_artifact, overlay, tmp_path
):
    partial = overlay.model_copy(update={"locator_overrides": {}})
    result = replay(base_artifact, INPUTS,
                    ReplayOptions(tenant_overlay=partial, evidence_root=tmp_path))
    assert result.status.value == "failure"
    assert result.failure_code == "LOCATOR_UNRESOLVED"
    assert result.failed_step_id == "click_open"


def test_overlay_fixes_it_and_replays_end_to_end(
    harborview_app, base_artifact, overlay, tmp_path
):
    result = replay(base_artifact, INPUTS,
                    ReplayOptions(tenant_overlay=overlay, evidence_root=tmp_path))
    assert result.status.value == "success"
    assert result.outputs["balance"] == "4210.55"
    assert result.outputs["sub_account_number"] == "100234-01"

    trace = {t.step_id: t.locator_used for t in result.context.trace}
    # the one step the overlay actually touched resolves on its prepended
    # strategy; every other step is untouched and still resolves on the base
    # chain's own primary strategy -- the overlay is a small diff, not a fork
    assert trace["click_open"] == "near_text#0"
    assert trace["enter_member_name"].endswith("#0")
    assert trace["submit_search"].endswith("#0")
    assert trace["read_balance"].endswith("#0")
