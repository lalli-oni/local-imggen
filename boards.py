"""Boards (#7): the triage view of a creative project (projects.py).

A project already holds what the #7 sketch called a board's state: prompt versions with author and
note, passes (tagged gen runs), refs and assets. This module adds what triage needs on top:

  <output_dir>/projects/<slug>/board.json   ratings and notes, plus an event log
  <output_dir>/projects/<slug>/index.html   the board page (templates/board.html), rebuilt on change

- An image is a pass image ("P:N" = pass P, image N, both from 1) or any image file on disk.
  It is stored by its path relative to output_dir, so the board survives a moved output_dir.
- Ratings are pick / maybe / reject ("clear" removes one). Notes go on an image or on the board.
- Rating or noting does not make a project version: it records an opinion, not a change of look.
- Every rating and note is appended to board.json "log" with its author, so projects.summary() can
  say what changed on the board since Claude last looked.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import imagegen
import projects

RATINGS = ("pick", "maybe", "reject")
TEMPLATE = imagegen.ROOT / "templates" / "board.html"
_PASS_ID = re.compile(r"^(\d+):(\d+)$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _root() -> Path:
    return Path(imagegen.load_config()["output_dir"]).expanduser()


def board_file(slug: str) -> Path:
    return projects.project_dir(slug) / "board.json"


def page_file(slug: str) -> Path:
    return projects.project_dir(slug) / "index.html"


def load(slug: str) -> dict:
    try:
        b = json.loads(board_file(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        b = {}
    b.setdefault("images", {})
    b.setdefault("notes", [])
    b.setdefault("log", [])
    return b


def _key(path: Path) -> str:
    """How board.json names an image: relative to output_dir when inside it, else absolute."""
    p = path.resolve()
    try:
        return p.relative_to(_root().resolve()).as_posix()
    except ValueError:
        return str(p)


def _abs(key: str) -> Path:
    p = Path(key)
    return p if p.is_absolute() else _root() / p


def _pass_images(p: dict) -> list[dict]:
    """A pass's images from its run.json ([] when the run folder is gone)."""
    try:
        run = json.loads((Path(p["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return run.get("images") or []


def resolve(proj: dict, image: str) -> tuple[str, str | None]:
    """(board key, pass id or None) for "P:N" or an image path."""
    if m := _PASS_ID.match(image.strip()):
        pi, ni = int(m[1]), int(m[2])
        if not 1 <= pi <= len(proj["passes"]):
            raise ValueError(f"no pass {pi} (the project has {len(proj['passes'])})")
        ims = _pass_images(proj["passes"][pi - 1])
        if not 1 <= ni <= len(ims):
            raise ValueError(f"pass {pi} has {len(ims)} image(s), no #{ni}")
        return _key(Path(ims[ni - 1]["path"])), f"{pi}:{ni}"
    path = imagegen.to_linux_path(image)
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() not in projects.IMAGE_EXTS:
        raise ValueError(f"{path.name}: not an image ({', '.join(sorted(projects.IMAGE_EXTS))})")
    key = _key(path)
    return key, pass_ids(proj).get(key)


def pass_ids(proj: dict) -> dict[str, str]:
    """{board key: "P:N"} for every image of every pass whose run folder still exists."""
    out = {}
    for pi, p in enumerate(proj["passes"], 1):
        for ni, im in enumerate(_pass_images(p), 1):
            out[_key(Path(im["path"]))] = f"{pi}:{ni}"
    return out


def _save(slug: str, b: dict) -> None:
    projects._write_json(board_file(slug), b)


def rate(slug: str, images: list[str], rating: str, note: str | None = None, author: str = "human") -> list[dict]:
    """Rate images pick/maybe/reject ("clear" removes the rating); an optional note goes on each."""
    if rating not in (*RATINGS, "clear"):
        raise ValueError(f"rating must be one of {', '.join(RATINGS)} or clear, got '{rating}'")
    if not images:
        raise ValueError("no images given")
    with projects.locked(slug):
        proj = projects.load(slug)
        targets = [resolve(proj, im) for im in images]  # all valid before any is written
        b = load(slug)
        out = []
        for key, pid in targets:
            e = b["images"].setdefault(key, {"notes": []})
            if rating == "clear":
                for k in ("rating", "rated_by", "rated_at"):
                    e.pop(k, None)
            else:
                e.update(rating=rating, rated_by=author, rated_at=_now())
            if note and note.strip():
                e["notes"].append({"author": author, "text": note.strip(), "at": _now()})
            b["log"].append({"at": _now(), "author": author, "action": "rate", "image": key,
                             **({"id": pid} if pid else {}), "rating": rating,
                             **({"note": note.strip()} if note and note.strip() else {})})
            out.append({"image": str(_abs(key)), **({"id": pid} if pid else {}), "rating": e.get("rating")})
        _save(slug, b)
        write_page(slug, proj, b)
    return out


def note(slug: str, text: str, image: str | None = None, author: str = "human") -> dict:
    """A note on an image (path or "P:N"), or on the board when image is None."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty note")
    with projects.locked(slug):
        proj = projects.load(slug)
        b = load(slug)
        n = {"author": author, "text": text, "at": _now()}
        if image is None:
            b["notes"].append(n)
            entry = {"at": n["at"], "author": author, "action": "note", "note": text}
        else:
            key, pid = resolve(proj, image)
            b["images"].setdefault(key, {"notes": []})["notes"].append(n)
            entry = {"at": n["at"], "author": author, "action": "note", "image": key,
                     **({"id": pid} if pid else {}), "note": text}
        b["log"].append(entry)
        _save(slug, b)
        write_page(slug, proj, b)
    return entry


def summary(proj: dict, notes: int = 10) -> dict:
    """The board part of projects.summary(): counts, every rated or noted image, recent board notes."""
    b = load(proj["slug"])
    ids = pass_ids(proj)
    counts = {r: 0 for r in RATINGS}
    images = []
    for key, e in b["images"].items():
        if e.get("rating") in counts:
            counts[e["rating"]] += 1
        if not e.get("rating") and not e.get("notes"):
            continue
        pid = ids.get(key)
        p = proj["passes"][int(pid.split(":")[0]) - 1] if pid else None
        images.append({"image": str(_abs(key)), **({"id": pid} if pid else {}),
                       **({"version": p["version"]} if p else {}),
                       **({"rating": e["rating"]} if e.get("rating") else {}),
                       **({"notes": [f"{n['author']}: {n['text']}" for n in e["notes"]]} if e.get("notes") else {})})
    order = {"pick": 0, "maybe": 1, "reject": 2, None: 3}
    images.sort(key=lambda x: (order[x.get("rating")], -x.get("version", 0)))
    counts["unrated"] = sum(1 for k in ids if not b["images"].get(k, {}).get("rating"))
    return {"page": str(page_file(proj["slug"])), "counts": counts, "images": images,
            "notes": [f"{n['author']}: {n['text']}" for n in b["notes"][-notes:]]}


# ---------------------------------------------------------------- the page

def _url(target: Path, base: Path) -> str:
    """A relative URL from the page's folder, so the page works over file:// on Windows and Linux."""
    return quote(Path(os.path.relpath(target, base)).as_posix())


def page_data(proj: dict, b: dict) -> dict:
    """Everything templates/board.html shows, with image paths relative to the page."""
    slug = proj["slug"]
    d = projects.project_dir(slug)
    s = projects.summary(slug, history=len(proj["versions"]))
    st = s["state"]
    passes = []
    for pi, p in enumerate(proj["passes"], 1):
        try:
            run = json.loads((Path(p["run_dir"]) / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            run = None
        ims = []
        for ni, im in enumerate((run or {}).get("images") or [], 1):
            path = Path(im["path"])
            e = b["images"].get(_key(path), {})
            ims.append({"id": f"{pi}:{ni}", "src": _url(path, d), "path": str(path), "seed": im.get("seed"),
                        "rating": e.get("rating"), "notes": e.get("notes", [])})
        passes.append({"n": pi, "version": p["version"], "at": p["at"], "author": p["author"],
                       "extra": p.get("extra"), "ref": p.get("ref"), "chars": p.get("chars"),
                       "prompt": (run or {}).get("prompt"), "missing": run is None,
                       "sheet": _url(Path(p["run_dir"]) / "index.html", d), "images": ims})
    in_pass = {i["path"] for p in passes for i in p["images"]}
    others = [{"src": _url(_abs(k), d), "path": str(_abs(k)), "rating": e.get("rating"), "notes": e.get("notes", [])}
              for k, e in b["images"].items() if str(_abs(k)) not in in_pass and (e.get("rating") or e.get("notes"))]
    versions = [{"version": v["version"], "at": v["at"], "author": v["author"], "note": v["note"],
                 "changed": v["changed"], "prompt": projects.full_prompt(v["state"]),
                 "negative": v["state"].get("negative"), "preset": v["state"].get("preset")}
                for v in proj["versions"]]
    return {
        "slug": slug, "title": proj["title"], "brief": proj["brief"], "version": proj["version"],
        "dir": s["dir"], "generated": _now(),
        "state": {"prompt": st["full_prompt"], "negative": st["negative"], "preset": st["preset"],
                  "params": st["params"], "palette": st["palette"], "cast": list(st["cast"]),
                  "refs": [{"name": n, "role": r["role"], "src": _url(Path(r["path"]), d)} for n, r in st["refs"].items()]},
        "assets": [{"name": n, "current": a["current"], "stale": a["stale"], "reasons": a["stale_reasons"],
                    "src": _url(Path(a["file"]), d)} for n, a in s["assets"].items()],
        "versions": versions, "passes": passes, "others": others, "notes": b["notes"],
    }


def write_page(slug: str, proj: dict | None = None, b: dict | None = None) -> Path:
    """(Re)write <project>/index.html from project.json and board.json."""
    proj = proj or projects.load(slug)
    data = page_data(proj, b if b is not None else load(slug))
    fills = {"TITLE": html.escape(f"{proj['title']} · board"),
             "BOARD_JSON": json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")}
    page = re.sub(r"\{\{(TITLE|BOARD_JSON)\}\}", lambda m: fills[m.group(1)], TEMPLATE.read_text(encoding="utf-8"))
    out = page_file(slug)
    out.write_text(page, encoding="utf-8")
    return out


def refresh(slug: str) -> None:
    """write_page, but never fails the change that triggered it (a bad run.json, a missing character)."""
    try:
        write_page(slug)
    except Exception as e:  # noqa: BLE001 - the page is a view; the state change already landed
        imagegen.log(f"warning: could not rebuild the board page for {slug}: {type(e).__name__}: {e}")
