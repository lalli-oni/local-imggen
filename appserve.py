"""gen app serve (#82): the viewer app. A read-only JSON API over everything imagegen made, plus the
built SvelteKit PWA from app/build, on 127.0.0.1.

    GET /api/overview                  counts, the latest wallpaper, recent runs
    GET /api/projects                  every project: title, version, cast, stage, counts, cover image
    GET /api/projects/<slug>           one project: board data (passes, ratings, notes), assets, versions
    GET /api/characters                every character: stage, description, first ref, projects using it
    GET /api/characters/<name>         one character: refs, palette, versions (with their refs)
    GET /api/prompts                   the prompt material in one place (#99): daily themes with their use, props, styles,
                                       preset suffixes, templates, stage instructions, character and project prompts
    GET /api/runs?offset&limit&preset&q&wallpaper=1    the run history, newest first
    GET /api/runs/<date>/<run>         one run with each image's sidecar (prompt, seed, params, timing)
    GET /api/wallpapers                wallpaper runs (daily ones with theme/props) + daily.log
    GET /files/<path under output_dir> images and contact sheets (boardserve's rules)
    anything else                      the app (app/build), index.html for client-side routes

Read-only on purpose: no POST, no jobs, nothing that runs the CLI (that comes later, see #82).
Reuses boardserve.Handler for the Host check (DNS rebinding) and the output_dir file rules.
Stdlib only. `npm run build` in app/ makes app/build; `npm run dev` there proxies to this server.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import boards
import boardserve
import characters
import daily
import imagegen
import projects
import stages

DEFAULT_PORT = 8191
APP_DIR = imagegen.ROOT / "app" / "build"
MAX_LIMIT = 200
LOG_LINES = 60
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8", ".woff2": "font/woff2"}
class BadRequest(ValueError):
    """A bad query from the client (400). Any other error, such as a broken json on disk, is a 500."""


_RUN_NAME = re.compile(r"^(\d{6})-(.+)$")
_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (ok|FAILED)\b[:,]?\s*(.*)$")


class Paths:
    """Turns files under output_dir into /files/ URLs (None for anything outside it)."""

    def __init__(self, root: Path):
        self.raw = root.expanduser()
        self.root = self.raw.resolve()

    def url(self, path: str | Path | None) -> str | None:
        if not path:
            return None
        p = Path(os.path.normpath(path))
        for base in (self.root, self.raw):  # no resolve() per image: /mnt/c makes every syscall slow
            try:
                return "/files/" + quote(p.relative_to(base).as_posix())
            except ValueError:
                pass
        try:
            return "/files/" + quote(p.resolve().relative_to(self.root).as_posix())
        except (ValueError, OSError):
            return None


# ---------------------------------------------------------------- runs

class RunIndex:
    """Parsed run.json summaries, re-read only when a run.json changes (a page of runs = one glob + stats)."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._cache: dict[Path, tuple[int, dict]] = {}
        self._lock = threading.Lock()

    def files(self) -> list[Path]:
        # newest first by folder names (date, then HHMMSS), which needs no stat
        fs = self.paths.root.glob("????-??-??/*/run.json")
        return sorted(fs, key=lambda f: (f.parent.parent.name, f.parent.name), reverse=True)

    def get(self, f: Path) -> dict | None:
        try:
            m = f.stat().st_mtime_ns
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(f)
        if hit and hit[0] == m:
            return hit[1]
        try:
            s = run_summary(f, self.paths)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None  # a run being written, or one from an older layout: leave it out
        with self._lock:
            self._cache[f] = (m, s)
        return s

    def all(self) -> list[dict]:
        files = self.files()
        with self._lock:  # forget runs that were deleted or moved
            for gone in set(self._cache) - set(files):
                del self._cache[gone]
        return [s for f in files if (s := self.get(f))]


def is_wallpaper(run: dict) -> bool:
    return bool(run.get("daily")) or str(run.get("preset", "")).startswith("wallpaper")


