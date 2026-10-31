"""Creative projects (#22): long-running work that owns its state and improves pass by pass.

A project lives in <output_dir>/projects/<slug>/:
  project.json   current state, version history, assets, passes
  refs/          reference images (<name>-v<N>.<ext>, never overwritten)
  assets/<name>/ promoted asset versions (v1.png, v2.png, ...)

- state: prompt, negative, preset, params (width/height/steps/cfg/strength), snippets
  (named prompt fragments appended in order), palette (hex) and refs.
- Every change (`set`, `promote`) creates a new project version that snapshots the state.
- A pass is a `gen` run made from the project state (`gen project gen`); its sidecars and run.json
  record the project and version, so a promoted image knows which version it came from.
- Assets are named entries (`tulu`, `icon/folder`) with a history. An asset is stale when a later
  project version changed the look (prompt, negative, snippets, palette or refs) and nobody has
  regenerated it or marked it as kept since.

Boards (#7) are not built yet; until then the run folders listed under "passes" stand in for them.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path

import imagegen

SCHEMA = 1
# changes to these make assets from older versions stale; preset/params changes do not
LOOK_KEYS = ("prompt", "negative", "snippets", "palette", "refs")
PARAM_KEYS = {"width": int, "height": int, "steps": int, "cfg": float, "strength": float}
PARAM_RANGES = {"width": (256, 4096), "height": (256, 4096), "steps": (1, 150), "cfg": (0.0, 30.0),
                "strength": (0.01, 1.0)}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def projects_root() -> Path:
    return Path(imagegen.load_config()["output_dir"]).expanduser() / "projects"


def _check_name(name: str, what: str) -> str:
    """Slugs are one segment; asset names may have several (icon/folder)."""
    parts = name.split("/") if what == "asset" else [name]
    if not name or not all(_SLUG.match(p) for p in parts):
        raise ValueError(f"bad {what} name '{name}': use lowercase letters, digits and dashes"
                         + (", '/' between parts" if what == "asset" else ""))
    return name


def project_dir(slug: str) -> Path:
    return projects_root() / _check_name(slug, "project")


def load(slug: str) -> dict:
    f = project_dir(slug) / "project.json"
    if not f.is_file():
        known = ", ".join(list_projects()) or "none yet"
        raise FileNotFoundError(f"no project '{slug}' (known: {known})")
    return json.loads(f.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    # unique tmp + replace: a crash never leaves half a file, and two writers never share a tmp
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save(proj: dict) -> None:
    d = project_dir(proj["slug"])
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / "project.json", proj)


_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def locked(slug: str):
    """Hold this while doing load -> change -> save, so the MCP job thread, project_get and a
    terminal `gen project` never overwrite each other's changes."""
    root = projects_root()
    root.mkdir(parents=True, exist_ok=True)
    # next to the project folders, so locking an unknown slug leaves no empty folder behind
    with _THREAD_LOCK, open(root / f".{_check_name(slug, 'project')}.lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
        except OSError:  # a filesystem without flock: the in-process lock still covers MCP threads
            pass
        yield


def list_projects() -> list[str]:
    root = projects_root()
    return sorted(f.parent.name for f in root.glob("*/project.json")) if root.is_dir() else []


def _empty_state() -> dict:
    return {"prompt": "", "negative": None, "preset": "draft", "params": {},
            "snippets": {}, "palette": [], "refs": {}}


def _commit(proj: dict, changed: list[str], author: str, note: str | None) -> dict:
    """Append a version that snapshots the current state (and which asset versions are current)."""
    v = proj["version"] + 1 if proj["versions"] else 1
    proj["version"] = v
    proj["versions"].append({
        "version": v, "at": _now(), "author": author, "note": note or "", "changed": changed,
        "state": json.loads(json.dumps(proj["state"])),
        "assets": {n: a["current"] for n, a in proj["assets"].items()}})
    return proj["versions"][-1]


def new(slug: str, title: str | None = None, brief: str = "", author: str = "human",
        **state) -> dict:
    """Create a project at version 1. state: any of set_state()'s arguments."""
    with locked(slug):
        if (project_dir(slug) / "project.json").exists():
            raise ValueError(f"project '{slug}' already exists")
        proj = {"schema": SCHEMA, "slug": slug, "title": title or slug, "brief": brief,
                "created": _now(), "version": 0, "state": _empty_state(),
                "versions": [], "assets": {}, "passes": []}
        _apply(proj, **state)
        _commit(proj, ["created"], author, "created")
        save(proj)
    return proj


def _image(path: str, what: str) -> Path:
    src = imagegen.to_linux_path(path)
    if not src.is_file():
        raise FileNotFoundError(src)
    if src.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"{src.name}: {what} must be {', '.join(sorted(IMAGE_EXTS))}")
    return src


