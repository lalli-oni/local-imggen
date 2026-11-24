"""The viewer app (#82, app/) in a real headless Chromium, served by `gen app serve` with test data.

Needs the built app (cd app && npm ci && npm run build): skipped without it, unless IMAGEGEN_REQUIRE_APP=1
(CI builds it first). Same Playwright/Chromium rules as test_contact_sheet_browser.py.
"""
import os
import threading

import pytest

import appserve
from test_appserve import world  # noqa: F401 - the shared project/character/runs fixture
from test_contact_sheet_browser import browser  # noqa: F401 - the shared module-scoped fixture


def _built() -> bool:
    if (appserve.APP_DIR / "index.html").is_file():
        return True
    if os.environ.get("IMAGEGEN_REQUIRE_APP") == "1":
        pytest.fail("app/build missing: cd app && npm ci && npm run build")
    return False


pytestmark = pytest.mark.skipif(not _built(), reason="app not built (cd app && npm ci && npm run build)")


@pytest.fixture
def app(browser, world):  # noqa: F811
    srv = appserve.make_server(0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    base = f"http://127.0.0.1:{srv.server_port}"

    def go(path="/"):
        page.goto(base + path)
        page.wait_for_load_state("networkidle")
        page.evaluate("document.querySelectorAll('img').forEach(i => { i.loading = 'eager'; })")
        page.wait_for_function("[...document.images].every(i => i.complete)")
        return page

    yield go, page, errors
    page.close()
    srv.shutdown()
    srv.server_close()


def images_ok(page) -> bool:
    return page.evaluate("[...document.images].every(i => i.naturalWidth > 0)")


def test_home(app):
    go, page, errors = app
    go("/")
    assert "latest wallpaper" in page.locator(".hero").inner_text().lower()
    assert "synthwave beach" in page.locator(".hero").inner_text()
    assert "1 project · 1 character · 4 runs · 8 images" in page.locator(".stats").inner_text()
    tile = page.locator("a.tile[href='/projects/walls']")
    assert "Walls" in tile.inner_text() and "converge" in tile.inner_text().lower()
    assert "1 picked" in tile.inner_text()
    assert page.locator("a.tile[href='/characters/tulu']").count() == 1
    assert images_ok(page)
    assert not errors, errors


def test_project_board_filters_and_lightbox(app):
    go, page, errors = app
    go("/")
    page.locator("a.tile[href='/projects/walls']").click()
    page.wait_for_url("**/projects/walls")
    page.wait_for_selector("article.pass")
    assert page.locator("h1").inner_text() == "Walls v3"
    assert page.locator("article.pass header strong").all_inner_texts() == ["Pass 2", "Pass 1"]
    assert page.locator(".cell").count() == 3  # 1:2 is a reject: hidden
    assert "the light!" in page.locator(".cell.pick").inner_text()
    page.get_by_role("button", name="Picks 1").click()
    assert page.locator(".cell").count() == 1
    page.get_by_role("button", name="All").click()
    page.get_by_label("hide rejects").uncheck()
    assert page.locator(".cell").count() == 4
    assert "go warmer" in page.locator("aside").inner_text()
    assert "hero" in page.locator("figcaption").all_inner_texts()[-1]

    page.locator(".cell.pick button.thumb").click()
    lb = page.locator(".lb")
    assert lb.is_visible() and "2:1 · pick" in lb.inner_text() and "1 / 4" in lb.inner_text()
    page.keyboard.press("ArrowRight")
    assert "2 / 4" in lb.inner_text()
    page.keyboard.press("Escape")
    assert page.locator(".lb").count() == 0
    assert not errors, errors


def test_character(app):
    go, page, errors = app
    go("/characters/tulu")
    assert page.locator("h1").inner_text() == "lil tulu v2"
    assert "chibi cthulhu" in page.locator(".info").inner_text()
    assert page.locator(".stages li.now").inner_text().lower() == "converge"
    assert page.locator(".timeline > li").count() == 2
    assert page.locator("a[href='/projects/walls']").count() == 1
    assert images_ok(page)
    assert not errors, errors


def test_wallpapers(app):
    go, page, errors = app
    go("/wallpapers")
    assert page.locator(".wall").count() == 2
    assert "daily" in page.locator(".wall").first.inner_text().lower()
    log = page.locator(".log li")
    assert log.count() == 2 and "ok" in log.first.inner_text() and "failed" in log.nth(1).inner_text()
    page.locator(".wall").first.click()
    assert "with a boombox" in page.locator(".lb").inner_text()
    page.get_by_role("link", name="Details").click()
    page.wait_for_url("**/runs/2026-11-17/*")
    assert "daily wallpaper" in page.locator(".chips").inner_text()
    assert not errors, errors


def test_runs_search_and_detail(app):
    go, page, errors = app
    go("/runs")
    assert page.locator("article.run").count() == 4
    page.get_by_label("Search").fill("fox")
    page.wait_for_url("**/runs?q=fox")
    page.wait_for_function("document.querySelectorAll('article.run').length === 1")
    page.locator("article.run a.label").click()
    page.wait_for_url("**/runs/2026-11-01/090000-draft-a-red-fox")
    page.wait_for_selector("dl")
    assert "width" in page.locator("dl").inner_text()
    assert page.locator("figure").count() == 2
    assert not errors, errors


def test_unknown_project_shows_the_error(app):
    go, page, errors = app
    go("/projects/nope")
    assert "404" in page.locator("h1").inner_text()
    assert "not found" in page.locator(".empty").inner_text()
    errors[:] = [e for e in errors if "404" not in e]  # the failed fetch itself is logged by Chromium
    assert not errors, errors


def test_installable(app):
    go, page, errors = app
    go("/")
    assert page.evaluate("fetch('/manifest.webmanifest').then(r => r.json()).then(m => m.display)") == "standalone"
    assert page.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)")
    assert not errors, errors


def test_prompts_page(app):
    go, page, errors = app
    go("/prompts")
    text = page.locator("main").inner_text()
    for part in ("Daily wallpaper", "Themes", "Props", "Styles", "Presets", "Daily templates", "Character stages",
                 "chibi cthulhu", "misty fjord, warm light", "synthwave"):
        assert part in text, part
    assert page.locator("a[href='/characters/tulu']").count() == 1
    assert page.locator("a[href='/projects/walls']").count() == 1
    # the filter narrows themes, props and styles, and clears again
    n = page.locator("#themes li").count()
    page.fill("input[type=search]", "heist")
    assert 0 < page.locator("#themes li").count() < n
    assert page.locator("#styles .panel").count() == 0
    page.fill("input[type=search]", "")
    assert page.locator("#themes li").count() == n
    # the nav has a Prompts link
    assert page.locator("nav a[href='/prompts']").count() == 1
    assert errors == []