def run_summary(f: Path, paths: Paths) -> dict:
    r = json.loads(f.read_text(encoding="utf-8"))
    d = f.parent
    m = _RUN_NAME.match(d.name)
    hhmmss, label = (m.group(1), m.group(2)) if m else ("", d.name)
    images = []
    for im in r.get("images") or []:
        p = d / Path(im["path"]).name  # the folder we found, even if output_dir moved since
        images.append({"name": p.name, "url": paths.url(p), "seed": im.get("seed"),
                       **({"blank": True} if im.get("blank") else {}),
                       **({"scene": paths.url(d / Path(im["scene"]).name)} if im.get("scene") else {}),
                       **({"prompt": im["prompt"]} if im.get("prompt") else {})})
    daily = r.get("daily") if isinstance(r.get("daily"), dict) else None
    return {"id": f"{d.parent.name}/{d.name}", "date": d.parent.name,
            "time": f"{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}" if hhmmss else "",
            "label": label, "preset": r.get("preset"), "styles": r.get("styles") or [], "prompt": r.get("prompt"),
            "count": len(images), "images": images, "transparent": bool(r.get("transparent")),
            # not checked here: the summary is cached per run.json, and the sheet is written after it
            # (and rebuilt by gen sheet); run_detail checks it
            "sheet": paths.url(d / "index.html"),
            "timing": (r.get("timing") or {}).get("summary"),
            "seconds": (r.get("timing") or {}).get("total_s"),
            "source": paths.url(r["source_image"]) if r.get("source_image") else None,
            "daily": {k: daily.get(k) for k in ("date", "theme", "from", "props", "cast", "styles")} if daily else None,
            "wallpaper": is_wallpaper(r)}