def _add_ref(proj: dict, name: str, src: Path, role: str = "") -> None:
    """Copy an already validated image (see _image) into refs/ and point the state at it."""
    refs = project_dir(proj["slug"]) / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    # named after the version it arrives in, so older snapshots keep pointing at their own file
    dest = refs / f"{name}-v{proj['version'] + 1}{src.suffix.lower()}"
    shutil.copy2(src, dest)
    proj["state"]["refs"][name] = {"file": f"refs/{dest.name}", "role": role, "source": str(src)}


def _apply(proj: dict, prompt: str | None = None, negative: str | None = None,
           preset: str | None = None, params: dict | None = None,
           snippets: dict | None = None, drop_snippets: list[str] | None = None,
           palette: list[str] | None = None, refs: dict | None = None,
           drop_refs: list[str] | None = None) -> list[str]:
    """Change proj["state"] in place; returns the state keys that actually changed.

    params/snippets merge (a None value removes the key); palette replaces the list;
    refs is {name: path or {"path", "role"}}.
    """
    st = proj["state"]
    before = json.loads(json.dumps(st))
    if prompt is not None:
        st["prompt"] = prompt.strip()
    if negative is not None:
        st["negative"] = negative.strip() or None  # "" = back to the preset's default negative
    if preset is not None:
        if preset not in imagegen.list_presets():
            raise ValueError(f"unknown preset '{preset}'. Available: {', '.join(imagegen.list_presets())}")
        st["preset"] = preset
    for k, v in (params or {}).items():
        if k not in PARAM_KEYS:
            raise ValueError(f"unknown param '{k}' (known: {', '.join(PARAM_KEYS)})")
        if v is None:
            st["params"].pop(k, None)
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"param {k}={v!r} is not a number") from None
        if PARAM_KEYS[k] is int and not num.is_integer():
            raise ValueError(f"param {k} must be a whole number, got {v}")
        lo, hi = PARAM_RANGES[k]
        if not lo <= num <= hi:
            raise ValueError(f"param {k} must be between {lo} and {hi}, got {v}")
        if k in ("width", "height") and num % 8:
            raise ValueError(f"param {k} must be a multiple of 8, got {v}")
        st["params"][k] = PARAM_KEYS[k](num)
    for k, v in (snippets or {}).items():
        _check_name(k, "snippet")
        if v is None or not str(v).strip():
            st["snippets"].pop(k, None)
        else:
            st["snippets"][k] = str(v).strip()
    for k in drop_snippets or []:
        if k not in st["snippets"]:
            raise ValueError(f"no snippet '{k}'")
        del st["snippets"][k]
    if palette is not None:
        bad = [c for c in palette if not _HEX.match(c)]
        if bad:
            raise ValueError(f"not hex colours: {', '.join(bad)} (use #rgb or #rrggbb)")
        st["palette"] = [c.lower() for c in palette]
    drop_refs = list(drop_refs or [])
    new_refs = {}
    for name, r in (refs or {}).items():  # validate every ref before copying any
        _check_name(name, "ref")
        r = r if isinstance(r, dict) else {"path": r}
        if not r.get("path"):  # NAME= / null removes it, like params and snippets
            drop_refs.append(name)
        else:
            new_refs[name] = (_image(r["path"], "refs"), r.get("role", ""))
    for k in drop_refs:
        if k not in st["refs"]:
            raise ValueError(f"no ref '{k}'")
        del st["refs"][k]
    for name, (src, role) in new_refs.items():
        _add_ref(proj, name, src, role)
    return [k for k in st if st[k] != before.get(k)]


