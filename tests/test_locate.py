"""Locator resolution tests -- these pin down bugs found while building
replay: the page-layout table must never be mistaken for a data row/cell, and
{param} placeholders must resolve inside nested locator options, not just the
top-level value."""

import pytest

from cua._appserver import running_bankapp
from cua.browser import BrowserSession
from cua.locate import Resolver
from cua.schema import Locator, LocatorKind, LocatorStrategy


@pytest.fixture(scope="module")
def page():
    with running_bankapp() as base:
        with BrowserSession(base_url=base, headless=True) as session:
            yield session.page


def _near_text(anchor):
    return Locator(intent=anchor, strategies=[LocatorStrategy(
        kind=LocatorKind.NEAR_TEXT, value=anchor, options={"anchor": anchor})])


def test_near_text_finds_the_field_not_the_page_wrapper(page):
    # regression: the outer content <td> "contains" every label's text too
    # (textContent includes descendants), and used to win as the match
    page.goto("http://127.0.0.1:5001/members/101445/sub-accounts/new")
    loc, tag = Resolver(page).resolve(_near_text("Initial Deposit (USD):"), {})
    assert tag == "near_text#0"
    assert loc.evaluate("el => el.tagName") == "INPUT"
    assert loc.get_attribute("name") == "initial_deposit"


def test_near_text_reads_a_plain_value_cell(page):
    # near_text also has to work when there's no form control -- just text
    page.goto("http://127.0.0.1:5001/members/100234")
    loc, tag = Resolver(page).resolve(_near_text("Email"), {})
    assert tag == "near_text#0"
    assert "alice.thornton" in loc.inner_text()


def test_row_scoped_near_text_picks_the_right_row(page):
    page.goto("http://127.0.0.1:5001/members/search?q=thornton")  # 2 rows
    loc_strat = LocatorStrategy(
        kind=LocatorKind.NEAR_TEXT, value="Open",
        options={"row_contains": "{member_id}", "target_text": "Open"})
    locator = Locator(intent="open row", strategies=[loc_strat])
    loc, tag = Resolver(page).resolve(locator, {"member_id": "102310"})
    assert tag == "near_text#0"
    assert loc.get_attribute("href") == "/members/102310"


def test_table_cell_resolves_by_column_and_row(page):
    page.goto("http://127.0.0.1:5001/members/100234")
    strat = LocatorStrategy(
        kind=LocatorKind.TABLE_CELL, value="Balance",
        options={"column": "Balance", "row_contains": ["{account_type}"]})
    locator = Locator(intent="balance cell", strategies=[strat])
    loc, tag = Resolver(page).resolve(locator, {"account_type": "Checking"})
    assert tag == "table_cell#0"
    assert loc.inner_text().strip() == "1875.10"


def test_table_cell_key_value_grid(page):
    page.goto("http://127.0.0.1:5001/members/100234")
    strat = LocatorStrategy(kind=LocatorKind.TABLE_CELL, value="Status",
                            options={"column": "Status"})
    locator = Locator(intent="member status", strategies=[strat])
    loc, tag = Resolver(page).resolve(locator, {})
    assert tag == "table_cell#0"
    assert loc.inner_text().strip() == "Active"


def test_role_and_css_strategies_still_work(page):
    page.goto("http://127.0.0.1:5001/members/search")
    role = Locator(intent="search button", strategies=[
        LocatorStrategy(kind=LocatorKind.ROLE, value="button", options={"name": "Search"})])
    loc, tag = Resolver(page).resolve(role, {})
    assert tag == "role#0"

    css = Locator(intent="search box", strategies=[
        LocatorStrategy(kind=LocatorKind.CSS, value="input[name='q']")])
    loc, tag = Resolver(page).resolve(css, {})
    assert tag == "css#0"


def test_no_match_returns_none(page):
    page.goto("http://127.0.0.1:5001/members/search")
    loc, tag = Resolver(page).resolve(_near_text("Nonexistent Field Label"), {})
    assert loc is None and tag == ""


def test_fallback_chain_tries_next_strategy_on_miss(page):
    page.goto("http://127.0.0.1:5001/members/100234")
    locator = Locator(intent="balance", strategies=[
        LocatorStrategy(kind=LocatorKind.CSS, value="#does-not-exist"),
        LocatorStrategy(kind=LocatorKind.TABLE_CELL, value="Balance",
                        options={"column": "Balance", "row_contains": ["Savings"]}),
    ])
    loc, tag = Resolver(page).resolve(locator, {})
    assert tag == "table_cell#1"
    assert loc.inner_text().strip() == "4210.55"
