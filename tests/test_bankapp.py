"""Smoke tests for the mock bank app: the happy path plus both business/error
surfaces the automation must detect."""

import pytest

from bankapp import create_app
from bankapp.data import store


@pytest.fixture()
def client():
    app = create_app()
    app.config.update(TESTING=True)
    store.reset()
    with app.test_client() as c:
        yield c
    store.reset()


@pytest.fixture()
def interstitial_client(monkeypatch):
    monkeypatch.setenv("BANKAPP_INTERSTITIAL", "1")
    app = create_app()
    app.config.update(TESTING=True)
    store.reset()
    with app.test_client() as c:
        yield c
    store.reset()


@pytest.fixture()
def unexpected_notice_client(monkeypatch):
    monkeypatch.setenv("BANKAPP_UNEXPECTED_NOTICE", "1")
    app = create_app()
    app.config.update(TESTING=True)
    store.reset()
    with app.test_client() as c:
        yield c
    store.reset()


def test_search_by_name_returns_matches(client):
    resp = client.get("/members/search?q=thornton")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Alice Thornton" in body
    assert "James Thornton" in body


def test_search_by_member_number_exact(client):
    resp = client.get("/members/search?q=100987")
    body = resp.get_data(as_text=True)
    assert "Robert Chen" in body


def test_member_not_found_panel(client):
    resp = client.get("/members/search?q=nobody-here")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "No members match your search." in body


def test_member_detail_and_missing(client):
    ok = client.get("/members/100234")
    assert "Alice Thornton" in ok.get_data(as_text=True)

    missing = client.get("/members/999999")
    assert missing.status_code == 404
    assert "does not exist" in missing.get_data(as_text=True)


def test_open_sub_account_happy_path(client):
    resp = client.post(
        "/members/101445/sub-accounts",
        data={
            "account_type": "Savings",
            "initial_deposit": "100.00",
            "authorization_confirmed": "on",
        },
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "opened successfully" in body
    assert "101445-01" in body

    detail = client.get("/members/101445").get_data(as_text=True)
    assert "101445-01" in detail


def test_open_sub_account_validation_errors(client):
    resp = client.post(
        "/members/100234/sub-accounts",
        data={
            "account_type": "",
            "initial_deposit": "5",
            "authorization_confirmed": "",
        },
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Account type is required." in body
    assert "Minimum opening deposit is $25.00." in body
    assert "You must confirm the member authorized this action." in body


def test_permission_denied_for_dormant_member(client):
    # 100987 Robert Chen is Dormant -> the open-sub-account form is refused
    form = client.get("/members/100987/sub-accounts/new")
    body = form.get_data(as_text=True)
    assert form.status_code == 200
    assert "cannot be opened for this member" in body
    assert "supervisor override" in body

    posted = client.post(
        "/members/100987/sub-accounts",
        data={
            "account_type": "Savings",
            "initial_deposit": "100",
            "authorization_confirmed": "on",
        },
    )
    assert posted.status_code == 200
    assert "cannot be opened for this member" in posted.get_data(as_text=True)


def test_interstitial_gates_member_detail_and_dismisses(interstitial_client):
    gated = interstitial_client.get("/members/100234")
    body = gated.get_data(as_text=True)
    assert "System Notice" in body
    assert "Scheduled maintenance window" in body
    assert "Alice Thornton" not in body

    dismissed = interstitial_client.get("/members/100234?ack=1")
    assert "Alice Thornton" in dismissed.get_data(as_text=True)


def test_unexpected_notice_gates_member_detail_and_dismisses(unexpected_notice_client):
    gated = unexpected_notice_client.get("/members/100234")
    body = gated.get_data(as_text=True)
    assert "Browser Compatibility Notice" in body
    assert "Alice Thornton" not in body

    dismissed = unexpected_notice_client.get("/members/100234?notice_dismissed=1")
    assert "Alice Thornton" in dismissed.get_data(as_text=True)


def test_no_interstitial_by_default(client):
    resp = client.get("/members/100234")
    assert "System Notice" not in resp.get_data(as_text=True)
    assert "Alice Thornton" in resp.get_data(as_text=True)


def test_admin_reset_restores_seed(client):
    client.post(
        "/members/101445/sub-accounts",
        data={
            "account_type": "Checking",
            "initial_deposit": "50",
            "authorization_confirmed": "on",
        },
    )
    assert "101445-01" in client.get("/members/101445").get_data(as_text=True)

    client.post("/admin/reset")
    assert "No sub-accounts on file." in client.get("/members/101445").get_data(as_text=True)
