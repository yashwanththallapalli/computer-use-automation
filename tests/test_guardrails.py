"""Guardrail tests: the denylist can't be configured away, and the config
file loads with sane defaults."""

from cua.config import load_guardrail_config
from cua.guardrails import Guardrail


def test_denylist_blocks_admin_even_with_a_broad_allowlist():
    g = Guardrail(allowed_url_patterns=[r"^http://127\.0\.0\.1:5001/"])
    assert g.check_navigation("http://127.0.0.1:5001/members/100234")
    denied = g.check_navigation("http://127.0.0.1:5001/admin/reset")
    assert not denied
    assert "denylist" in denied.reason


def test_denylist_applies_even_with_no_allowlist_configured():
    g = Guardrail()  # no allowed_url_patterns -> everything else is open
    assert g.check_navigation("http://anywhere.example/whatever")
    assert not g.check_navigation("http://127.0.0.1:5001/admin/reset")


def test_allowlist_scopes_to_the_capability_routes():
    g = Guardrail(allowed_url_patterns=[r"^http://127\.0\.0\.1:5001/members(/|$|\?)"])
    assert g.check_navigation("http://127.0.0.1:5001/members/search")
    assert not g.check_navigation("http://127.0.0.1:5001/some-other-page")
    assert not g.check_navigation("http://evil.example/members/100234")


def test_load_guardrail_config_has_the_expected_shape():
    cfg = load_guardrail_config()
    assert cfg["allowed_url_patterns"]
    assert any("admin" in p for p in cfg["denylist_patterns"])


def test_unwritable_config_path_falls_back_to_defaults(tmp_path):
    cfg = load_guardrail_config(tmp_path / "does_not_exist.json")
    assert cfg["allowed_url_patterns"]
    assert cfg["denylist_patterns"]