def set_state(slug: str, note: str | None = None, author: str = "human", **changes) -> dict:
    """Change the project state and commit it as a new version. Returns the new version entry."""
    with locked(slug):
        proj = load(slug)
        changed = _apply(proj, **changes)
        if not changed:
            raise ValueError("nothing changed")
        v = _commit(proj, changed, author, note)
        save(proj)
    return v


def full_prompt(state: dict, extra: str | None = None) -> str:
    parts = [state["prompt"], *state["snippets"].values(), extra or ""]
    return ", ".join(p.strip().strip(",") for p in parts if p and p.strip())


def _tag_json(f: Path, tag: dict) -> None:
    """Add "project": tag to a sidecar or run.json written by generate(); unreadable ones are skipped."""
    try:
        meta = json.loads(f.read_text())
    except (OSError, ValueError):
        imagegen.log(f"warning: could not tag {f} with the project")
        return
    if isinstance(meta, dict):
        meta["project"] = tag
        f.write_text(json.dumps(meta, indent=2))


def run_pass(slug: str, count: int = 4, extra: str | None = None, seed: int | None = None,
             ref: str | None = None, strength: float | None = None, author: str = "human") -> dict:
    """Generate from the current project state (txt2img, or img2img from one of its refs).

    The run is tagged with the project and version, and listed in the project's passes.
    """
    proj = load(slug)
    st, v = proj["state"], proj["version"]
    prompt = full_prompt(st, extra)
    if not prompt:
        raise ValueError(f"project '{slug}' has no prompt yet: gen project set {slug} --prompt '...'")
    if strength is not None and ref is None:
        raise ValueError("strength needs a ref (img2img from a project ref)")
    p = st["params"]
    kwargs = dict(prompt=prompt, preset=st["preset"], count=count, seed=seed, negative=st["negative"],
                  width=p.get("width"), height=p.get("height"), steps=p.get("steps"), cfg=p.get("cfg"),
                  label=f"{slug}-v{v}")
    if ref is not None:
        if ref not in st["refs"]:
            raise ValueError(f"no ref '{ref}' (refs: {', '.join(st['refs']) or 'none'})")
        kwargs.update(source_image=str(project_dir(slug) / st["refs"][ref]["file"]),
                      denoise=strength if strength is not None else p.get("strength"))
    run = imagegen.generate(**kwargs)

    # tagged afterwards rather than inside generate(), so generate() needs no project knowledge
    tag = {"slug": slug, "version": v, **({"ref": ref} if ref else {})}
    for im in run["images"]:
        img = Path(im["path"])
        for f in (img.with_suffix(".json"), img.with_name(img.stem.removesuffix("-transparent") + ".json")):
            if f.is_file():
                _tag_json(f, tag)
                break
    rf = Path(run["run_dir"]) / "run.json"
    _tag_json(rf, tag)
    run["project"] = tag
    try:  # re-write the sheet so it knows the project (its promote buttons need that)
        imagegen.write_contact_sheet(rf.parent, json.loads(rf.read_text()))
    except (OSError, ValueError, KeyError) as e:
        imagegen.log(f"warning: could not rebuild the contact sheet: {e}")

    with locked(slug):
        proj = load(slug)  # re-read: other changes may have landed during the run
        proj["passes"].append({"run_dir": run["run_dir"], "version": v, "at": _now(), "author": author,
                               "count": len(run["images"]), "extra": extra, "ref": ref,
                               "base_seed": run["base_seed"]})
        save(proj)
    return run


def promote(slug: str, image: str, asset: str, note: str | None = None, as_ref: str | None = None,
            author: str = "human") -> dict:
    """Make an image the next version of an asset (and optionally a ref); commits a project version.

    The asset version records the project version the image was made from (from its sidecar when it
    came from a project pass, else the current one), which is what staleness is measured against.
    """
    _check_name(asset, "asset")
    if as_ref:
        _check_name(as_ref, "ref")
    src = _image(image, "assets")
    with locked(slug):
        return _promote(load(slug), src, asset, note, as_ref, author)


