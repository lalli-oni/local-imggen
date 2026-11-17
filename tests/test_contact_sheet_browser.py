"""The contact sheet in a real (headless) Chromium: renders, no JS errors, picking and compare work.

Needs the optional Python playwright package (requirements-browser.txt) and a Chromium. Skipped
when either is missing, unless IMAGEGEN_REQUIRE_BROWSER=1 (CI sets it), which turns the skip
into a failure. IMAGEGEN_TEST_CHROMIUM points at a Chromium binary to use instead of
Playwright's own download.
"""
import json
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


def test_t2i_bakeoff_sheet_shows_size_notes_and_letterboxes_mixed_sizes(make_run, open_sheet):
    """gen bakeoff --t2i --ref (#51): a size column, skipped columns as a muted note, and in fit mode the
    square +ref images sit in cells of the text-only images' aspect instead of overflowing them."""
    run = make_run(n=3, preset="bakeoff: klein vs klein+ref", prompt="1. a tide pool")
    from PIL import Image
    for im, size in zip(run["images"], [(1024, 1024), (1344, 768), (1344, 768)]):  # the square one comes first
        Image.new("RGB", size, (20, 90, 160)).save(im["path"])
        side = Path(im["path"]).with_suffix(".json")
        meta = json.loads(side.read_text())
        meta["params"].update(width=size[0], height=size[1])
        side.write_text(json.dumps(meta))
    run["images"][0]["tag"] = "klein+ref · 1"
    run["images"][2]["tag"] = "klein · 1"
    run["source_image"] = str(Path(run["images"][0]["path"]))  # stands in for the ref
    run["bakeoff"] = {"mode": "t2i", "instructions": ["a tide pool"], "ref": run["source_image"], "presets": [
        {"preset": "klein", "images": 2, "size": "1344x768", "steps": 16, "error": None, "peak": {}},
        {"preset": "draft+ref", "images": 0, "error": None, "note": "no ref input for this model", "peak": {}}]}
    page, errors = open_sheet(run)
    rows = page.locator("#bake tr")
    assert rows.nth(1).locator("td").all_inner_texts()[:4] == ["klein", "2", "1344x768", "16"]
    assert rows.nth(2).locator("td.note").inner_text() == "no ref input for this model"
    assert rows.nth(2).locator("td.err").count() == 0
    meta = page.locator("#meta").inner_text()
    assert "+ref columns use" in meta and "img2img from" not in meta and "edit of" not in meta
    assert page.locator("#neg").is_hidden()  # each model has its own negative (or none)
    page.locator("#mode-fit").click()
    page.wait_for_function("[...document.querySelectorAll('#grid img')].every(i => i.complete && i.naturalWidth)")
    boxes = [page.locator("#grid figure img").nth(i).bounding_box() for i in range(3)]
    assert all(abs(b["height"] - boxes[0]["height"]) < 2 for b in boxes)  # one cell shape for all
    # the most common size (16:9) sets the cell shape, even though the square +ref image is listed first
    assert abs(boxes[0]["width"] / boxes[0]["height"] - 1344 / 768) < 0.05
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


def test_timing_shown(make_run, open_sheet):
    run = make_run(n=2, timing={"summary": "2 images in 90.0s: sample 80.0s"})
    for im, s in zip(run["images"], (50.0, 40.0)):
        im["timing"] = {"total_s": s, "sample_s": s - 5, "comfy_s": s}
    page, errors = open_sheet(run)
    assert "2 images in 90.0s" in page.locator("#meta").inner_text()
    first = page.locator("#grid figcaption .time").first
    assert first.inner_text() == "50.0s" and first.get_attribute("title") == "sample 45.0s"
    assert not errors, errors


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
    assert rows.nth(1).locator("td").all_inner_texts()[:9] == ["kontext", "1", "-", "-", "180.2", "-", "14.3", "20.0",
                                                                "15.5"]
    assert rows.nth(2).locator("td.err").inner_text() == "RuntimeError: <b>crashed</b>"  # text, not HTML
    caps = page.locator("#grid figcaption").all_inner_texts()
    assert caps[0].startswith("kontext · 1") and "81.5s" in caps[0] and caps[1].startswith("klein · 1")
    assert "steps" not in page.locator("#meta").inner_text()  # presets differ: no shared params
    assert not errors, errors
