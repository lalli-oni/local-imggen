"""The contact sheet in a real (headless) Chromium: renders, no JS errors, picking and compare work.

Needs the optional Python playwright package (requirements-browser.txt) and a Chromium. Skipped
when either is missing, unless IMAGEGEN_REQUIRE_BROWSER=1 (CI sets it), which turns the skip
into a failure. IMAGEGEN_TEST_CHROMIUM points at a Chromium binary to use instead of
Playwright's own download.
"""
import os
from pathlib import Path

import pytest

import imagegen


def _need(reason):
    if os.environ.get("IMAGEGEN_REQUIRE_BROWSER") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _need("playwright not installed (pip install -r requirements-browser.txt)")
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=os.environ.get("IMAGEGEN_TEST_CHROMIUM") or None)
        except Exception as e:
            _need(f"no Chromium for playwright ({str(e).splitlines()[0]}); run: python -m playwright install chromium")
        yield b
        b.close()


@pytest.fixture
def open_sheet(browser):
    pages = []

    def open_(run: dict):
        sheet = imagegen.write_contact_sheet(Path(run["run_dir"]), run)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(sheet.as_uri())
        page.wait_for_function("[...document.images].every(i => i.complete)")
        pages.append(page)
        return page, errors

    yield open_
    for p in pages:
        p.close()


def test_renders_all_images(make_run, open_sheet):
    page, errors = open_sheet(make_run(n=4, prompt="a misty fjord"))
    assert page.locator("#prompt").inner_text() == "a misty fjord"
    assert page.locator("#grid figure").count() == 4
    assert page.evaluate("[...document.images].every(i => i.naturalWidth > 0)"), "an image failed to load"
    assert not errors, errors


def test_fit_all_keeps_everything_on_screen(make_run, open_sheet):
    page, _ = open_sheet(make_run(n=8))
    page.locator("#mode-fit").click()
    bottom = page.evaluate("Math.max(...[...document.querySelectorAll('#grid figure')].map(f => f.getBoundingClientRect().bottom))")
    assert bottom <= 800


def test_pick_two_and_compare(make_run, open_sheet):
    page, errors = open_sheet(make_run(n=3))
    assert page.locator("#compare").is_disabled()
    figs = page.locator("#grid figure")
    figs.nth(0).locator("button.pick").click()
    assert figs.nth(0).locator("button.pick").inner_text() == "A"
    figs.nth(2).locator("button.pick").click()  # the second pick opens the A/B view
    assert page.locator("#compare").is_enabled()
    assert "open" in page.locator("#view").get_attribute("class")
    assert page.locator("#stage img").count() == 2
    page.keyboard.press("Escape")
    assert "open" not in (page.locator("#view").get_attribute("class") or "")
    page.locator("#compare").click()  # and the button opens it again
    assert page.locator("#stage img").count() == 2
    assert not errors, errors


def test_img2img_run_shows_input_first(make_run, make_image, open_sheet):
    run = make_run(n=2, source_image=str(make_image("orig.png")), mask=None, denoise=0.4)
    page, errors = open_sheet(run)
    figs = page.locator("#grid figure")
    assert figs.count() == 3 and "src" in figs.nth(0).get_attribute("class")
    assert "img2img from" in page.locator("#meta").inner_text()
    assert not errors, errors


def test_project_run_has_promote_buttons(make_run, open_sheet):
    page, errors = open_sheet(make_run(n=2, project={"slug": "tulu", "version": 3}))
    assert page.get_by_role("button", name="promote").count() == 2
    assert "tulu v3" in page.locator("#meta").inner_text()
    assert not errors, errors


def test_transparent_run_uses_checkerboard(make_run, open_sheet):
    page, _ = open_sheet(make_run(n=1, transparent=True))
    assert "checker" in page.locator("#grid img").first.get_attribute("class")


def test_bakeoff_sheet_shows_tags_and_numbers(make_run, open_sheet):
    run = make_run(n=2, preset="bakeoff: kontext vs klein", prompt="1. make it wave")
    run["images"][0]["tag"], run["images"][1]["tag"] = "kontext · 1", "klein · 1"
    run["images"][0]["seconds"] = 81.5
    run["bakeoff"] = {"instructions": ["make it wave"], "presets": [
        {"preset": "kontext", "images": 1, "first_seconds": 180.2, "median_seconds": None, "error": None,
         "peak": {"vram_gib": 14.25, "ram_gib": 20.0, "comfy_rss_gib": 15.5, "swap_gib": 3.0}},
        {"preset": "klein", "images": 1, "first_seconds": None, "error": "RuntimeError: <b>crashed</b>", "peak": {}}]}
    page, errors = open_sheet(run)
    assert page.locator("#prompt").inner_text() == "bakeoff: kontext vs klein"
    assert page.locator("#bake li").all_inner_texts() == ["make it wave"]
    rows = page.locator("#bake tr")
    assert rows.count() == 3
    assert rows.nth(1).locator("td").all_inner_texts()[:7] == ["kontext", "1", "180.2", "-", "14.3", "20.0", "15.5"]
    assert rows.nth(2).locator("td.err").inner_text() == "RuntimeError: <b>crashed</b>"  # text, not HTML
    caps = page.locator("#grid figcaption").all_inner_texts()
    assert caps[0].startswith("kontext · 1") and "81.5s" in caps[0] and caps[1].startswith("klein · 1")
    assert "steps" not in page.locator("#meta").inner_text()  # presets differ: no shared params
    assert not errors, errors
