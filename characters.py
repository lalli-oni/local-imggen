"""Shared characters (#46): a character that any creative project can cast.

A character lives in <output_dir>/characters/<name>/:
  character.json   current state + version history
  refs/            reference images (<ref>-v<N>.<ext>, never overwritten)

- state: description (the prompt text that draws the character), palette (hex) and refs.
- Every change makes a new character version that snapshots the state, like project versions.
- Projects cast characters (projects.py, state["cast"]); a pass puts the cast's descriptions in front
  of the project prompt, and records which character versions it used.
- Assets record the characters they show (and at which version). When a character changes, the assets
  that show it go stale, in every project (unless the project pins an older version), and nothing else does.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import imagegen
import projects
from projects import _HEX, _check_name, _image, _now, _write_json

LOOK_KEYS = ("description", "palette", "refs")


def characters_root() -> Path:
    return Path(imagegen.load_config()["output_dir"]).expanduser() / "characters"


def char_dir(name: str) -> Path:
    return characters_root() / _check_name(name, "character")


def locked(name: str):
    return projects.lock_file(characters_root(), _check_name(name, "character"))


def list_characters() -> list[str]:
    root = characters_root()
    return sorted(f.parent.name for f in root.glob("*/character.json")) if root.is_dir() else []


def exists(name: str) -> bool:
    return (char_dir(name) / "character.json").is_file()


def load(name: str) -> dict:
    f = char_dir(name) / "character.json"
    if not f.is_file():
        known = ", ".join(list_characters()) or "none yet"
        raise FileNotFoundError(f"no character '{name}' (known: {known})")
    return json.loads(f.read_text(encoding="utf-8"))


def save(ch: dict) -> None:
    d = char_dir(ch["name"])
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / "character.json", ch)


def state_at(ch: dict, version: int | None = None) -> dict:
    """The character's state at a version (None = current)."""
    if version is None or version == ch["version"]:
        return ch["state"]
    for v in ch["versions"]:
        if v["version"] == version:
            return v["state"]
    raise ValueError(f"character '{ch['name']}' has no v{version} (latest: v{ch['version']})")


def _commit(ch: dict, changed: list[str], author: str, note: str | None) -> dict:
    v = ch["version"] + 1
    ch["version"] = v
    ch["versions"].append({"version": v, "at": _now(), "author": author, "note": note or "",
                           "changed": changed, "state": json.loads(json.dumps(ch["state"]))})
    return ch["versions"][-1]


def _apply(ch: dict, description: str | None = None, palette: list[str] | None = None,
           refs: dict | None = None, drop_refs: list[str] | None = None) -> list[str]:
    """Change ch["state"] in place; returns the state keys that actually changed.
    refs is {name: path or {"path", "role"}}; a None/"" path removes that ref."""
    st = ch["state"]
    before = json.loads(json.dumps(st))
    if description is not None:
        st["description"] = description.strip()
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
        if not r.get("path"):
            drop_refs.append(name)
        else:
            new_refs[name] = (_image(r["path"], "refs"), r.get("role", ""))
    for k in drop_refs:
        if k not in st["refs"]:
            raise ValueError(f"character '{ch['name']}' has no ref '{k}'")
        del st["refs"][k]
    for name, (src, role) in new_refs.items():
        d = char_dir(ch["name"]) / "refs"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{name}-v{ch['version'] + 1}{src.suffix.lower()}"
        shutil.copy2(src, dest)
        st["refs"][name] = {"file": f"refs/{dest.name}", "role": role, "source": str(src)}
    return [k for k in st if st[k] != before.get(k)]


def new(name: str, title: str | None = None, author: str = "human", note: str | None = None,
        **state) -> dict:
    """Create a character at v1. state: description, palette, refs."""
    with locked(name):
        if exists(name):
            raise ValueError(f"character '{name}' already exists")
        ch = {"schema": 1, "name": name, "title": title or name, "created": _now(), "version": 0,
              "state": {"description": "", "palette": [], "refs": {}}, "versions": []}
        _apply(ch, **state)
        _commit(ch, ["created"], author, note or "created")
        save(ch)
    return ch