def run_detail(index: RunIndex, run_id: str) -> dict | None:
    parts = run_id.split("/")
    if len(parts) != 2 or not re.fullmatch(r"\d{4}-\d\d-\d\d", parts[0]) or parts[1] in ("", ".", ".."):
        return None
    f = index.paths.root / parts[0] / parts[1] / "run.json"
    s = index.get(f) if f.is_file() else None
    if not s:
        return None
    images = []
    for im in s["images"]:
        side = f.parent / im["name"]
        meta = {}
        for cand in (side.with_suffix(".json"), f.parent / (im["name"].split("-transparent")[0].split("-crt")[0] + ".json")):
            try:
                meta = json.loads(cand.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        images.append({**im, "full_prompt": meta.get("full_prompt"), "negative": meta.get("negative"),
                       "params": meta.get("params"), "created": meta.get("created"),
                       "timing": (meta.get("timing") or {}).get("total_s")})
    sheet = f.parent / "index.html"
    return {**s, "images": images, "sheet": index.paths.url(sheet) if sheet.is_file() else None}


def runs_page(index: RunIndex, q: dict) -> dict:
    def arg(k, default=""):
        return (q.get(k) or [default])[0]
    try:
        offset = max(0, int(arg("offset", "0")))
        limit = min(MAX_LIMIT, max(1, int(arg("limit", "50"))))
    except ValueError:
        raise BadRequest("offset and limit: whole numbers")
    runs = index.all()
    presets = sorted({r["preset"] for r in runs if r["preset"]})
    if preset := arg("preset"):
        runs = [r for r in runs if r["preset"] == preset]
    if arg("wallpaper") in ("1", "true"):
        runs = [r for r in runs if r["wallpaper"]]
    if text := arg("q").strip().lower():
        runs = [r for r in runs if text in (r["prompt"] or "").lower() or text in r["label"].lower()
                or text in ((r["daily"] or {}).get("theme") or "").lower()]
    return {"total": len(runs), "offset": offset, "limit": limit, "presets": presets,
            "runs": runs[offset:offset + limit]}


def daily_log(paths: Paths, n: int = LOG_LINES) -> list[dict]:
    try:
        lines = (paths.raw / "daily.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-n:]):
        m = _LOG_LINE.match(line)
        out.append({"at": m.group(1), "ok": m.group(2) == "ok", "text": m.group(3)} if m
                   else {"at": None, "ok": None, "text": line})
    return out


# ---------------------------------------------------------------- projects and characters

def _stage(st: dict) -> dict | None:
    try:
        return stages.of_project(st)
    except (OSError, ValueError, KeyError):
        return None


def project_card(slug: str, paths: Paths) -> dict:
    proj = projects.load(slug)
    st = proj["state"]
    b = boards.load(slug)
    ids = boards.pass_ids(proj)
    stale = 0
    for name in proj["assets"]:
        try:
            stale += bool(projects.stale_reasons(proj, name))
        except (OSError, ValueError, KeyError):
            pass
    # the cover: the newest pick, else the newest asset, else the last pass's first image
    cover = None
    picks = [k for k, e in b["images"].items() if e.get("rating") == "pick" and k in ids]
    if picks:
        cover = paths.url(boards._abs(max(picks, key=lambda k: tuple(map(int, ids[k].split(":"))))))
    if not cover and proj["assets"]:
        a = list(proj["assets"].values())[-1]
        cover = paths.url(projects.project_dir(slug) / a["versions"][a["current"] - 1]["file"])
    if not cover:
        def newest_pass_first(kv):
            p, n = map(int, kv[1].split(":"))
            return -p, n
        for k, _ in sorted(ids.items(), key=newest_pass_first):
            cover = paths.url(boards._abs(k))
            break
    times = [v["at"] for v in proj["versions"]] + [p["at"] for p in proj["passes"]] + \
            [n.get("at", "") for n in b.get("log", [])]
    return {"slug": slug, "title": proj["title"], "brief": proj["brief"], "version": proj["version"],
            "cast": list(st.get("cast") or {}), "stage": _stage(st), "passes": len(proj["passes"]),
            "assets": len(proj["assets"]), "stale": stale, "counts": boards.counts(b, ids),
            "cover": cover, "updated": max((t for t in times if t), default=None)}


def project_detail(slug: str, paths: Paths) -> dict:
    proj = projects.load(slug)
    data = boards.page_data(proj, boards.load(slug))
    d = projects.project_dir(slug)

    def url(src: str | None) -> str | None:  # page_data's URLs are relative to the project folder
        return paths.url(d / unquote(src)) if src else None

    for p in data["passes"]:
        p["sheet"] = url(p["sheet"])
        for im in p["images"]:
            im["url"] = paths.url(im.pop("path"))
            im.pop("src", None)
    for o in data["others"]:
        o["url"] = paths.url(o.pop("path"))
        o.pop("src", None)
    for r in data["state"]["refs"]:
        r["url"] = url(r.pop("src"))
    for a in data["assets"]:
        a["url"] = url(a.pop("src"))
    st = proj["state"]
    data["state"]["docs"] = sorted(st.get("docs") or {})
    data["state"]["snippets"] = st.get("snippets") or {}
    data.pop("dir", None)
    data["passes"].reverse()  # newest first
    data["versions"].reverse()
    return data


def character_card(name: str, paths: Paths) -> dict:
    ch = characters.load(name)
    st = ch["state"]
    d = characters.char_dir(name)
    first = next(iter(st["refs"].values()), None)
    return {"name": name, "title": ch["title"], "version": ch["version"], "stage": stages.character_stage(ch),
            "description": st.get("description", ""), "palette": st.get("palette") or [],
            "ref": paths.url(d / first["file"]) if first else None, "refs": len(st["refs"]),
            "projects": sorted(characters.used_by(name)), "updated": ch["versions"][-1]["at"] if ch["versions"] else None}


def character_detail(name: str, paths: Paths) -> dict:
    ch = characters.load(name)
    d = characters.char_dir(name)

    def refs(st: dict) -> list[dict]:
        return [{"name": n, "role": r.get("role", ""), "url": paths.url(d / r["file"])} for n, r in st["refs"].items()]

    st = ch["state"]
    return {"name": name, "title": ch["title"], "version": ch["version"], "stage": stages.character_stage(ch),
            "description": st.get("description", ""), "palette": st.get("palette") or [], "refs": refs(st),
            "used_by": characters.used_by(name),
            "versions": [{"version": v["version"], "at": v["at"], "author": v["author"], "note": v["note"],
                          "changed": v["changed"], "stage": v["state"].get("stage", "explore"),
                          "description": v["state"].get("description", ""), "refs": refs(v["state"])}
                         for v in reversed(ch["versions"])]}


def overview(index: RunIndex) -> dict:
    runs = index.all()
    walls = [r for r in runs if r["wallpaper"] and any(not im.get("blank") for im in r["images"])]
    return {"projects": len(projects.list_projects()), "characters": len(characters.list_characters()),
            "runs": len(runs), "images": sum(r["count"] for r in runs),
            "wallpaper": walls[0] if walls else None, "recent": runs[:8]}


# ---------------------------------------------------------------- prompts (#99)

def _texts(value) -> list[str]:
    return list(dict.fromkeys(daily._pool(value)))  # no repeats: the page keys rows by their text


def _theme_rows(texts: list[str], used: dict[str, dict]) -> list[dict]:
    return [{"text": t, "uses": used.get(t, {}).get("uses", 0), "last": used.get(t, {}).get("last")} for t in texts]


def theme_usage(index: RunIndex) -> dict[str, dict]:
    """{theme: {uses, last}} over every daily run (its day: daily.date, else the folder's date)."""
    used: dict[str, dict] = {}
    for r in index.all():
        d = r.get("daily") or {}
        if not isinstance(d.get("theme"), str):
            continue
        day = d.get("date") or r["date"]
        e = used.setdefault(d["theme"], {"uses": 0, "last": day})
        e["uses"] += 1
        e["last"] = max(e["last"], day)
    return used


def prompts_overview(index: RunIndex) -> dict:
    """Everything that shapes a prompt, read from config.toml, the characters and the projects, for one page.
    Read-only; a part that fails to load is left out with an `errors` note instead of failing the page."""
    conf = imagegen.load_config()
    d = conf.get("daily") or {}
    used = theme_usage(index)
    errors: list[str] = []
    presets = conf.get("presets", {})
    styles = imagegen.list_styles(conf)

    def builtin(key: str, default: str) -> dict:
        custom = str(d.get(key) or "").strip()
        return {"text": custom or default, "custom": bool(custom)}

    daily_preset = d.get("preset", "wallpaper_cast")
    style = str(d.get("style") or "").strip()  # like daily.settings: blank = the preset's styles
    out = {
        "daily": {"preset": daily_preset, "style": style, "cast": _texts(d.get("cast")),
                  "no_repeat_days": d.get("no_repeat_days", 7), "props_per_day": d.get("props_per_day", 0),
                  "count": d.get("count", 1)},
        "themes": {"pool": _theme_rows(_texts(d.get("themes")), used),
                   "dates": {k: _theme_rows(_texts(v), used) for k, v in (d.get("dates") or {}).items()},
                   "months": {k: _theme_rows(_texts(v), used) for k, v in (d.get("months") or {}).items()},
                   "weekdays": {k: _theme_rows(_texts(v), used) for k, v in (d.get("weekdays") or {}).items()}},
        "props": {k: _texts(v) for k, v in (d.get("props") or {}).items()},
        "styles": [{"name": n, "description": s.get("description", ""), "suffix": s.get("suffix", ""),
                    "negative_extra": s.get("negative_extra", ""), "post": s.get("post", []),
                    "presets": sorted(k for k, p in presets.items() if n in imagegen.split_styles(p.get("styles"))),
                    "daily": n in (imagegen.split_styles(style) if style
                                   else imagegen.split_styles(presets.get(daily_preset, {}).get("styles")))}
                   for n, s in styles.items()],
        "presets": [{"name": n, "description": p.get("description", ""), "model": p.get("model"),
                     "kind": "edit" if "edit_workflow" in p and "workflow" not in p else "image",
                     "styles": imagegen.split_styles(p.get("styles")), "suffix": p.get("suffix", ""),
                     "negative_extra": p.get("negative_extra", ""), "post": p.get("post", []),
                     "size": f"{p['width']}x{p['height']}" if "width" in p and "height" in p else None,
                     "daily": n == daily_preset}
                    for n, p in presets.items()],
        "templates": {"scene_prompt": builtin("scene_prompt", daily.SCENE_PROMPT),
                      "cast_prompt": builtin("cast_prompt", daily.CAST_PROMPT),
                      "ref_prompt": {"text": str(d.get("ref_prompt") or ""), "custom": bool(d.get("ref_prompt"))},
                      "stand_in": str(d.get("stand_in", daily.STAND_IN) or ""),  # "" = nobody in the scene
                      "stand_ins": {k: str(v) for k, v in (d.get("stand_ins") or {}).items()}},
        "stages": [], "characters": [], "projects": [], "errors": errors,
    }
    for st in stages.STAGES:
        try:
            s = stages.settings(st, conf)
            out["stages"].append({"stage": st, "mode": s["mode"], "preset": s["preset"], "count": int(s["count"]),
                                  "instruction": s.get("instruction", ""), "ideas": s.get("ideas") or [],
                                  "needs_text": bool(s.get("needs_text"))})
        except (ValueError, KeyError, TypeError) as e:
            errors.append(f"stage {st}: {e}")
    for name in characters.list_characters():
        try:
            ch = characters.load(name)
            out["characters"].append({"name": name, "title": ch["title"], "stage": stages.character_stage(ch),
                                      "description": ch["state"].get("description", ""),
                                      "projects": sorted(characters.used_by(name))})
        except (OSError, ValueError, KeyError) as e:
            errors.append(f"character {name}: {e}")
    for slug in projects.list_projects():
        try:
            proj = projects.load(slug)
            st = proj["state"]
            out["projects"].append({"slug": slug, "title": proj["title"], "prompt": st.get("prompt", ""),
                                    "negative": st.get("negative"), "preset": st.get("preset"),
                                    "snippets": st.get("snippets") or {}, "cast": list(st.get("cast") or {}),
                                    "docs": sorted(st.get("docs") or {})})
        except (OSError, ValueError, KeyError) as e:
            errors.append(f"project {slug}: {e}")
    return out


# ---------------------------------------------------------------- server

class Handler(boardserve.Handler):
    server_version = "imagegen-app"
    index: RunIndex
    paths: Paths
    app_dir: Path

    def do_POST(self):
        self._err(405, "the viewer is read-only")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "bad Host header")
        u = urlsplit(self.path)
        parts = [unquote(p) for p in u.path.split("/") if p]
        if parts[:1] == ["files"]:
            return self._file(u.path[len("/files"):])
        if parts[:1] == ["api"]:
            try:
                data = self._api(parts[1:], parse_qs(u.query))
            except BadRequest as e:
                return self._err(400, str(e))
            except Exception as e:  # noqa: BLE001 - a broken json on /mnt/c: say so, keep serving
                imagegen.log(f"app serve: {u.path} failed: {type(e).__name__}: {e}")
                return self._err(500, f"{type(e).__name__}: {e}")
            if data is None:
                return self._err(404, "not found")
            return self._json(200, data)
        return self._static(u.path)

    def _api(self, parts: list[str], q: dict):
        if parts == ["overview"]:
            return overview(self.index)
        if parts == ["projects"]:
            return {"projects": sorted((project_card(s, self.paths) for s in projects.list_projects()),
                                       key=lambda p: p["updated"] or "", reverse=True)}
        if len(parts) == 2 and parts[0] == "projects":
            return project_detail(parts[1], self.paths) if self._exists(projects.project_dir, parts[1], "project.json") else None
        if parts == ["characters"]:
            return {"characters": [character_card(n, self.paths) for n in characters.list_characters()]}
        if len(parts) == 2 and parts[0] == "characters":
            return character_detail(parts[1], self.paths) if self._exists(characters.char_dir, parts[1], "character.json") else None
        if parts == ["prompts"]:
            return prompts_overview(self.index)
        if parts == ["runs"]:
            return runs_page(self.index, q)
        if len(parts) == 3 and parts[0] == "runs":
            return run_detail(self.index, f"{parts[1]}/{parts[2]}")
        if parts == ["wallpapers"]:
            walls = [r for r in self.index.all() if r["wallpaper"]]
            return {"runs": walls, "log": daily_log(self.paths)}
        return None

    @staticmethod
    def _exists(folder, name: str, file: str) -> bool:
        try:
            return (folder(name) / file).is_file()
        except ValueError:  # not a valid name
            return False

    def _static(self, path: str) -> None:
        if not (self.app_dir / "index.html").is_file():
            return self._send(503, NOT_BUILT.encode(), "text/plain; charset=utf-8")
        try:
            f = (self.app_dir / unquote(path).lstrip("/")).resolve()
            f.relative_to(self.app_dir)
        except (ValueError, OSError):
            return self._err(404, "not found")
        if not f.is_file():
            # a missing build file is a 404; anything else is a client-side route (names may contain dots)
            if path.startswith("/_app/") or Path(path).suffix.lower() in STATIC_TYPES:
                return self._err(404, "not found")
            f = self.app_dir / "index.html"  # a client-side route
        cache = "public, max-age=31536000, immutable" if "/_app/immutable/" in path else "no-cache"
        self._send(200, f.read_bytes(), STATIC_TYPES.get(f.suffix.lower(), "application/octet-stream"),
                   {"Cache-Control": cache})


NOT_BUILT = """The viewer app is not built yet. On tulu:
  cd ~/imagegen/app && npm ci && npm run build
then reload. (The API works without it: /api/overview)
"""


def make_server(port: int = DEFAULT_PORT, host: str = "127.0.0.1", app_dir: Path | None = None) -> ThreadingHTTPServer:
    """A ready server (not yet serving); port 0 picks a free one (tests)."""
    paths = Paths(Path(imagegen.load_config()["output_dir"]))
    handler = type("AppHandler", (Handler,), {"root": paths.root, "paths": paths, "index": RunIndex(paths),
                                              "app_dir": (app_dir or APP_DIR).resolve(), "jobs": None})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


USAGE = """usage: gen app serve [--port N] [--open]
  the viewer app (read-only: projects, characters, wallpapers, every run) on http://127.0.0.1:PORT
  (default {port}) until Ctrl+C. Install it from the browser's address bar to get its own window.
  Build the app first: cd ~/imagegen/app && npm ci && npm run build"""


def main(argv: list[str]) -> int:
    import argparse
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE.format(port=DEFAULT_PORT))
        return 0 if argv else 2
    if argv[0] != "serve":
        print(f"unknown app command '{argv[0]}'\n" + USAGE.format(port=DEFAULT_PORT), file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="gen app serve")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="open it in the Windows browser")
    a = ap.parse_args(argv[1:])
    try:
        srv = make_server(a.port)
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{a.port} ({e.strerror or e}); try --port {a.port + 1}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{srv.server_port}/"
    print(f"app: {url}  (Ctrl+C stops)", flush=True)
    if not (APP_DIR / "index.html").is_file():
        print("note: the app is not built yet: cd ~/imagegen/app && npm ci && npm run build", file=sys.stderr)
    if a.open:
        imagegen.open_in_windows(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    return 0