def _promote(proj: dict, src: Path, asset: str, note: str | None, as_ref: str | None, author: str) -> dict:
    slug = proj["slug"]
    meta = imagegen.find_sidecar(src) or {}
    origin = meta.get("project") or {}
    made_from = origin.get("version") if origin.get("slug") == slug else None
    entry = proj["assets"].setdefault(asset, {"current": 0, "kept_at": None, "versions": []})
    n = len(entry["versions"]) + 1
    dest_dir = project_dir(slug) / "assets" / asset
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"v{n}{src.suffix.lower()}"
    shutil.copy2(src, dest)
    entry["versions"].append({
        "v": n, "file": str(dest.relative_to(project_dir(slug))), "source": str(src), "at": _now(),
        "author": author, "note": note or "",
        "project_version": made_from or proj["version"],
        "from_pass": made_from is not None, "promoted_in": proj["version"] + 1,
        **{k: meta[k] for k in ("prompt", "seed", "preset") if k in meta}})
    entry["current"], entry["kept_at"] = n, None
    changed = ["assets"]
    if as_ref:
        _add_ref(proj, as_ref, dest)
        changed.append("refs")
    v = _commit(proj, changed, author, note or f"promote {asset} v{n}")
    save(proj)
    return {"asset": asset, "asset_version": n, "file": str(dest), "project_version": v["version"]}


def keep(slug: str, asset: str, note: str | None = None, author: str = "human") -> dict:
    """Mark an asset as still fine at the current project version (clears its stale flag)."""
    with locked(slug):
        proj = load(slug)
        if asset not in proj["assets"]:
            raise ValueError(f"no asset '{asset}'")
        proj["assets"][asset]["kept_at"] = proj["version"]
        v = _commit(proj, ["assets"], author, note or f"keep {asset} as-is")
        save(proj)
    return v


def stale_reasons(proj: dict, asset: str) -> list[str]:
    """Why an asset's current version is out of date: later versions that changed the look."""
    a = proj["assets"][asset]
    cur = a["versions"][a["current"] - 1]
    since = max(cur["project_version"], a.get("kept_at") or 0)
    out = []
    for v in proj["versions"]:
        if v["version"] > since and (look := [k for k in v["changed"] if k in LOOK_KEYS]):
            # promoting this very image as a ref changes refs, but that does not make it stale
            if not (v["version"] == cur.get("promoted_in") and look == ["refs"]):
                out.append(f"v{v['version']} changed {', '.join(look)}")
    return out


def summary(slug: str, mark_seen: str | None = None, history: int = 5) -> dict:
    """Everything needed to talk about a project: state, assets (with stale flags), recent history.

    mark_seen = a reader name ("claude"): "new_since_last_look" lists the versions and passes added
    since that reader's previous call, then the reader is marked as up to date.
    """
    proj = load(slug)
    d = project_dir(slug)
    st = proj["state"]
    assets = {}
    for name, a in proj["assets"].items():
        cur = a["versions"][a["current"] - 1]
        reasons = stale_reasons(proj, name)
        assets[name] = {"current": f"v{a['current']}", "file": str(d / cur["file"]),
                        "made_from_project_version": cur["project_version"],
                        "stale": bool(reasons), "stale_reasons": reasons,
                        "history": [{"v": h["v"], "at": h["at"], "author": h["author"], "note": h["note"],
                                     "project_version": h["project_version"]} for h in a["versions"]]}
    out = {
        "slug": slug, "title": proj["title"], "brief": proj["brief"], "dir": str(d),
        "dir_windows": imagegen.to_windows_path(d), "version": proj["version"],
        "state": {**st, "full_prompt": full_prompt(st),
                  "refs": {n: {**r, "path": str(d / r["file"])} for n, r in st["refs"].items()}},
        "assets": assets,
        "recent_versions": [{k: v[k] for k in ("version", "at", "author", "note", "changed")}
                            for v in proj["versions"][-history:]],
        "recent_passes": proj["passes"][-history:],
    }
    if mark_seen:
        # kept out of project.json so that reading a project never rewrites its state
        seen_file = d / "seen.json"
        with locked(slug):
            try:
                seen_all = json.loads(seen_file.read_text())
            except (OSError, ValueError):
                seen_all = {}
            seen = seen_all.get(mark_seen)
            now = {"version": proj["version"], "passes": len(proj["passes"])}
            if seen != now:
                _write_json(seen_file, {**seen_all, mark_seen: now})
        if seen is not None:
            out["new_since_last_look"] = {
                "since": seen,
                "versions": [{k: v[k] for k in ("version", "author", "note", "changed")}
                             for v in proj["versions"] if v["version"] > seen["version"]],
                "passes": proj["passes"][seen["passes"]:]}
    return out