def set_state(name: str, note: str | None = None, author: str = "human", **changes) -> dict:
    """Change a character; commits a new character version. Assets that show it go stale."""
    with locked(name):
        ch = load(name)
        changed = _apply(ch, **changes)
        if not changed:
            raise ValueError("nothing changed")
        v = _commit(ch, changed, author, note)
        save(ch)
    return v


def _ref_sources(st: dict) -> dict:
    return {n: r.get("source") for n, r in st["refs"].items()}


def stale_reasons(name: str, since: int, own_files: set[str] = frozenset(), upto: int | None = None) -> list[str]:
    """Character versions after `since` (up to `upto`, a pin) that changed the look.

    own_files: the asset's own image paths; a version that only made one of them a ref is skipped
    (making the very image a reference does not make it out of date)."""
    try:
        ch = load(name)
    except FileNotFoundError:
        return [f"character {name} is missing"]
    own_files = {os.path.realpath(f) for f in own_files if f}
    out, prev = [], None
    for v in ch["versions"]:
        if v["version"] <= since or (upto is not None and v["version"] > upto):
            prev = v
            continue
        look = [k for k in v["changed"] if k in LOOK_KEYS]
        if look == ["refs"] and prev is not None and own_files:
            added = {n: s for n, s in _ref_sources(v["state"]).items()
                     if prev["state"]["refs"].get(n, {}).get("file") != v["state"]["refs"][n]["file"]}
            if added and not set(_ref_sources(prev["state"])) - set(_ref_sources(v["state"])) \
                    and all(s and os.path.realpath(s) in own_files for s in added.values()):
                prev = v
                continue
        if look:
            out.append(f"{name} v{v['version']} changed {', '.join(look)}")
        prev = v
    return out


def used_by(name: str) -> dict:
    """Projects that cast this character, and their assets that show it."""
    out = {}
    for slug in projects.list_projects():
        p = projects.load(slug)
        cast = p["state"].get("cast", {})
        shows = [a for a, e in p["assets"].items()
                 if name in (e["versions"][e["current"] - 1].get("shows") or {})]
        if name in cast or shows:
            out[slug] = {"pin": (cast.get(name) or {}).get("pin"), "assets": shows}
    return out


def summary(name: str, history: int = 5) -> dict:
    ch = load(name)
    d = char_dir(name)
    st = ch["state"]
    return {"name": name, "title": ch["title"], "dir": str(d), "dir_windows": imagegen.to_windows_path(d),
            "version": ch["version"],
            "state": {**st, "refs": {n: {**r, "path": str(d / r["file"])} for n, r in st["refs"].items()}},
            "used_by": used_by(name),
            "recent_versions": [{k: v[k] for k in ("version", "at", "author", "note", "changed")}
                                for v in ch["versions"][-history:]]}


def from_project(slug: str, name: str | None = None, author: str = "human", title: str | None = None) -> dict:
    """Move a single-subject project's prompt, refs and palette into a new character and cast it there.

    For projects made before characters existed (the tulu project): the passes keep the same prompt
    (cast descriptions come first), and the project's assets are recorded as showing the character,
    so nothing goes stale."""
    name = name or slug
    with projects.locked(slug):
        proj = projects.load(slug)
        st = proj["state"]
        if name in st.get("cast", {}):
            raise ValueError(f"project '{slug}' already casts '{name}'")
        if not st["prompt"]:
            raise ValueError(f"project '{slug}' has no prompt to move into a character")
        d = projects.project_dir(slug)
        ch = new(name, title or proj["title"], author, f"from project {slug} v{proj['version']}",
                 description=st["prompt"], palette=st["palette"],
                 refs={n: {"path": str(d / r["file"]), "role": r["role"]} for n, r in st["refs"].items()})
        st["prompt"], st["refs"], st["palette"] = "", {}, []
        st.setdefault("cast", {})[name] = {"pin": None}
        # images from earlier passes have no "chars" tag; promote() reads this to know they show v1
        proj["moved_to_character"] = {"name": name, "version": proj["version"] + 1}
        for e in proj["assets"].values():
            for av in e["versions"]:
                av.setdefault("shows", {})[name] = 1
        # the full prompt of a pass is unchanged, so only "cast" counts as changed (no asset goes stale)
        projects._commit(proj, ["cast"], author, f"moved prompt, refs and palette into character {name}")
        projects.save(proj)
    return ch


