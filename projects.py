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
import shlex
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
DOC_EXTS = {".md", ".txt"}


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
    proj = json.loads(f.read_text(encoding="utf-8"))
    proj["state"].setdefault("cast", {})  # projects made before #46
    proj["state"].setdefault("docs", {})
    return proj


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
def lock_file(root: Path, name: str):
    """An exclusive lock on <root>/.<name>.lock (process + thread). Shared with characters.py."""
    root.mkdir(parents=True, exist_ok=True)
    # next to the folders, so locking an unknown name leaves no empty folder behind
    with _THREAD_LOCK, open(root / f".{name}.lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
        except OSError:  # a filesystem without flock: the in-process lock still covers MCP threads
            pass
        yield


def locked(slug: str):
    """Hold this while doing load -> change -> save, so the MCP job thread, project_get and a
    terminal `gen project` never overwrite each other's changes."""
    return lock_file(projects_root(), _check_name(slug, "project"))


def list_projects() -> list[str]:
    root = projects_root()
    return sorted(f.parent.name for f in root.glob("*/project.json")) if root.is_dir() else []


def _empty_state() -> dict:
    return {"prompt": "", "negative": None, "preset": "draft", "params": {},
            "snippets": {}, "palette": [], "refs": {}, "cast": {}, "docs": {}}


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


def _add_doc(proj: dict, name: str, src: Path) -> None:
    """Copy a text doc into docs/, named after the version it arrives in (like refs)."""
    d = project_dir(proj["slug"]) / "docs"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{name}-v{proj['version'] + 1}{src.suffix.lower()}"
    shutil.copy2(src, dest)
    proj["state"]["docs"][name] = {"file": f"docs/{dest.name}", "source": str(src),
                                   "bytes": dest.stat().st_size}


def read_doc(slug: str, name: str) -> str:
    st = load(slug)["state"]
    if name not in st["docs"]:
        raise ValueError(f"no doc '{name}' (docs: {', '.join(st['docs']) or 'none'})")
    return (project_dir(slug) / st["docs"][name]["file"]).read_text(encoding="utf-8", errors="replace")


def _apply(proj: dict, prompt: str | None = None, negative: str | None = None,
           preset: str | None = None, params: dict | None = None,
           snippets: dict | None = None, drop_snippets: list[str] | None = None,
           palette: list[str] | None = None, refs: dict | None = None,
           drop_refs: list[str] | None = None, cast: dict | None = None,
           uncast: list[str] | None = None, docs: dict | None = None,
           drop_docs: list[str] | None = None) -> list[str]:
    """Change proj["state"] in place; returns the state keys that actually changed.

    params/snippets merge (a None value removes the key); palette replaces the list;
    refs is {name: path or {"path", "role"}}; cast is {character: pinned version or None};
    docs is {name: path to a text/markdown file} (a None/"" path removes it).
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
    for name, pin in (cast or {}).items():
        import characters
        ch = characters.load(_check_name(name, "character"))
        if pin is not None:
            pin = int(pin)
            characters.state_at(ch, pin)  # raises if there is no such version
        st["cast"][name] = {"pin": pin}
    for name in uncast or []:
        if name not in st["cast"]:
            raise ValueError(f"project does not cast '{name}' (cast: {', '.join(st['cast']) or 'nobody'})")
        del st["cast"][name]
    drop_docs = list(drop_docs or [])
    new_docs = {}
    for name, path in (docs or {}).items():  # validate every doc before copying any
        _check_name(name, "doc")
        if not path:
            drop_docs.append(name)
            continue
        src = imagegen.to_linux_path(str(path))
        if not src.is_file():
            raise FileNotFoundError(src)
        if src.suffix.lower() not in DOC_EXTS:
            raise ValueError(f"{src.name}: docs must be {', '.join(sorted(DOC_EXTS))}")
        new_docs[name] = src
    for k in drop_docs:
        if k not in st["docs"]:
            raise ValueError(f"no doc '{k}'")
        del st["docs"][k]
    for name, src in new_docs.items():
        _add_doc(proj, name, src)
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


def full_prompt(state: dict, extra: str | None = None, cast_text: list[str] = ()) -> str:
    """Cast descriptions first (the subject), then the project prompt, snippets and extra."""
    parts = [*cast_text, state["prompt"], *state["snippets"].values(), extra or ""]
    return ", ".join(p.strip().strip(",") for p in parts if p and p.strip())


def cast_versions(state: dict, strict: bool = True) -> dict:
    """{character: the version this project uses (its pin, else the character's latest)}.
    strict: a cast character that no longer exists is an error (a pass must not quietly drop it)."""
    import characters
    out = {}
    for name, c in state.get("cast", {}).items():
        try:
            ch = characters.load(name)
        except FileNotFoundError:
            if strict:
                raise FileNotFoundError(f"cast character '{name}' is missing "
                                        f"(gen char new {name} ..., or gen project set SLUG --uncast {name})") from None
            continue
        out[name] = c.get("pin") or ch["version"]
    return out


def _pick_chars(state: dict, chars: list[str] | None) -> dict:
    """The characters a pass draws: all of the cast (None), or the named ones, with their versions."""
    used = cast_versions(state)
    if chars is None:
        return used
    missing = [c for c in chars if c not in used]
    if missing:
        raise ValueError(f"not cast in this project: {', '.join(missing)} "
                         f"(cast: {', '.join(used) or 'nobody'}; add with: gen project set SLUG --cast NAME)")
    return {c: used[c] for c in chars}


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


def _ref_path(slug: str, st: dict, ref: str) -> Path:
    """A project ref by name, or CHARACTER:REF for a cast character's ref (at the version it uses)."""
    import characters
    if ":" in ref:
        name, _, r = ref.partition(":")
        used = _pick_chars(st, [name])
        cst = characters.state_at(characters.load(name), used[name])
        if r not in cst["refs"]:
            raise ValueError(f"character '{name}' has no ref '{r}' (refs: {', '.join(cst['refs']) or 'none'})")
        return characters.char_dir(name) / cst["refs"][r]["file"]
    if ref not in st["refs"]:
        raise ValueError(f"no ref '{ref}' (refs: {', '.join(st['refs']) or 'none'})")
    return project_dir(slug) / st["refs"][ref]["file"]


def run_pass(slug: str, count: int = 4, extra: str | None = None, seed: int | None = None,
             ref: str | None = None, strength: float | None = None, author: str = "human",
             chars: list[str] | None = None) -> dict:
    """Generate from the current project state (txt2img, or img2img from one of its refs).

    chars: which cast characters to draw (None = the whole cast, [] = none). Their descriptions go in
    front of the prompt. ref is a project ref, or CHARACTER:REF for one of a character's refs.
    The run is tagged with the project, version and character versions, and listed in the passes.
    """
    import characters
    proj = load(slug)
    st, v = proj["state"], proj["version"]
    used = _pick_chars(st, chars)
    loaded = {c: characters.load(c) for c in used}
    cast_text = [characters.state_at(loaded[c], ver)["description"] for c, ver in used.items()]
    prompt = full_prompt(st, extra, cast_text)
    if not prompt:
        raise ValueError(f"project '{slug}' has no prompt yet: gen project set {slug} --prompt '...'")
    if strength is not None and ref is None:
        raise ValueError("strength needs a ref (img2img from a project ref)")
    p = st["params"]
    kwargs = dict(prompt=prompt, preset=st["preset"], count=count, seed=seed, negative=st["negative"],
                  width=p.get("width"), height=p.get("height"), steps=p.get("steps"), cfg=p.get("cfg"),
                  label=f"{slug}-v{v}")
    if ref is not None:
        kwargs.update(source_image=str(_ref_path(slug, st, ref)),
                      denoise=strength if strength is not None else p.get("strength"))
    run = imagegen.generate(**kwargs)

    # tagged afterwards rather than inside generate(), so generate() needs no project knowledge
    tag = {"slug": slug, "version": v, **({"chars": used} if used else {}), **({"ref": ref} if ref else {})}
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
        imagegen.write_contact_sheet(rf.parent, run)
    except (OSError, ValueError, KeyError) as e:
        imagegen.log(f"warning: could not rebuild the contact sheet: {e}")

    with locked(slug):
        proj = load(slug)  # re-read: other changes may have landed during the run
        proj["passes"].append({"run_dir": run["run_dir"], "version": v, "at": _now(), "author": author,
                               "count": len(run["images"]), "extra": extra, "ref": ref,
                               "base_seed": run["base_seed"], **({"chars": used} if used else {})})
        save(proj)
    return run


def promote(slug: str, image: str, asset: str, note: str | None = None, as_ref: str | None = None,
            author: str = "human", shows: list[str] | None = None) -> dict:
    """Make an image the next version of an asset (and optionally a ref); commits a project version.

    The asset version records the project version the image was made from (from its sidecar when it
    came from a project pass, else the current one), which is what staleness is measured against.
    shows: the characters in the image (default: the ones its pass drew, else none).
    """
    _check_name(asset, "asset")
    if as_ref:
        _check_name(as_ref, "ref")
    src = _image(image, "assets")
    with locked(slug):
        proj = load(slug)
        r = _add_asset_version(proj, src, asset, note, author, shows)
        changed = ["assets"]
        if as_ref:
            _add_ref(proj, as_ref, project_dir(slug) / r["file"])
            changed.append("refs")
        v = _commit(proj, changed, author, note or f"promote {asset} v{r['asset_version']}")
        save(proj)
    return {**r, "file": str(project_dir(slug) / r["file"]), "project_version": v["version"]}


def _shows(proj: dict, shows: list[str] | None, origin: dict) -> dict:
    """{character: version shown}: versions come from the pass that made the image, else the cast's."""
    import characters
    drawn = origin.get("chars") or {}
    moved = proj.get("moved_to_character")
    if not drawn and moved and origin.get("version") is not None and origin["version"] < moved["version"]:
        drawn = {moved["name"]: 1}  # a pass from before from_project(): its prompt was that character's v1
    if shows is None:
        return dict(drawn)
    out = {}
    current = cast_versions(proj["state"])
    for name in shows:
        if name in drawn:
            out[name] = drawn[name]
        elif name in current:
            out[name] = current[name]
        else:
            out[name] = characters.load(_check_name(name, "character"))["version"]
    return out


def _add_asset_version(proj: dict, src: Path, asset: str, note: str | None, author: str,
                       shows: list[str] | None, imported: bool = False) -> dict:
    """Copy src in as the next version of asset (no commit; the caller commits)."""
    slug = proj["slug"]
    meta = imagegen.find_sidecar(src) or {}
    origin = meta.get("project") or {}
    if origin.get("slug") != slug:
        origin = {}
    made_from = origin.get("version")
    shown = _shows(proj, shows, origin)
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
        **({"imported": True} if imported else {}), **({"shows": shown} if shown else {}),
        **{k: meta[k] for k in ("prompt", "seed", "preset") if k in meta}})
    entry["current"], entry["kept_at"], entry["kept_chars"] = n, None, {}
    return {"asset": asset, "asset_version": n, "file": str(dest.relative_to(project_dir(slug)))}


def keep(slug: str, asset: str, note: str | None = None, author: str = "human") -> dict:
    """Mark an asset as still fine at the current project version (clears its stale flag),
    including any character changes since."""
    with locked(slug):
        proj = load(slug)
        if asset not in proj["assets"]:
            raise ValueError(f"no asset '{asset}'")
        a = proj["assets"][asset]
        cur = a["versions"][a["current"] - 1]
        used = cast_versions(proj["state"], strict=False)
        a["kept_at"] = proj["version"]
        a["kept_chars"] = {c: _char_upto(c, used) for c in cur.get("shows") or {}}
        v = _commit(proj, ["assets"], author, note or f"keep {asset} as-is")
        save(proj)
    return v


def _char_upto(name: str, used: dict) -> int | None:
    """The newest version of a character that counts for this project: its pin, else the latest."""
    import characters
    if name in used:
        return used[name]
    try:
        return characters.load(name)["version"]
    except FileNotFoundError:
        return None


def stale_reasons(proj: dict, asset: str) -> list[str]:
    """Why an asset's current version is out of date: later project versions that changed the look,
    or later versions of the characters it shows (up to the version this project casts)."""
    import characters
    a = proj["assets"][asset]
    cur = a["versions"][a["current"] - 1]
    since = max(cur["project_version"], a.get("kept_at") or 0)
    out = []
    for v in proj["versions"]:
        if v["version"] > since and (look := [k for k in v["changed"] if k in LOOK_KEYS]):
            # promoting this very image as a ref changes refs, but that does not make it stale
            if not (v["version"] == cur.get("promoted_in") and look == ["refs"]):
                out.append(f"v{v['version']} changed {', '.join(look)}")
    d = project_dir(proj["slug"])
    own = {str(d / cur["file"]), cur.get("source") or ""}
    for name, shown_v in (cur.get("shows") or {}).items():
        kept = (a.get("kept_chars") or {}).get(name) or 0
        pin = (proj["state"]["cast"].get(name) or {}).get("pin")
        if pin is not None and pin < shown_v and kept != pin:
            out.append(f"{name} is pinned at v{pin}, the asset shows v{shown_v}")
            continue
        out += characters.stale_reasons(name, max(shown_v, kept), own, pin)
    return out


def import_folder(slug: str, folder: str, apply: bool = False, shows: list[str] | None = None,
                  title: str | None = None, author: str = "human") -> dict:
    """Bring an existing body of work into a project: images become assets, .md/.txt files docs.

    Asset and doc names come from the file path (lowercased, dashes; subfolders become asset/name).
    With apply=False nothing is written, the plan is returned. Creates the project if it is missing.
    The whole import is one project version.
    """
    root = imagegen.to_linux_path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"no folder {root}")
    plan = {"slug": slug, "folder": str(root), "assets": {}, "docs": {}, "skipped": [], "applied": apply}
    for f in sorted(root.rglob("*")):
        if not f.is_file() or any(part.startswith(".") for part in f.relative_to(root).parts):
            continue
        ext = f.suffix.lower()
        parts = [_slugify(x) for x in f.relative_to(root).with_suffix("").parts]
        if ext in IMAGE_EXTS and all(parts):
            plan["assets"].setdefault("/".join(parts), []).append(str(f))
        elif ext in DOC_EXTS and all(parts):
            name = "-".join(parts)
            if name in plan["docs"]:
                plan["skipped"].append(f"{f} (doc '{name}' already taken)")
            else:
                plan["docs"][name] = str(f)
        else:
            plan["skipped"].append(str(f))
    if not plan["assets"] and not plan["docs"]:
        raise ValueError(f"nothing to import in {root} (images: {', '.join(sorted(IMAGE_EXTS))}; "
                         f"docs: {', '.join(sorted(DOC_EXTS))})")
    exists = (project_dir(slug) / "project.json").exists()
    plan["new_project"] = not exists
    if exists:
        proj = load(slug)
        plan["existing_assets"] = sorted(a for a in plan["assets"] if a in proj["assets"])
        plan["existing_docs"] = sorted(d for d in plan["docs"] if d in proj["state"]["docs"])
    if shows:
        import characters
        for c in shows:
            characters.load(_check_name(c, "character"))
    if not apply:
        return plan
    if not exists:
        new(slug, title, author=author)
    with locked(slug):
        proj = load(slug)
        changed = []
        if plan["docs"]:
            changed += _apply(proj, docs=plan["docs"])
        for asset, files in plan["assets"].items():
            for f in files:  # x.png + x.jpg: both, as versions of one asset
                _add_asset_version(proj, Path(f), asset, f"imported from {f}", author, shows, imported=True)
        if plan["assets"]:
            changed.append("assets")
        v = _commit(proj, changed, author,
                    f"import {len(plan['assets'])} assets, {len(plan['docs'])} docs from {root}")
        save(proj)
    plan["project_version"] = v["version"]
    return plan


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def summary(slug: str, mark_seen: str | None = None, history: int = 5) -> dict:
    """Everything needed to talk about a project: state, assets (with stale flags), recent history.

    mark_seen = a reader name ("claude"): "new_since_last_look" lists the versions and passes added
    since that reader's previous call, then the reader is marked as up to date.
    """
    import characters
    proj = load(slug)
    d = project_dir(slug)
    st = proj["state"]
    cast = {}
    for name, c in st["cast"].items():
        try:
            ch = characters.load(name)
        except FileNotFoundError:
            cast[name] = {"missing": True, "pin": c.get("pin")}
            continue
        ver = c.get("pin") or ch["version"]
        cst = characters.state_at(ch, ver)
        cast[name] = {"title": ch["title"], "version": ver, "latest": ch["version"], "pin": c.get("pin"),
                      "description": cst["description"], "palette": cst["palette"],
                      "refs": {n: str(characters.char_dir(name) / r["file"]) for n, r in cst["refs"].items()}}
    assets = {}
    for name, a in proj["assets"].items():
        cur = a["versions"][a["current"] - 1]
        reasons = stale_reasons(proj, name)
        assets[name] = {"current": f"v{a['current']}", "file": str(d / cur["file"]),
                        "made_from_project_version": cur["project_version"],
                        **({"shows": cur["shows"]} if cur.get("shows") else {}),
                        **({"imported": True} if cur.get("imported") else {}),
                        "stale": bool(reasons), "stale_reasons": reasons,
                        "history": [{"v": h["v"], "at": h["at"], "author": h["author"], "note": h["note"],
                                     "project_version": h["project_version"]} for h in a["versions"]]}
    out = {
        "slug": slug, "title": proj["title"], "brief": proj["brief"], "dir": str(d),
        "dir_windows": imagegen.to_windows_path(d), "version": proj["version"],
        "state": {**st, "full_prompt": full_prompt(st, cast_text=[c.get("description", "") for c in cast.values()]),
                  "refs": {n: {**r, "path": str(d / r["file"])} for n, r in st["refs"].items()},
                  "cast": cast,
                  "docs": {n: {**doc, "path": str(d / doc["file"])} for n, doc in st["docs"].items()}},
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
    ap.add_argument("--cast", action="append", metavar="CHAR[@V]",
                    help="cast a shared character (gen char), optionally pinned at version V (repeatable)")
    ap.add_argument("--doc", action="append", metavar="[NAME=]PATH",
                    help="add or replace a markdown/text doc; NAME defaults to the file name (repeatable)")
    ap.add_argument("--author", default="human")


def _cast_arg(items: list[str] | None) -> dict:
    out = {}
    for it in items or []:
        name, _, pin = it.partition("@")
        if pin and not pin.lstrip("v").isdigit():
            raise ValueError(f"--cast {it}: the pin must be a version number, e.g. {name}@3")
        out[name.strip()] = int(pin.lstrip("v")) if pin else None
    return out


def _doc_arg(items: list[str] | None) -> dict:
    out = {}
    for it in items or []:
        name, sep, path = it.partition("=")
        if not sep or imagegen.to_linux_path(it).is_file():
            name, path = _slugify(Path(it).stem), it
        out[name.strip()] = path or None
    return out


def _state_kwargs(a) -> dict:
    kw = {"prompt": a.prompt, "negative": a.negative, "preset": a.preset,
          "params": _kv(a.param, "--param") or None, "snippets": _kv(a.snippet, "--snippet") or None,
          "refs": _kv(a.ref, "--ref") or None,
          "cast": _cast_arg(a.cast) or None, "docs": _doc_arg(a.doc) or None,
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
    for n, c in st["cast"].items():
        if c.get("missing"):
            print(f"  cast {n}: MISSING (no such character)")
            continue
        ver = f"v{c['version']}" + (f", pinned (latest v{c['latest']})" if c["pin"] else "")
        print(f"  cast {n} ({ver}): {c['description'] or '(no description)'}")
    for n, doc in st["docs"].items():
        print(f"  doc {n}: {doc['file']} ({doc['bytes']} bytes)")
    if s["assets"]:
        print("assets:")
        for n, a in s["assets"].items():
            flag = f"  STALE: {'; '.join(a['stale_reasons'])}" if a["stale"] else ""
            shows = f" shows {', '.join(a['shows'])}" if a.get("shows") else ""
            origin = "imported" if a.get("imported") else f"from project v{a['made_from_project_version']}"
            print(f"  {n:20} {a['current']:4} ({origin}){shows}{flag}")
    print("history:")
    for v in s["recent_versions"]:
        print(f"  v{v['version']:<3} {v['at']}  {v['author']:6} {v['note']}  [{', '.join(v['changed'])}]")
    if s["recent_passes"]:
        print("passes:")
        for p in s["recent_passes"]:
            print(f"  v{p['version']:<3} {p['count']}x  {p['run_dir']}")


def _csv(items: list[str] | None) -> list[str] | None:
    """--shows a,b --shows c -> [a, b, c]; None when the option was not given."""
    if items is None:
        return None
    return [x.strip() for it in items for x in it.split(",") if x.strip()]


def _print_plan(plan: dict) -> None:
    verb = "imported into" if plan["applied"] else "would import into"
    print(f"{plan['folder']} -> {verb} {'new ' if plan['new_project'] else ''}project {plan['slug']}"
          + (f" (v{plan['project_version']})" if plan.get("project_version") else ""))
    existing = set(plan.get("existing_assets", [])) | set(plan.get("existing_docs", []))
    for name, files in plan["assets"].items():
        more = " (next version)" if name in existing else ""
        print(f"  asset {name}{more}: {', '.join(Path(f).name for f in files)}")
    for name, f in plan["docs"].items():
        print(f"  doc {name}{' (replaces)' if name in existing else ''}: {Path(f).name}")
    for f in plan["skipped"]:
        print(f"  skip {f}")


USAGE = """usage: gen project COMMAND ...
  new SLUG [--title T] [--brief B] [state options]   create a project (v1)
  list                                                list projects
  status SLUG                                         state, assets (stale flags), history
  set SLUG [state options] [--note N]                 change the state -> new version
  gen SLUG [-n 4] [--extra TEXT] [--char C ... | --no-chars] [--ref NAME|CHAR:REF -s 0.4] [--seed S] [--open]
                                                      a pass: generate from the project state
                                                      (cast characters' descriptions go first)
  promote SLUG IMAGE --asset NAME [--as-ref REF] [--shows C,C] [--note N]
                                                      image -> next asset version -> new project version
  keep SLUG ASSET [--note N]                          mark a stale asset as fine as-is
  import SLUG FOLDER [--shows C,C] [--title T] [--yes]
                                                      images -> assets, .md/.txt -> docs (dry run without --yes)
  doc SLUG NAME                                       print a project doc
state options: --prompt --neg -p/--preset --param K=V --snippet NAME=TEXT --palette '#hex,...' --ref NAME=PATH
               --cast CHAR[@V] --doc [NAME=]PATH; set also takes --uncast CHAR --drop-doc NAME
characters are shared between projects: gen char -h
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
        ap.add_argument("--uncast", action="append")
        ap.add_argument("--drop-doc", action="append", dest="drop_docs")
        ap.add_argument("--note")
        _state_args(ap)
        a = ap.parse_args(rest)
        kw = _state_kwargs(a)
        kw.update({k: getattr(a, k) for k in ("drop_snippets", "drop_refs", "uncast", "drop_docs")
                   if getattr(a, k)})
        v = set_state(a.slug, a.note, a.author, **kw)
        print(f"{a.slug} v{v['version']}: changed {', '.join(v['changed'])}")
        return 0
    if cmd == "gen":
        ap.add_argument("slug")
        ap.add_argument("-n", "--count", type=int, default=4)
        ap.add_argument("--extra", help="appended to the project prompt for this pass only")
        ap.add_argument("--ref", help="img2img from this project ref, or CHAR:REF for a character's ref")
        ap.add_argument("--char", action="append", dest="chars",
                        help="draw only these cast characters (repeatable; default: the whole cast)")
        ap.add_argument("--no-chars", action="store_true", help="draw none of the cast")
        ap.add_argument("-s", "--strength", type=float, help="with --ref (default: the strength param, else 0.4)")
        ap.add_argument("--seed", type=int)
        ap.add_argument("--author", default="human")
        ap.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
        a = ap.parse_args(rest)
        if a.strength is not None and a.ref is None:
            ap.error("--strength needs --ref")
        if a.no_chars and a.chars:
            ap.error("--char and --no-chars together")
        chars = [] if a.no_chars else _csv(a.chars)
        run = run_pass(a.slug, a.count, a.extra, a.seed, a.ref, a.strength, a.author, chars)
        imagegen._print_run(run, a.open)
        return 0
    if cmd == "promote":
        ap.add_argument("slug")
        ap.add_argument("image")
        ap.add_argument("--asset", required=True, help="asset name, e.g. tulu or icon/folder")
        ap.add_argument("--as-ref", help="also use the image as this project ref")
        ap.add_argument("--shows", action="append",
                        help="characters in the image, comma-separated (default: the ones its pass drew)")
        ap.add_argument("--note")
        ap.add_argument("--author", default="human")
        a = ap.parse_args(rest)
        r = promote(a.slug, a.image, a.asset, a.note, a.as_ref, a.author, _csv(a.shows))
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
    if cmd == "import":
        ap.add_argument("slug")
        ap.add_argument("folder")
        ap.add_argument("--shows", action="append", help="characters every imported image shows (comma-separated)")
        ap.add_argument("--title", help="title, when the project is new")
        ap.add_argument("-y", "--yes", action="store_true", help="import (without it: only show the plan)")
        ap.add_argument("--author", default="human")
        a = ap.parse_args(rest)
        plan = import_folder(a.slug, a.folder, a.yes, _csv(a.shows), a.title, a.author)
        _print_plan(plan)
        if not a.yes:
            print(f"dry run, nothing written. Import with: gen project import {a.slug} {shlex.quote(a.folder)} --yes")
        return 0
    if cmd == "doc":
        ap.add_argument("slug")
        ap.add_argument("name")
        a = ap.parse_args(rest)
        print(read_doc(a.slug, a.name), end="")
        return 0
    print(f"unknown project command '{cmd}'\n{USAGE}", file=sys.stderr)
    return 2