# ---------------------------------------------------------------- CLI

def _kv(items: list[str] | None, what: str) -> dict:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise ValueError(f"{what} must be NAME=VALUE, got '{it}'")
        out[k.strip()] = v if v != "" else None  # NAME= removes it
    return out


def _state_args(ap) -> None:
    ap.add_argument("--prompt")
    ap.add_argument("--neg", dest="negative", help="negative prompt ('' = preset default)")
    ap.add_argument("-p", "--preset")
    ap.add_argument("--param", action="append", metavar="K=V",
                    help=f"one of {', '.join(PARAM_KEYS)}; K= removes it (repeatable)")
    ap.add_argument("--snippet", action="append", metavar="NAME=TEXT",
                    help="named prompt fragment, appended in order; NAME= removes it (repeatable)")
    ap.add_argument("--palette", help="comma-separated hex colours, replaces the palette")
    ap.add_argument("--ref", action="append", metavar="NAME=PATH",
                    help="add or replace a reference image (repeatable)")
    ap.add_argument("--author", default="human")


def _state_kwargs(a) -> dict:
    kw = {"prompt": a.prompt, "negative": a.negative, "preset": a.preset,
          "params": _kv(a.param, "--param") or None, "snippets": _kv(a.snippet, "--snippet") or None,
          "refs": _kv(a.ref, "--ref") or None,
          "palette": [c.strip() for c in a.palette.split(",") if c.strip()] if a.palette is not None else None}
    return {k: v for k, v in kw.items() if v is not None}


def _print_status(s: dict) -> None:
    st = s["state"]
    print(f"{s['title']} ({s['slug']})  v{s['version']}")
    if s["brief"]:
        print(f"  brief:    {s['brief']}")
    print(f"  folder:   {s['dir_windows'] or s['dir']}")
    print(f"  preset:   {st['preset']}  {' '.join(f'{k}={v}' for k, v in st['params'].items())}")
    print(f"  prompt:   {st['full_prompt'] or '(none yet)'}")
    if st["negative"]:
        print(f"  negative: {st['negative']}")
    if st["snippets"]:
        print(f"  snippets: {', '.join(st['snippets'])}")
    if st["palette"]:
        print(f"  palette:  {' '.join(st['palette'])}")
    for n, r in st["refs"].items():
        print(f"  ref {n}: {r['file']}" + (f" ({r['role']})" if r["role"] else ""))
    if s["assets"]:
        print("assets:")
        for n, a in s["assets"].items():
            flag = f"  STALE: {'; '.join(a['stale_reasons'])}" if a["stale"] else ""
            print(f"  {n:20} {a['current']:4} (from project v{a['made_from_project_version']}){flag}")
    print("history:")
    for v in s["recent_versions"]:
        print(f"  v{v['version']:<3} {v['at']}  {v['author']:6} {v['note']}  [{', '.join(v['changed'])}]")
    if s["recent_passes"]:
        print("passes:")
        for p in s["recent_passes"]:
            print(f"  v{p['version']:<3} {p['count']}x  {p['run_dir']}")


