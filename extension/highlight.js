// Injected into the active tab when the person presses a Highlight button (chrome.scripting, activeTab:
// only the tab they are looking at, only on that click). It outlines one element and scrolls to it.
// It never clicks, types, submits, reads page content or saves anything, and it removes the outline itself.
// Must stay self-contained: executeScript serialises this function.
function imagegenHighlight(spec) {
  const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== "hidden"; };
  document.querySelectorAll("[data-imagegen-hl]").forEach(e => {
    e.style.outline = e.dataset.imagegenHl.split("|")[0];
    e.style.boxShadow = e.dataset.imagegenHl.split("|")[1];
    delete e.dataset.imagegenHl;
  });
  let found = null;
  for (const css of (spec && spec.css) || []) {
    try { found = [...document.querySelectorAll(css)].find(visible); } catch (e) { /* bad selector: next */ }
    if (found) break;
  }
  if (!found && spec && spec.text) {
    const re = new RegExp(spec.text, "i");
    found = [...document.querySelectorAll("button, [role='button'], a")]
      .find(e => visible(e) && re.test((e.getAttribute("aria-label") || e.textContent || "").trim()));
  }
  if (!found) return { found: false };
  found.dataset.imagegenHl = `${found.style.outline}|${found.style.boxShadow}`;
  found.style.outline = "4px solid #ff2bd6";
  found.style.boxShadow = "0 0 24px 6px #ff2bd6aa";
  found.scrollIntoView({ block: "center", behavior: "smooth" });
  setTimeout(() => {
    if (found.dataset.imagegenHl !== undefined) {
      found.style.outline = found.dataset.imagegenHl.split("|")[0];
      found.style.boxShadow = found.dataset.imagegenHl.split("|")[1];
      delete found.dataset.imagegenHl;
    }
  }, 10000);
  return { found: true, tag: found.tagName.toLowerCase() };
}
