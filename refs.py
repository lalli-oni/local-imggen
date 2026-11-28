"""Reference inbox (#112): a daily checklist for making reference images by hand on free web tiers,
and an import that files the results into projects and characters with where they came from.

Nothing here talks to a web service, and nothing drives a web page. The services' terms ban bots and
programmatic extraction (see #112), so a person clicks Generate and saves the file; this module only
plans the day and imports what lands on disk.

  plan text (JSON, written by the scheduled Claude session or by hand)
    {"date": "2026-11-29", "tasks": [
      {"id": "01-chatgpt-skyline", "service": "chatgpt", "title": "Neon skyline",
       "prompt": "...", "attachments": ["/mnt/c/.../ref.png"],
       "target": {"project": "radical-sabbatical", "asset": "world/skyline", "as_ref": "skyline"}}]}
    target is {"project", "asset", ["as_ref"]} (a new asset version, optionally also a project ref)
    or {"character", "ref", ["role"]} (a character ref, which is a new character version).
    id is optional (then NN-service-slug); the file to save is <id>.png (jpg/webp also fine).

  gen ref plan --from FILE|-   writes <output_dir>/refs-inbox/<date>/{plan.json,checklist.md,checklist.html}
  gen ref import [--yes]       finds <id>.<ext> in the inbox (and [refs] watch folders) and files each one
  gen ref status               what is still to do
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date as _date
from pathlib import Path

import imagegen
import projects

IMAGE_EXTS = projects.IMAGE_EXTS
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEMPLATE = imagegen.ROOT / "templates" / "ref_checklist.html"


def inbox_root() -> Path:
    return Path(imagegen.load_config()["output_dir"]).expanduser() / "refs-inbox"


def inbox_dir(day: str) -> Path:
    if not _DATE.match(day):
        raise ValueError(f"bad date '{day}': use YYYY-MM-DD")
    return inbox_root() / day


def services(conf: dict | None = None) -> dict:
    """[ref_services.NAME] from config.toml: title, url, daily (free cap per day), upload, tips."""
    return (conf or imagegen.load_config()).get("ref_services", {})


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def _target(t: dict, where: str) -> dict:
    if not isinstance(t, dict):
        raise ValueError(f"{where}: target must be an object")
    if ("project" in t) == ("character" in t):
        raise ValueError(f"{where}: target needs exactly one of project or character")
    if "project" in t:
        projects._check_name(t["project"], "project")
        if not t.get("asset"):
            raise ValueError(f"{where}: a project target needs an asset name")
        projects._check_name(t["asset"], "asset")
        if t.get("as_ref"):
            projects._check_name(t["as_ref"], "ref")
        return {k: t[k] for k in ("project", "asset", "as_ref") if t.get(k)}
    import characters
    characters._check_name(t["character"], "character")
    if not t.get("ref"):
        raise ValueError(f"{where}: a character target needs a ref name")
    characters._check_name(t["ref"], "ref")
    return {k: t[k] for k in ("character", "ref", "role") if t.get(k)}


def _task(raw: dict, n: int, svcs: dict, taken: set) -> dict:
    where = f"task {n}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be an object")
    svc = raw.get("service")
    if svc not in svcs:
        raise ValueError(f"{where}: unknown service '{svc}' (known: {', '.join(svcs) or 'none: add [ref_services.NAME]'})")
    prompt = (raw.get("prompt") or "").strip()
    if not prompt:
        raise ValueError(f"{where}: no prompt")
    target = _target(raw.get("target"), where)
    title = (raw.get("title") or target.get("asset") or target.get("ref") or svc).strip()
    tid = raw.get("id") or f"{n:02d}-{svc}-{_slug(title) or 'ref'}"
    if not _ID.match(tid):
        raise ValueError(f"{where}: bad id '{tid}' (lowercase letters, digits, dashes)")
    if tid in taken:
        raise ValueError(f"{where}: id '{tid}' is used twice")
    atts = []
    for a in raw.get("attachments") or []:
        p = imagegen.to_linux_path(a)
        if not p.is_file():
            raise FileNotFoundError(f"{where}: attachment {a} does not exist")
        atts.append({"path": str(p), "windows": imagegen.to_windows_path(p) or str(p)})
    return {"id": tid, "service": svc, "title": title, "prompt": prompt, "attachments": atts,
            "target": target, "status": "todo", **({"note": raw["note"]} if raw.get("note") else {})}


def plan(text: str | dict | list, day: str | None = None) -> dict:
    """Write the day's plan.json, checklist.md and checklist.html from the plan text.

    Planning a day again adds the new tasks (ids must be new); existing ones, and what is already
    imported, stay. Returns the plan with "files" and "warnings".
    """
    data = json.loads(text) if isinstance(text, str) else text
    if isinstance(data, list):
        data = {"tasks": data}
    if not isinstance(data, dict) or not data.get("tasks"):
        raise ValueError("the plan text needs a non-empty tasks list")
    conf = imagegen.load_config()
    svcs = services(conf)
    day = day or data.get("date") or _date.today().isoformat()
    d = inbox_dir(day)
    with projects.lock_file(inbox_root(), "refs"):
        cur = load_plan(day) if (d / "plan.json").is_file() else {"schema": 1, "date": day, "tasks": []}
        taken = {t["id"] for t in cur["tasks"]}
        for raw in data["tasks"]:
            t = _task(raw, len(cur["tasks"]) + 1, svcs, taken)
            taken.add(t["id"])
            cur["tasks"].append(t)
        cur["inbox"] = str(d)
        cur["inbox_windows"] = imagegen.to_windows_path(d) or str(d)
        used = {t["service"] for t in cur["tasks"]}
        cur["services"] = {s: {k: v for k, v in svcs[s].items()} for s in svcs if s in used}
        cur["warnings"] = []
        for s in sorted(used):
            n = sum(t["service"] == s for t in cur["tasks"])
            cap = svcs[s].get("daily")
            if cap and n > cap:
                cur["warnings"].append(f"{n} tasks for {s}, but its free tier gives about {cap} a day "
                                       f"(check the cap in [ref_services.{s}])")
        d.mkdir(parents=True, exist_ok=True)
        projects._write_json(d / "plan.json", cur)
        (d / "checklist.md").write_text(checklist_md(cur), encoding="utf-8")
        (d / "checklist.html").write_text(checklist_html(cur), encoding="utf-8")
    cur["files"] = {n: str(d / n) for n in ("plan.json", "checklist.md", "checklist.html")}
    return cur


def load_plan(day: str) -> dict:
    f = inbox_dir(day) / "plan.json"
    if not f.is_file():
        raise FileNotFoundError(f"no plan for {day} ({f}); make one with gen ref plan --from FILE")
    return json.loads(f.read_text(encoding="utf-8"))


def _save_plan(p: dict) -> None:
    projects._write_json(inbox_dir(p["date"]) / "plan.json", p)
    d = inbox_dir(p["date"])
    (d / "checklist.md").write_text(checklist_md(p), encoding="utf-8")
    (d / "checklist.html").write_text(checklist_html(p), encoding="utf-8")


def checklist_md(p: dict) -> str:
    out = [f"# Reference images for {p['date']}", "",
           f"Save each result as the name shown, into `{p['inbox_windows']}`", "",
           "Then on tulu: `gen ref import --yes`", ""]
    for w in p.get("warnings", []):
        out.append(f"> {w}")
    for s, info in p["services"].items():
        out += ["", f"## {info.get('title', s)}: {info.get('url', '')}", ""]
        if info.get("tips"):
            out += [f"_{info['tips']}_", ""]
        for t in (t for t in p["tasks"] if t["service"] == s):
            mark = "x" if t["status"] == "imported" else " "
            out += [f"- [{mark}] **{t['title']}**, save as `{t['id']}.png`"]
            out += ["  ```", *("  " + ln for ln in t["prompt"].splitlines()), "  ```"]
            for a in t["attachments"]:
                out.append(f"  - attach: `{a['windows']}`")
    return "\n".join(out) + "\n"


def checklist_html(p: dict) -> str:
    """The same checklist as a page with copy buttons (static, opens via file://)."""
    data = json.dumps(p, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("{{DATA}}", data)


# ---------------------------------------------------------------- import

def _find(task: dict, dirs: list[Path]) -> list[Path]:
    """Files named <id>.<ext>, or <id> (1).<ext> (what a browser makes for a second save), newest first."""
    pat = re.compile(rf"^{re.escape(task['id'])}( \(\d+\))?$")
    found = [f for d in dirs if d.is_dir() for f in d.iterdir()
             if f.is_file() and f.suffix.lower() in IMAGE_EXTS and pat.match(f.stem)]
    return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)


def watch_dirs() -> list[Path]:
    """[refs] watch in config.toml: extra folders (e.g. the browser's Downloads) that are searched too."""
    return [imagegen.to_linux_path(w) for w in imagegen.load_config().get("refs", {}).get("watch", [])]


def import_inbox(day: str | None = None, apply: bool = False, watch: list[str] | None = None,
                 author: str = "human") -> dict:
    """File finished images. day=None: every day that still has tasks to do.

    Without apply nothing changes: the result says what would be imported. A task whose project or
    character is missing, or whose image is not there yet, is reported and left as todo.
    """
    days = [day] if day else sorted(f.parent.name for f in inbox_root().glob("*/plan.json")) \
        if inbox_root().is_dir() else []
    extra = [imagegen.to_linux_path(w) for w in watch or []] + watch_dirs()
    results = []
    for dday in days:
        p = load_plan(dday)
        for t in p["tasks"]:
            if t["status"] == "imported":
                continue
            r = {"day": dday, "id": t["id"], "service": t["service"], "target": t["target"]}
            files = _find(t, [inbox_dir(dday), *extra])
            if not files:
                r["status"] = "waiting"
            else:
                r["file"] = str(files[0])
                if len(files) > 1:
                    r["ignored"] = [str(f) for f in files[1:]]
                try:
                    r["status"] = "imported" if apply else "would import"
                    if apply:
                        r.update(_file_one(t, files[0], dday, author))
                        t["status"], t["imported_at"] = "imported", projects._now()
                        t["result"] = {k: v for k, v in r.items() if k in ("asset_version", "project_version",
                                                                           "character_version", "file")}
                except (FileNotFoundError, ValueError) as e:  # missing project/character, bad image
                    r["status"], r["error"] = "error", str(e)
            results.append(r)
        if apply and any(r["status"] == "imported" and r["day"] == dday for r in results):
            with projects.lock_file(inbox_root(), "refs"):
                _save_plan(p)
    return {"applied": apply, "results": results}


def _file_one(t: dict, f: Path, day: str, author: str) -> dict:
    tg = t["target"]
    ext = {"service": t["service"], "prompt": t["prompt"], "task": t["id"], "date": day}
    note = f"external ({t['service']}): {t['title']}"
    if "project" in tg:
        r = projects.promote(tg["project"], str(f), tg["asset"], note=note, as_ref=tg.get("as_ref"),
                             author=author, external=ext)
        return {"asset_version": r["asset_version"], "project_version": r["project_version"]}
    import characters
    v = characters.set_state(tg["character"], note=f"{note}. Prompt: {t['prompt'][:200]}", author=author,
                             refs={tg["ref"]: {"path": str(f), "role": tg.get("role", "")}})
    return {"character_version": v["version"]}


def status(day: str | None = None) -> list[dict]:
    days = [day] if day else sorted(f.parent.name for f in inbox_root().glob("*/plan.json")) \
        if inbox_root().is_dir() else []
    out = []
    for dday in days:
        p = load_plan(dday)
        todo = [t["id"] for t in p["tasks"] if t["status"] != "imported"]
        out.append({"day": dday, "tasks": len(p["tasks"]), "todo": todo, "inbox": p["inbox_windows"],
                    "checklist": str(inbox_dir(dday) / "checklist.html")})
    return out


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="gen ref", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan", help="write the day's checklist from a plan text")
    pp.add_argument("--from", dest="src", required=True, metavar="FILE|-", help="the plan text (JSON); - = stdin")
    pp.add_argument("--date", help="default: the date in the plan text, else today")
    pp.add_argument("--open", action="store_true", help="open the checklist in the Windows browser")
    ip = sub.add_parser("import", help="file finished images into projects/characters (dry run without --yes)")
    ip.add_argument("--date", help="default: every day with tasks still to do")
    ip.add_argument("--watch", action="append", default=[], metavar="DIR", help="also look here (e.g. Downloads)")
    ip.add_argument("-y", "--yes", action="store_true")
    sp = sub.add_parser("status", help="what is still to do")
    sp.add_argument("--date")
    sub.add_parser("services", help="the [ref_services] free tiers")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "plan":
            text = sys.stdin.read() if a.src == "-" else Path(a.src).expanduser().read_text(encoding="utf-8")
            p = plan(text, a.date)
            print(f"{len(p['tasks'])} tasks for {p['date']}; save the images into {p['inbox_windows']}")
            for w in p["warnings"]:
                print(f"warning: {w}")
            print(f"checklist: {p['files']['checklist.html']}")
            if a.open:
                imagegen.open_in_windows(p["files"]["checklist.html"])
            return 0
        if a.cmd == "import":
            r = import_inbox(a.date, a.yes, a.watch)
            for x in r["results"]:
                tg = x["target"].get("asset") or x["target"].get("ref")
                print(f"{x['status']:13} {x['id']} -> {next(iter(x['target'].values()))}/{tg}"
                      + (f"  {x['error']}" if x.get("error") else ""))
            if not r["results"]:
                print("nothing to do")
            elif not a.yes and any(x["status"] == "would import" for x in r["results"]):
                print("dry run: add --yes to import")
            return 1 if any(x["status"] == "error" for x in r["results"]) else 0
        if a.cmd == "status":
            for s in status(a.date) or []:
                print(f"{s['day']}: {s['tasks'] - len(s['todo'])}/{s['tasks']} done  {s['inbox']}")
                for i in s["todo"]:
                    print(f"  todo {i}")
            return 0
        for n, s in services().items():
            print(f"{n:10} {s.get('title', n):22} ~{s.get('daily', '?')}/day  {s.get('url', '')}")
        return 0
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"gen ref: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