USAGE = """usage: gen project COMMAND ...
  new SLUG [--title T] [--brief B] [state options]   create a project (v1)
  list                                                list projects
  status SLUG                                         state, assets (stale flags), history
  set SLUG [state options] [--note N]                 change the state -> new version
  gen SLUG [-n 4] [--extra TEXT] [--ref NAME -s 0.4] [--seed S] [--open]
                                                      a pass: generate from the project state
  promote SLUG IMAGE --asset NAME [--as-ref REF] [--note N]
                                                      image -> next asset version -> new project version
  keep SLUG ASSET [--note N]                          mark a stale asset as fine as-is
state options: --prompt --neg -p/--preset --param K=V --snippet NAME=TEXT --palette '#hex,...' --ref NAME=PATH
(gen project COMMAND -h for details)"""


def main(argv: list[str]) -> int:
    import argparse
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    ap = argparse.ArgumentParser(prog=f"gen project {cmd}")
    if cmd == "list":
        for slug in list_projects():
            p = load(slug)
            print(f"{slug:24} v{p['version']:<4} {p['title']}")
        return 0
    if cmd in ("status", "show"):
        ap.add_argument("slug")
        ap.add_argument("--history", type=int, default=5)
        a = ap.parse_args(rest)
        _print_status(summary(a.slug, history=a.history))
        return 0
    if cmd == "new":
        ap.add_argument("slug")
        ap.add_argument("--title")
        ap.add_argument("--brief", default="")
        _state_args(ap)
        a = ap.parse_args(rest)
        new(a.slug, a.title, a.brief, a.author, **_state_kwargs(a))
        _print_status(summary(a.slug))
        return 0
    if cmd == "set":
        ap.add_argument("slug")
        ap.add_argument("--drop-snippet", action="append", dest="drop_snippets")
        ap.add_argument("--drop-ref", action="append", dest="drop_refs")
        ap.add_argument("--note")
        _state_args(ap)
        a = ap.parse_args(rest)
        kw = _state_kwargs(a)
        kw.update({k: getattr(a, k) for k in ("drop_snippets", "drop_refs") if getattr(a, k)})
        v = set_state(a.slug, a.note, a.author, **kw)
        print(f"{a.slug} v{v['version']}: changed {', '.join(v['changed'])}")
        return 0
    if cmd == "gen":
        ap.add_argument("slug")
        ap.add_argument("-n", "--count", type=int, default=4)
        ap.add_argument("--extra", help="appended to the project prompt for this pass only")
        ap.add_argument("--ref", help="img2img from this project ref")
        ap.add_argument("-s", "--strength", type=float, help="with --ref (default: the strength param, else 0.4)")
        ap.add_argument("--seed", type=int)
        ap.add_argument("--author", default="human")
        ap.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
        a = ap.parse_args(rest)
        if a.strength is not None and a.ref is None:
            ap.error("--strength needs --ref")
        run = run_pass(a.slug, a.count, a.extra, a.seed, a.ref, a.strength, a.author)
        imagegen._print_run(run, a.open)
        return 0
    if cmd == "promote":
        ap.add_argument("slug")
        ap.add_argument("image")
        ap.add_argument("--asset", required=True, help="asset name, e.g. tulu or icon/folder")
        ap.add_argument("--as-ref", help="also use the image as this project ref")
        ap.add_argument("--note")
        ap.add_argument("--author", default="human")
        a = ap.parse_args(rest)
        r = promote(a.slug, a.image, a.asset, a.note, a.as_ref, a.author)
        print(f"{a.slug} v{r['project_version']}: {r['asset']} v{r['asset_version']} -> {r['file']}")
        return 0
    if cmd == "keep":
        ap.add_argument("slug")
        ap.add_argument("asset")
        ap.add_argument("--note")
        ap.add_argument("--author", default="human")
        a = ap.parse_args(rest)
        v = keep(a.slug, a.asset, a.note, a.author)
        print(f"{a.slug} v{v['version']}: {a.asset} kept as-is")
        return 0
    print(f"unknown project command '{cmd}'\n{USAGE}", file=sys.stderr)
    return 2
