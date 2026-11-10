"""The board page (templates/board.html) in a real headless Chromium: renders, images load, filters work.

Same Playwright/Chromium rules as test_contact_sheet_browser.py (skipped without them, unless
IMAGEGEN_REQUIRE_BROWSER=1).
"""
import pytest

import boards
import projects
from test_contact_sheet_browser import browser  # noqa: F401 - the shared module-scoped fixture


@pytest.fixture
def board(browser, out_dir, comfy):  # noqa: F811
    projects.new("fjord", "Fjord", "moody fjord", prompt="a fjord", palette=["#334455"])
    projects.run_pass("fjord", count=3, seed=1)
    projects.set_state("fjord", "less fog", "claude", prompt="a fjord, light mist")
    projects.run_pass("fjord", count=2, seed=10)
    boards.rate("fjord", ["1:1"], "reject", "fog")
    boards.rate("fjord", ["2:1"], "pick", "the light!")
    boards.rate("fjord", ["2:2"], "maybe")
    boards.note("fjord", "go warmer")
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(boards.page_file("fjord").as_uri())
    page.evaluate("document.querySelectorAll('img').forEach(i => { i.loading = 'eager'; })")  # load all now
    page.wait_for_function("[...document.images].every(i => i.complete)")
    yield page, errors
    page.close()


def visible_ids(page):
    return page.locator(".card:visible .id").all_inner_texts()


def test_renders_versions_passes_and_notes(board):
    page, errors = board
    assert page.locator("#title").inner_text() == "Fjord · v2"
    assert page.locator("section.version h2").all_inner_texts()[:2] == ["v2 · less fog", "v1 · created"]
    assert page.locator(".card").count() == 5
    assert page.evaluate("[...document.images].every(i => i.naturalWidth > 0)"), "an image failed to load"
    assert "go warmer" in page.locator("#board-notes").inner_text()
    assert "the light!" in page.locator(".card.pick").inner_text()
    assert "1 pick · 1 maybe · 1 reject · 2 unrated" in page.locator("#counts").inner_text()
    assert "null" not in page.locator("body").inner_text()  # optional parts are left out, not printed
    assert not errors, errors


def test_filters(board):
    page, errors = board
    assert visible_ids(page) == ["2:1", "2:2", "1:2", "1:3"]  # rejects hidden by default
    page.locator("[data-filter=pick]").click()
    assert visible_ids(page) == ["2:1"]
    page.locator("[data-filter=reject]").click()
    assert visible_ids(page) == ["1:1"]
    page.locator("[data-filter=all]").click()
    page.locator("#hide-rejects").uncheck()
    assert len(visible_ids(page)) == 5
    assert not errors, errors
