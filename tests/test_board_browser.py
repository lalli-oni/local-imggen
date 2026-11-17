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


@pytest.mark.parametrize("clipboard", ["api", "fallback"])
def test_static_keys_copy_commands(board, clipboard):
    page, errors = board
    if clipboard == "fallback":  # as in CI: the clipboard API refuses, so copy() uses a scratch textarea
        page.evaluate("() => { navigator.clipboard.writeText = () => Promise.reject(new Error('denied')); }")
    page.keyboard.press("j")
    page.keyboard.press("1")
    page.wait_for_selector(".toast.show")
    assert "2:1" in page.locator("#toast").inner_text()
    assert not page.locator("#gen-more").is_visible()
    assert page.evaluate("document.activeElement.dataset.label") == "2:1"  # copying kept the selection
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


def selected(page):
    return page.evaluate("document.activeElement.closest('.card')?.dataset.label ?? null")


def test_click_selects_and_arrows_start_without_focus(board):
    """Feedback from tulu: keys only worked once a card had focus, and clicking an image opened a tab."""
    page, errors = board
    page.locator("#size").fill("400")  # 3 per row at 1280px
    page.locator("#hide-rejects").uncheck()
    page.locator("body").click(position={"x": 5, "y": 5})
    assert selected(page) is None
    page.keyboard.press("ArrowRight")  # no selection yet: the first card in view
    assert selected(page) == "2:1"
    pages_before = len(page.context.pages)
    page.locator(".card", has_text="1:2").locator("img").click()
    assert selected(page) == "1:2"
    assert len(page.context.pages) == pages_before  # a click no longer opens a tab
    page.keyboard.press("1")  # rates the clicked card (static page: copies the command)
    page.wait_for_selector(".toast.show")
    assert "1:2" in page.locator("#toast").inner_text()
    page.keyboard.press("Escape")
    assert selected(page) is None
    assert not errors, errors


def test_up_down_move_by_row(board):
    page, errors = board
    page.locator("#hide-rejects").uncheck()
    page.set_viewport_size({"width": 1000, "height": 800})
    page.locator("#size").fill("200")  # pass 1 (3 images) wraps into rows of 4 at most
    page.locator(".card", has_text="2:2").locator("img").click()
    page.keyboard.press("ArrowDown")  # from v2's pass to the row below: pass 1, nearest column
    assert selected(page) == "1:2"
    page.keyboard.press("ArrowUp")
    assert selected(page) == "2:2"
    page.keyboard.press("ArrowUp")  # nothing above: stays
    assert selected(page) == "2:2"
    assert not errors, errors


def test_open_full_size(board):
    page, errors = board
    page.locator(".card", has_text="2:1").locator("img").click()
    with page.context.expect_page() as tab:
        page.keyboard.press("o")
    assert tab.value.url.endswith(".png")
    tab.value.close()
    with page.context.expect_page() as tab:
        page.locator(".card", has_text="2:1").locator("img").dblclick()
    tab.value.close()
    assert not errors, errors


def test_slider_keeps_its_arrows(board):
    page, errors = board
    page.locator("#size").focus()
    before = page.locator("#size").input_value()
    page.keyboard.press("ArrowRight")
    assert page.locator("#size").input_value() != before
    assert selected(page) is None
    assert not errors, errors


# ---------------------------------------------------------------- character stages (#68)

