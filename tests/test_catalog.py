"""Capability catalog tests. TestClient triggers the real FastAPI lifespan
(the mock app starts for real) and /invoke drives a real browser through
cua.replay -- nothing here is mocked, just run in-process instead of over a
socket."""

import pytest
from fastapi.testclient import TestClient

from cua.catalog import app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # keep invoke()'s real replay evidence out of the published evidence/
    # directory -- these are test runs, not curated demo evidence
    app.state.evidence_root = tmp_path_factory.mktemp("catalog_evidence")
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert {"open_subaccount", "read_member_balance"} <= set(body["capabilities"])


def test_list_capabilities_exposes_contract_not_implementation(client):
    r = client.get("/capabilities")
    assert r.status_code == 200
    by_id = {c["capability_id"]: c for c in r.json()}
    assert by_id["open_subaccount"]["requires_approval_for_risky_steps"] is True
    assert by_id["read_member_balance"]["requires_approval_for_risky_steps"] is False
    # the contract never leaks steps/locators -- those aren't in the model at all
    assert "steps" not in by_id["open_subaccount"]
    assert "target" not in by_id["open_subaccount"]


def test_get_capability_card_has_typed_io(client):
    r = client.get("/capabilities/read_member_balance")
    assert r.status_code == 200
    card = r.json()
    names = {p["name"] for p in card["input_params"]}
    assert names == {"member_id", "account_type"}
    out_names = {o["name"] for o in card["outputs"]}
    assert "balance" in out_names
    balance = next(o for o in card["outputs"] if o["name"] == "balance")
    assert balance["sensitive"] is True


def test_unknown_capability_is_404(client):
    r = client.get("/capabilities/does_not_exist")
    assert r.status_code == 404


def test_tool_schema_is_a_valid_anthropic_style_tool(client):
    r = client.get("/capabilities/read_member_balance/tool_schema")
    assert r.status_code == 200
    tool = r.json()
    assert tool["name"] == "read_member_balance"
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["member_id", "account_type"]
    assert schema["properties"]["account_type"]["enum"] == [
        "Savings", "Checking", "Money Market", "Certificate of Deposit"]
    assert schema["additionalProperties"] is False


def test_invoke_read_only_capability_end_to_end(client):
    r = client.post("/capabilities/read_member_balance/invoke", json={
        "inputs": {"member_id": "100234", "account_type": "Savings"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["outputs"]["balance"] == "4210.55"
    assert body["outputs"]["sub_account_number"] == "100234-01"


def test_invoke_reports_business_outcome_not_an_error(client):
    r = client.post("/capabilities/read_member_balance/invoke", json={
        "inputs": {"member_id": "999999", "account_type": "Savings"},
    })
    assert r.status_code == 200  # a business outcome is not an HTTP error
    body = r.json()
    assert body["status"] == "business_outcome"
    assert body["outcome_code"] == "MEMBER_NOT_FOUND"


def test_invoke_risky_capability_without_approval_fails_cleanly(client):
    r = client.post("/capabilities/open_subaccount/invoke", json={
        "inputs": {"member_id": "101445", "account_type": "Savings",
                  "initial_deposit": "250.00", "member_authorized": "yes"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failure"
    assert body["failure_code"] == "APPROVAL_REQUIRED"


def test_invoke_risky_capability_with_approval_succeeds(client):
    r = client.post("/capabilities/open_subaccount/invoke", json={
        "inputs": {"member_id": "101445", "account_type": "Checking",
                  "initial_deposit": "500.00", "member_authorized": "yes"},
        "approve_risky": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["outputs"]["sub_account_number"].startswith("101445-")
