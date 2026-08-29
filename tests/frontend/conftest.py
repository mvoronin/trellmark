import pytest
from playwright.sync_api import expect, sync_playwright

from tests.postgres import TEST_LOGIN, TEST_PASSWORD


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def unauthenticated_page(browser):
    page = browser.new_page()
    yield page
    page.close()


@pytest.fixture
def page(unauthenticated_page, app):
    base_url, _ = app
    unauthenticated_page.goto(base_url)
    unauthenticated_page.get_by_label("Login").fill(TEST_LOGIN)
    unauthenticated_page.get_by_label("Password").fill(TEST_PASSWORD)
    unauthenticated_page.get_by_role("button", name="Log in").click()
    expect(unauthenticated_page.locator("#app-view")).to_be_visible()
    return unauthenticated_page