@pytest.fixture
def staged(browser, out_dir, comfy, make_image):  # noqa: F811
    """Project tulu casting character tulu (explore, with a ref), one explore pass of 2, served."""
    import threading

    import boardserve
    import characters
    import stages
    characters.new("tulu", description="chibi green cthulhu", refs={"main": str(make_image("ref.png"))})
    projects.new("tulu", cast={"tulu": None})
    stages.run_stage("tulu", "explore", count=2, seed=1)
    jobs = boardserve.Jobs()
    srv = boardserve.make_server(0, jobs=jobs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(f"http://127.0.0.1:{srv.server_port}/projects/tulu/")
    yield page, errors, jobs
    page.close()
    srv.shutdown()
    srv.server_close()


def test_stage_row_and_converge_from_the_selected_image(staged):
    import characters
    page, errors, jobs = staged
    assert page.locator("#stagebar").is_visible() and not page.locator("#gen-more").is_visible()
    assert page.locator("#stage-select").input_value() == "explore"
    assert page.locator(".stage-btn.current").inner_text() == "Explore ×8"
    assert page.locator(".pass h3").first.inner_text().startswith("Pass 1 · explore")
    assert "tulu's ref" in page.locator("#stage-src").inner_text()
    page.locator(".card img").nth(1).click()  # select 1:2: converge edits it
    assert "edit 1:2 (selected)" in page.locator("#stage-src").inner_text()
    page.get_by_role("button", name="Converge ×4").click()
    page.wait_for_function("document.querySelectorAll('.pass').length === 2", timeout=20000)
    p = projects.load("tulu")["passes"][-1]
    assert p["stage"] == "converge" and p["source"] == "1:2"
    assert "converge" in page.locator(".pass h3").first.inner_text()
    assert "sitting down" in page.locator(".card .idea").first.inner_text()
    page.wait_for_function("document.querySelector('.stage-btn[data-stage=converge]').textContent === 'Converge ×4'")
    # the stage menu moves the character; polish then needs the fix in the text field
    page.locator("#stage-select").select_option("polish")
    page.wait_for_function("document.querySelector('.stage-btn.current').dataset.stage === 'polish'")
    assert characters.load("tulu")["state"]["stage"] == "polish"
    page.get_by_role("button", name="Polish ×2").click()
    page.wait_for_selector(".toast.show")
    assert "type the one thing to fix" in page.locator("#toast").inner_text()
    assert jobs.get()["stage"] == "converge"  # nothing new started
    page.locator("#stage-text").fill("remove the bump on the elbow")
    page.keyboard.press("Enter")  # runs the current stage
    page.wait_for_function("document.querySelectorAll('.pass').length === 3", timeout=20000)
    p = projects.load("tulu")["passes"][-1]
    assert p["stage"] == "polish" and p["text"] == "remove the bump on the elbow"
    assert not errors, errors


def test_make_ref_button(staged):
    import characters
    page, errors, _ = staged
    page.on("dialog", lambda d: d.accept())
    page.locator(".card", has_text="1:1").get_by_role("button", name="ref").click()
    page.wait_for_selector(".toast.show")
    assert "1:1 is now tulu's main ref (v2)" in page.locator("#toast").inner_text()
    assert characters.load("tulu")["versions"][-1]["note"] == "main ref from tulu 1:1"
    assert not errors, errors


def test_static_stage_buttons_copy_commands(browser, out_dir, comfy, make_image):  # noqa: F811
    import characters
    import stages
    characters.new("tulu", description="chibi green cthulhu", refs={"main": str(make_image("ref.png"))})
    projects.new("tulu", cast={"tulu": None})
    stages.run_stage("tulu", "explore", count=2, seed=1)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(boards.page_file("tulu").as_uri())
    page.evaluate("() => { window.__copied = []; navigator.clipboard.writeText = (t) => { window.__copied.push(t); return Promise.resolve(); }; }")
    assert page.locator("#stage-select").is_disabled()
    page.locator(".card img").first.click()
    page.get_by_role("button", name="Converge ×4").click()
    page.locator("#stage-text").fill("it's waving")
    page.get_by_role("button", name="Polish ×2").click()
    page.locator(".card", has_text="1:2").get_by_role("button", name="ref").click()
    copied = page.evaluate("window.__copied")
    assert copied[0] == "gen project gen tulu --stage converge --from 1:1"
    assert copied[1] == "gen project gen tulu --stage polish --from 1:1 --text 'it'\\''s waving'"
    assert copied[2].startswith("gen char set tulu --ref main='") and copied[2].endswith("--note 'ref from tulu 1:2'")
    page.close()
