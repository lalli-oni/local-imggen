# imagegen reference helper (Chrome/Edge side panel)

Helps with the daily reference-image checklist (#112, `gen ref plan`). It **only copies text and outlines
elements**; you press Generate and save the file yourself. It never fills a box, clicks, submits, reads the
page or saves an image, and it has no persistent content script or host permissions: the outline is injected into
the tab you are looking at, on the click of a button (`activeTab`). That keeps it on the "a person using the site"
side of the free tiers' terms (they ban bots and programmatic extraction). If a service's terms change, drop it
from `services.js`.

## Install (once)
1. Open `chrome://extensions` (or `edge://extensions`), turn on Developer mode.
2. Load unpacked: the `extension/` folder of this repo (on tulu: `\\wsl.localhost\Ubuntu-24.04\home\tulu\imagegen\extension`).
3. Pin it. The toolbar button opens the side panel.

## Daily use
1. Paste the plan text the scheduled Claude session wrote (format in `refs.py`) and press **Load plan**.
2. Press **Copy tulu command** and run it in WSL once: it writes `refs-inbox/<date>/checklist.html` and the
   `plan.json` that `gen ref import` needs.
3. Per task: **Open site**, **Copy prompt** (paste it), **Outline** prompt/attach/generate/download to see where to
   click, press Generate yourself, save the result as the name from **Copy filename** into the inbox folder.
4. `gen ref import --yes` files everything into the right project/character with its prompt and source.

## Notes
- The selectors in `services.js` are best effort and drift when a site changes its markup: a highlight that finds
  nothing says so. Fix the selector there.
- Tests: `tests/test_refs_browser.py` runs the panel on stubbed `chrome.*` APIs and the highlighter on a stub page.
