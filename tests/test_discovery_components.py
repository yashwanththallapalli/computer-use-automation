"""Unit tests for the discovery-loop building blocks (no LLM, no network for
the redaction/guardrail/schema tests; the perception tests drive a real browser
against the mock app)."""

import json

import pytest

from cua.agent_tools import TOOLS
from cua.guardrails import Guardrail
from cua.redaction import mask_value, redact, redact_mapping


# --- redaction ----------------------------------------------------------

def test_redact_masks_pii_patterns():
    s = "email alice@example.com phone 555-0142 dob 1985-03-12 ssn 111-22-3333"
    out = redact(s)
    for leaked in ("alice@example.com", "555-0142", "1985-03-12", "111-22-3333"):
        assert leaked not in out
    assert out.count("[REDACTED]") == 4


def test_redact_does_not_mangle_iso_timestamps():
    # regression: the DOB pattern (YYYY-MM-DD) also matched the date portion
    # of "started_at": "2026-09-11T12:41:25.599036Z", corrupting every
    # timestamp in transcript.json/result.json
    ts = "2026-09-11T12:41:25.599036Z"
    assert redact(f'"started_at": "{ts}"') == f'"started_at": "{ts}"'


def test_redact_masks_known_secrets_longest_first():
    assert redact("token=abcdef and ab", ["abcdef", "ab"]) == \
        "token=[REDACTED] and [REDACTED]"


def test_redact_mapping_is_deep_and_pure():
    data = {"a": ["x@y.com", {"b": "call 555-123-4567"}], "n": 3}
    out = redact_mapping(data)
    assert out["a"][0] == "[REDACTED]"
    assert out["a"][1]["b"] == "call [REDACTED]"
    assert out["n"] == 3
    assert data["a"][0] == "x@y.com"  # original untouched


def test_mask_value_keeps_shape_only():
    assert mask_value("4210.55") == "nnnn.nn"
    assert mask_value("101445-01") == "nnnnnn-nn"


# --- guardrails -------------------------------------------------------

def test_guardrail_url_allowlist():
    g = Guardrail(allowed_url_patterns=[r"^http://127\.0\.0\.1:5001/members"])
    assert g.check_navigation("http://127.0.0.1:5001/members/100234")
    assert not g.check_navigation("http://evil.example/x")
    assert not g.check_navigation("http://127.0.0.1:5001/admin/reset")


def test_guardrail_risky_action_gate():
    blocked = Guardrail(allow_risky=False)
    assert not blocked.check_action("confirm")
    assert blocked.check_action("click")

    approved = Guardrail(allow_risky=True)
    assert approved.check_action("confirm")

    by_id = Guardrail(allow_risky=False, risky_step_ids={"submit_open"})
    assert not by_id.check_action("click", step_id="submit_open")
    assert by_id.check_action("click", step_id="enter_member")


def test_guardrail_action_type_allowlist():
    g = Guardrail(allowed_actions={"click", "navigate"})
    assert g.check_action("click")
    assert not g.check_action("type_text")


# --- tool schemas ---------------------------------------------------

def test_tool_schemas_are_wellformed():
    names = {t["name"] for t in TOOLS}
    assert {"navigate", "click", "type_text", "select_option",
            "set_checkbox", "extract_value", "finish"} <= names
    for t in TOOLS:
        sch = t["input_schema"]
        assert sch["type"] == "object"
        if t.get("strict"):
            assert sch["additionalProperties"] is False
            assert set(sch["required"]) == set(sch["properties"])
    # a strict tool must be json-serialisable as an Anthropic tool
    json.dumps(TOOLS)


# --- perception (real browser + mock app) --------------------------

@pytest.fixture(scope="module")
def app_base():
    from cua._appserver import running_bankapp
    with running_bankapp() as base:
        yield base


@pytest.fixture(scope="module")
def browser(app_base):
    from cua.browser import BrowserSession
    with BrowserSession(base_url=app_base, headless=True) as s:
        yield s


def test_perceive_search_page(browser):
    from cua.perception import perceive
    browser.goto("/members/search")
    obs = perceive(browser.page)
    roles = {(e.role, e.name) for e in obs.elements}
    assert ("button", "Search") in roles
    box = next(e for e in obs.elements if e.role == "textbox")
    assert "Member name or number" in box.near


def test_perceive_member_detail_readables(browser):
    from cua.perception import perceive
    browser.goto("/members/100234")
    obs = perceive(browser.page)
    balance = next(
        (r for r in obs.readables
         if r.column == "Balance" and "Savings" in r.row_text), None)
    assert balance is not None and balance.name == "4210.55"
    # the key/value grid header resolves to the field label, not the value
    status = next(r for r in obs.readables if r.name == "Active")
    assert status.column == "Status"


def test_perceive_permission_denied_banner(browser):
    from cua.perception import perceive
    browser.goto("/members/100987/sub-accounts/new")  # Dormant member
    obs = perceive(browser.page)
    assert any("cannot be opened for this member" in b for b in obs.banners)
