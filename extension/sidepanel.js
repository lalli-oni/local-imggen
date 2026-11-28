// The checklist in a side panel: one pasted plan text -> a card per task with copy buttons and
// "outline this element" buttons. State (the plan, done ticks) lives in chrome.storage.local.
const $ = id => document.getElementById(id);
let state = { raw: "", plan: null, done: {} };

const slug = (t, n = 40) => t.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, n).replace(/-+$/, "");

// same ids as refs.py gives a task without one, so `gen ref plan` and this panel agree
function parsePlan(raw) {
  let d = JSON.parse(raw);
  if (Array.isArray(d)) d = { tasks: d };
  if (!d || !Array.isArray(d.tasks) || !d.tasks.length) throw new Error("the plan text needs a tasks list");
  const date = d.date || new Date().toISOString().slice(0, 10);
  const tasks = d.tasks.map((t, i) => {
    if (!t.prompt || !SERVICES[t.service] && !(d.services || {})[t.service]) throw new Error(`task ${i + 1}: needs a prompt and a known service (${Object.keys(SERVICES).join(", ")})`);
    const tg = t.target || {};
    const title = (t.title || tg.asset || tg.ref || t.service).trim();
    return { ...t, title, id: t.id || `${String(i + 1).padStart(2, "0")}-${t.service}-${slug(title) || "ref"}`, attachments: (t.attachments || []).map(a => typeof a === "string" ? a : (a.windows || a.path)) };
  });
  return { date, tasks, services: d.services || {}, inbox: d.inbox_windows || "" };
}

const svc = (p, name) => ({ ...(SERVICES[name] || {}), ...(p.services[name] || {}) });
const save = () => chrome.storage.local.set({ imagegenRefs: state });
const say = (m, bad) => { $("msg").textContent = m; $("msg").style.color = bad ? "#dc2626" : ""; };

async function copy(text, btn) {
  await navigator.clipboard.writeText(text);
  const old = btn.textContent; btn.textContent = "Copied"; btn.classList.add("copied");
  setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1200);
}

async function highlight(name, kind) {
  const spec = svc(state.plan, name)[kind];
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !spec) return say(`No ${kind} element known for ${name}`, true);
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: imagegenHighlight, args: [spec] });
    say(r.result.found ? `Outlined the ${kind} element: it is yours to press.` : `Could not find the ${kind} element on this page (the site may have changed: see services.js).`, !r.result.found);
  } catch (e) { say(`Cannot highlight on this tab: ${e.message}`, true); }
}

const mk = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); kids.flat().forEach(k => e.append(k)); return e; };
const btn = (label, fn, cls = "") => { const b = mk("button", { textContent: label, className: cls }); b.onclick = () => fn(b); return b; };

function render() {
  const list = $("list");
  list.replaceChildren();
  if (!state.plan) return;
  const p = state.plan;
  const names = [...new Set(p.tasks.map(t => t.service))];
  list.append(mk("div", { className: "mute", textContent: `${p.date}: ${p.tasks.filter(t => state.done[t.id]).length} / ${p.tasks.length} done` + (p.inbox ? `. Save into ${p.inbox}` : "") }));
  for (const name of names) {
    const s = svc(p, name);
    list.append(mk("h2", { textContent: `${s.title || name}${s.daily ? ` (about ${s.daily} free a day)` : ""}` }));
    if (s.tips) list.append(mk("div", { className: "mute", textContent: s.tips }));
    for (const t of p.tasks.filter(x => x.service === name)) {
      const cb = mk("input", { type: "checkbox", checked: !!state.done[t.id] });
      cb.onchange = () => { state.done[t.id] = cb.checked; save(); render(); };
      list.append(mk("div", { className: "task" + (state.done[t.id] ? " done" : "") },
        mk("div", { className: "row" }, cb, mk("b", { textContent: t.title })),
        mk("div", { className: "prompt", textContent: t.prompt }),
        mk("div", { className: "row" },
          btn("Open site", () => chrome.tabs.create({ url: s.url }) ),
          btn("Copy prompt", b => copy(t.prompt, b), "primary"),
          btn("Copy filename", b => copy(t.id, b))),
        ...t.attachments.map(a => mk("div", { className: "row" }, mk("span", { className: "mute", textContent: "attach" }), mk("code", { textContent: a }), btn("Copy path", b => copy(a, b)))),
        mk("div", { className: "row" }, mk("span", { className: "mute", textContent: "Outline:" }),
          ...["prompt", "attach", "generate", "download"].filter(k => k !== "attach" || t.attachments.length).map(k => btn(k, () => highlight(name, k))))));
    }
  }
}

$("load").onclick = () => {
  try { state = { raw: $("paste").value, plan: parsePlan($("paste").value), done: {} }; save(); render(); say(`Loaded ${state.plan.tasks.length} tasks.`); }
  catch (e) { say(`Not a valid plan: ${e.message}`, true); }
};
$("cmd").onclick = b => state.raw ? copy(`gen ref plan --from - <<'EOF'\n${state.raw.trim()}\nEOF`, $("cmd")) : say("Load a plan first", true);
$("clear").onclick = () => { state = { raw: "", plan: null, done: {} }; $("paste").value = ""; save(); render(); say(""); };

chrome.storage.local.get("imagegenRefs").then(r => { if (r.imagegenRefs) { state = r.imagegenRefs; render(); } });
