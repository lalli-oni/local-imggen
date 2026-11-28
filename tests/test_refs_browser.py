"""The checklist page (templates/ref_checklist.html) and the extension's side panel in headless Chromium.

Same Playwright/Chromium rules as test_contact_sheet_browser.py. The side panel runs on a stub of the
chrome.* APIs; what it must NOT do (fill, click, submit a page) is checked on a stub site.
"""
import json
from pathlib import Path

import pytest

import imagegen
import projects
import refs
from test_contact_sheet_browser import browser  # noqa: F401 - the shared module-scoped fixture

EXT = Path(imagegen.ROOT) / "extension"
PLAN = {"date": "2026-11-29", "tasks": [
    {"service": "chatgpt", "title": "Neon skyline", "prompt": "a neon skyline at dusk",
     "target": {"project": "rs", "asset": "skyline"}, "attachments": ["C:\\refs\\tulu.png"]},
    {"service": "ideogram", "title": "Sign", "prompt": "a sign saying \"SALE\"",
     "target": {"project": "rs", "asset": "sign"}}]}

STUB_PAGE = """<!doctype html><title>stub</title>
<div style="height:2000px"></div>
<textarea id="prompt" style="width:300px;height:50px"></textarea>
<button id="gen" type="submit">Generate</button>
<button style="display:none" id="hidden-dl" aria-label="Download">x</button>
<button id="dl" aria-label="Download image">d</button>
<script>window.clicked = 0; document.getElementById('gen').onclick = () => window.clicked++;
window.inputs = 0; document.getElementById('prompt').oninput = () => window.inputs++;</script>"""


def test_checklist_page(browser, out_dir):  # noqa: F811
    projects.new("rs", "RS", "x", prompt="x")
    att = out_dir / "tulu.png"
    att.parent.mkdir(parents=True, exist_ok=True)
    att.write_bytes(b"x")
    plan = json.loads(json.dumps(PLAN))
    plan["tasks"][0]["attachments"] = [str(att)]
    p = refs.plan(plan)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(Path(p["files"]["checklist.html"]).as_uri())
    assert page.locator("h2").count() == 2
    assert page.locator(".task").count() == 2
    assert "a neon skyline at dusk" in page.locator(".task pre").first.inner_text()
    assert page.locator("#count").inner_text() == "0 / 2"
    page.locator("button", has_text="Copy prompt").first.click()
    assert page.evaluate("navigator.clipboard.readText()") == "a neon skyline at dusk"
    page.locator("button", has_text="Copy name").first.click()
    assert page.evaluate("navigator.clipboard.readText()") == "01-chatgpt-neon-skyline"
    page.locator(".task input[type=checkbox]").first.check()
    assert page.locator("#count").inner_text() == "1 / 2"
    assert not errors, errors
    page.close()


@pytest.fixture
def panel(browser):  # noqa: F811
    """sidepanel.html with chrome.* stubbed; chrome.scripting.executeScript runs the function on the stub site."""
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.add_init_script("""
      window.__store = {}; window.__opened = [];
      window.chrome = {
        storage: { local: { get: async k => ({ [k]: window.__store[k] }), set: async o => Object.assign(window.__store, o) } },
        tabs: { query: async () => [{ id: 1 }], create: async o => window.__opened.push(o.url) },
        scripting: { executeScript: async ({ func, args }) => [{ result: await window.__run(func, args) }] },
      };
      window.__run = async (func, args) => window.__site ? window.__site(func, args) : { found: false };
    """)
    page.goto((EXT / "sidepanel.html").as_uri())
    yield page, errors
    page.close()


def test_panel_loads_plan_and_copies(panel):
    page, errors = panel
    page.fill("#paste", json.dumps(PLAN))
    page.click("#load")
    assert "Loaded 2 tasks" in page.locator("#msg").inner_text()
    assert page.locator(".task").count() == 2
    page.locator("button", has_text="Copy prompt").first.click()
    assert page.evaluate("navigator.clipboard.readText()") == "a neon skyline at dusk"
    page.locator("button", has_text="Copy filename").first.click()
    assert page.evaluate("navigator.clipboard.readText()") == "01-chatgpt-neon-skyline"  # same id as refs.py makes
    page.locator("button", has_text="Copy path").first.click()
    assert page.evaluate("navigator.clipboard.readText()") == "C:\\refs\\tulu.png"
    page.click("#cmd")
    cmd = page.evaluate("navigator.clipboard.readText()")
    assert cmd.startswith("gen ref plan --from - <<'EOF'\n") and cmd.endswith("\nEOF") and "neon skyline" in cmd
    page.locator("button", has_text="Open site").first.click()
    assert page.evaluate("window.__opened") == ["https://chatgpt.com/"]
    page.locator(".task input[type=checkbox]").first.check()
    assert "1 / 2 done" in page.locator("#list .mute").first.inner_text()
    assert page.evaluate("window.__store.imagegenRefs.done['01-chatgpt-neon-skyline']") is True
    assert not errors, errors


def test_panel_rejects_bad_plans(panel):
    page, _ = panel
    for bad in ("not json", '{"tasks": []}', '{"tasks": [{"service": "nope", "prompt": "x"}]}'):
        page.fill("#paste", bad)
        page.click("#load")
        assert "Not a valid plan" in page.locator("#msg").inner_text()
    assert page.locator(".task").count() == 0


def run_highlight(site, spec):
    src = (EXT / "highlight.js").read_text()
    return site.evaluate(f"(spec) => {{ {src}\n return imagegenHighlight(spec); }}", spec)


def style(site, id_):
    return site.evaluate(f"getComputedStyle(document.getElementById('{id_}')).outlineStyle")


def test_highlight_outlines_but_never_touches_the_page(browser):  # noqa: F811
    site = browser.new_page()
    site.set_content(STUB_PAGE)
    assert run_highlight(site, {"css": ["#prompt"]})["found"]
    assert style(site, "prompt") == "solid"
    # the next highlight moves the outline; text fallback finds a button by its label
    assert run_highlight(site, {"css": ["#nope"], "text": "^generate"})["found"]
    assert style(site, "prompt") == "none" and style(site, "gen") == "solid"
    # a hidden element is skipped in favour of the visible one
    assert run_highlight(site, {"css": ["button[aria-label*='Download' i]"]})["found"]
    assert style(site, "dl") == "solid" and style(site, "hidden-dl") == "none"
    # nothing was typed, clicked or submitted on the page
    assert site.evaluate("[window.clicked, window.inputs, document.getElementById('prompt').value]") == [0, 0, ""]
    # an element that is not there is reported, not guessed
    assert run_highlight(site, {"css": ["#nope"], "text": "nope"}) == {"found": False}
    site.close()


def test_panel_highlight_passes_the_service_spec(panel):
    page, errors = panel
    page.evaluate("() => { window.__calls = []; window.__site = (func, args) => { window.__calls.push([func.name, args[0]]); return { found: true }; }; }")
    page.fill("#paste", json.dumps(PLAN))
    page.click("#load")
    page.locator(".task").first.locator("button", has_text="generate").click()
    page.wait_for_function("document.getElementById('msg').textContent.includes('Outlined the generate')")
    name, spec = page.evaluate("window.__calls[0]")
    assert name == "imagegenHighlight" and "button[data-testid='send-button']" in spec["css"]
    page.evaluate("() => { window.__site = () => ({ found: false }); }")
    page.locator(".task").first.locator("button", has_text="download").click()
    page.wait_for_function("document.getElementById('msg').textContent.includes('Could not find')")
    assert not errors, errors
