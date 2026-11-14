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


def test_history_is_a_popup(board):
    page, errors = board
    assert not page.locator("#history").is_visible()
    assert page.locator("#history-open").inner_text() == "History (2)"
    page.locator("#history-open").click()
    assert page.locator("#history li").all_inner_texts()[0].startswith("v2 ")
    page.keyboard.press("Escape")
    assert not page.locator("#history").is_visible()
    page.keyboard.press("h")
    assert page.locator("#history").is_visible()
    page.locator("#history-close").click()
    assert not page.locator("#history").is_visible()
    assert not errors, errors


# ---------------------------------------------------------------- live (gen board serve, #55)

@pytest.fixture
def live(browser, out_dir, comfy):  # noqa: F811
    """The fjord board served by boardserve on a free port, opened in Chromium."""
    import threading

    import boardserve
    projects.new("fjord", "Fjord", "moody fjord", prompt="a fjord")
    projects.run_pass("fjord", count=3, seed=1)
    boards.rate("fjord", ["1:3"], "reject")
    jobs = boardserve.Jobs()
    srv = boardserve.make_server(0, jobs=jobs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to fetch" not in m.text
            and "ERR_CONNECTION_REFUSED" not in m.text else None)
    page.goto(f"http://127.0.0.1:{srv.server_port}/projects/fjord/")
    page.evaluate("document.querySelectorAll('img').forEach(i => { i.loading = 'eager'; })")
    page.wait_for_function("[...document.images].every(i => i.complete)")
    yield page, errors, srv, jobs
    page.close()
    srv.shutdown()
    srv.server_close()


def rating_of(pid):
    proj = projects.load("fjord")
    key = {v: k for k, v in boards.pass_ids(proj).items()}[pid]
    return boards.load("fjord")["images"].get(key, {}).get("rating")


def test_live_click_rates(live):
    page, errors, *_ = live
    assert page.evaluate("[...document.images].every(i => i.naturalWidth > 0)"), "an image failed to load over http"
    assert page.locator("#live-status").inner_text() == "live"
    assert page.locator("#gen-more").is_visible()
    page.locator(".card", has_text="1:1").get_by_role("button", name="pick").click()
    page.wait_for_selector(".card.pick")
    assert rating_of("1:1") == "pick"
    assert "1 pick · 0 maybe · 1 reject · 1 unrated" in page.locator("#counts").inner_text()
    page.locator(".card", has_text="1:1").get_by_role("button", name="pick").click()  # again: clears
    page.wait_for_function("!document.querySelector('.card.pick')")
    assert rating_of("1:1") is None
    assert not errors, errors


def test_live_keys(live):
    page, errors, *_ = live
    page.keyboard.press("j")  # first visible card
    assert page.evaluate("document.activeElement.dataset.label") == "1:1"
    page.keyboard.press("2")
    page.wait_for_selector(".card.maybe")
    assert rating_of("1:1") == "maybe"
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.label") == "1:2"
    page.keyboard.press("3")  # reject: hidden by default, so the selection moves on
    page.wait_for_function("document.querySelectorAll('.card.reject').length === 2")
    assert rating_of("1:2") == "reject"
    assert page.evaluate("document.activeElement.dataset.label") == "1:1"  # 1:3 is a hidden reject too
    page.keyboard.press("0")
    page.wait_for_function("!document.querySelector('.card.maybe')")
    assert rating_of("1:1") is None
    page.keyboard.press("n")
    assert page.locator("#note-dlg").is_visible()
    page.keyboard.type("too dark, keep the boat")
    page.keyboard.press("Enter")
    page.wait_for_selector(".card .notes li")
    assert "too dark, keep the boat" in page.locator(".card", has_text="1:1").inner_text()
    assert boards.load("fjord")["log"][-1]["note"] == "too dark, keep the boat"
    assert not errors, errors


def test_live_board_note_and_escape(live):
    page, errors, *_ = live
    page.locator("#board-note").click()
    page.keyboard.type("go warmer")
    page.keyboard.press("Escape")  # cancel: nothing saved
    assert boards.load("fjord")["notes"] == []
    page.locator("#board-note").click()
    page.keyboard.type("go warmer")
    page.locator("#note-save").click()
    page.wait_for_selector("#board-notes li")
    assert "go warmer" in page.locator("#board-notes").inner_text()
    assert not errors, errors


def test_live_follows_changes_made_elsewhere(live):
    page, errors, *_ = live
    boards.rate("fjord", ["1:2"], "pick", "from the terminal", author="claude")
    page.wait_for_selector(".card.pick", timeout=6000)
    assert "from the terminal" in page.locator(".card.pick").inner_text()
    assert not errors, errors


def test_live_generate_more(live):
    page, errors, srv, jobs = live
    page.locator("#gen-more").click()
    # count passes, not cards: a pass started in the same second as the fixture's shares its run folder
    page.wait_for_function("document.querySelectorAll('.pass').length === 2", timeout=15000)
    assert len(projects.load("fjord")["passes"]) == 2
    page.wait_for_function("document.getElementById('gen-more').textContent === 'Generate 4 more'")
    assert not errors, errors


def test_live_server_gone(live):
    page, errors, srv, _ = live
    srv.shutdown()
    srv.server_close()
    page.wait_for_function("document.getElementById('live-status').textContent.includes('server stopped')", timeout=6000)
    assert page.locator("#gen-more").is_disabled()


def test_static_keys_copy_commands(board):
    page, errors = board
    page.keyboard.press("j")
    page.keyboard.press("1")
    page.wait_for_selector(".toast.show")
    assert "2:1" in page.locator("#toast").inner_text()
    assert not page.locator("#gen-more").is_visible()
    page.keyboard.press("n")
    assert page.locator("#note-save").inner_text() == "Copy command"
    page.keyboard.press("Escape")
    assert not errors, errors


def test_live_rerender_keeps_images(live):
    """A rating re-renders the board, but moves the loaded <img> elements instead of fetching them again."""
    page, errors, *_ = live
    page.evaluate("window.__img = document.querySelector('.card img')")
    page.keyboard.press("j")
    page.keyboard.press("1")
    page.wait_for_selector(".card.pick")
    assert page.evaluate("document.querySelector('.card img') === window.__img")
    assert not errors, errors


def test_keys_work_after_the_size_slider(board):
    page, errors = board
    page.locator("#size").focus()
    page.keyboard.press("h")
    assert page.locator("#history").is_visible()
    assert not errors, errors


def test_live_old_failure_not_toasted_again(live):
    import time
    page, errors, srv, jobs = live
    jobs.current = {"id": 9, "slug": "fjord", "count": 4, "state": "failed", "error": "RuntimeError: old",
                    "started": time.time() - 60, "finished": time.time() - 50}
    page.reload()
    page.wait_for_timeout(2600)  # one poll
    assert "old" not in page.locator("#toast").inner_text()