# ---------------------------------------------------------------- CLI

USAGE = """usage: gen char COMMAND ...
  new NAME [--title T] [--desc TEXT] [--palette '#hex,...'] [--ref NAME=PATH]
                                     create a character (v1)
  new NAME --from-project SLUG       move a project's prompt/refs/palette into a character it casts
  list                               list characters
  status NAME                        state, refs, which projects cast it, history
  set NAME [--desc ...] [--palette ...] [--ref NAME=PATH] [--drop-ref NAME] [--note N]
                                     change it -> new version; assets that show it go stale
Cast it in a project: gen project set SLUG --cast NAME"""


def _print(s: dict) -> None:
    st = s["state"]
    print(f"{s['title']} ({s['name']})  v{s['version']}")
    print(f"  folder:  {s['dir_windows'] or s['dir']}")
    print(f"  desc:    {st['description'] or '(none yet)'}")
    if st["palette"]:
        print(f"  palette: {' '.join(st['palette'])}")
    for n, r in st["refs"].items():
        print(f"  ref {n}: {r['file']}" + (f" ({r['role']})" if r["role"] else ""))
    for slug, u in s["used_by"].items():
        pin = f" (pinned at v{u['pin']})" if u["pin"] else ""
        print(f"  in {slug}{pin}: {', '.join(u['assets']) or 'no assets yet'}")
    print("history:")
    for v in s["recent_versions"]:
        print(f"  v{v['version']:<3} {v['at']}  {v['author']:6} {v['note']}  [{', '.join(v['changed'])}]")


def main(argv: list[str]) -> int:
    import argparse
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    ap = argparse.ArgumentParser(prog=f"gen char {cmd}")
    if cmd == "list":
        for n in list_characters():
            c = load(n)
            print(f"{n:24} v{c['version']:<4} {c['title']}")
        return 0
    if cmd in ("status", "show"):
        ap.add_argument("name")
        ap.add_argument("--history", type=int, default=5)
        a = ap.parse_args(rest)
        _print(summary(a.name, a.history))
        return 0
    if cmd in ("new", "set"):
        ap.add_argument("name")
        ap.add_argument("--desc", dest="description")
        ap.add_argument("--palette", help="comma-separated hex colours, replaces the palette")
        ap.add_argument("--ref", action="append", metavar="NAME=PATH", help="NAME= removes it (repeatable)")
        ap.add_argument("--author", default="human")
        if cmd == "new":
            ap.add_argument("--title")
            ap.add_argument("--from-project", metavar="SLUG")
        else:
            ap.add_argument("--drop-ref", action="append", dest="drop_refs")
            ap.add_argument("--note")
        a = ap.parse_args(rest)
        kw = {"description": a.description, "refs": projects._kv(a.ref, "--ref") or None,
              "palette": [c.strip() for c in a.palette.split(",") if c.strip()] if a.palette is not None else None}
        kw = {k: v for k, v in kw.items() if v is not None}
        if cmd == "new":
            if a.from_project:
                if kw:
                    ap.error("--from-project takes the description, refs and palette from the project")
                from_project(a.from_project, a.name, a.author, a.title)
                print(f"{a.from_project}: now casts {a.name}")
            else:
                new(a.name, a.title, a.author, **kw)
            _print(summary(a.name))
            return 0
        if a.drop_refs:
            kw["drop_refs"] = a.drop_refs
        v = set_state(a.name, a.note, a.author, **kw)
        print(f"{a.name} v{v['version']}: changed {', '.join(v['changed'])}")
        return 0
    print(f"unknown char command '{cmd}'\n{USAGE}")
    return 2
