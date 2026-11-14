"""gen board serve (#55): the board page (boards.py) on 127.0.0.1, with buttons that work.

The static <project>/index.html can only copy `gen project` commands. Served, the same template
(templates/board.html) gets "live": true in its data and then:
  - rates and notes with a click or a key (1/2/3, 0 clears, n notes) through POST /api/...,
  - runs "generate 4 more" (projects.run_pass, one job at a time: there is one GPU),
  - refreshes itself when project.json or board.json change (a rating from the CLI or Claude,
    a pass that finished), by polling GET /api/projects/<slug>/stamp.

Stdlib only (http.server). Binds 127.0.0.1; WSL2 forwards it to the Windows browser.
Requests whose Host is not 127.0.0.1/localhost are refused (DNS rebinding), and every POST must be
JSON from the page's own origin (a cross-site form or fetch cannot send that without a preflight
this server never answers), so other web pages cannot rate or start jobs.

Files: only image and .html files under output_dir (pass images, refs, assets, contact sheets).
"""
from __future__ import annotations

import html
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import boards
import imagegen
import projects

DEFAULT_PORT = 8190
FILE_EXTS = projects.IMAGE_EXTS | {".html"}
MAX_BODY = 64 * 1024
GEN_MAX = 16


class Jobs:
    """The one "generate more" job at a time (the GPU runs one anyway), and the last one's outcome."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current: dict | None = None
        self._seq = 0

    def get(self) -> dict | None:
        with self._lock:
            return dict(self.current) if self.current else None

    def start(self, slug: str, count: int, run=None) -> dict:
        """run(slug, count) does the work (default projects.run_pass); raises RuntimeError while one runs."""
        run = run or (lambda s, n: projects.run_pass(s, n, author="human"))
        with self._lock:
            if self.current and self.current["state"] == "running":
                c = self.current
                raise RuntimeError(f"already generating {c['count']} for {c['slug']}; wait for it to finish")
            self._seq += 1
            job = {"id": self._seq, "slug": slug, "count": count, "state": "running", "started": time.time()}
            self.current = job

        def work():
            try:
                r = run(slug, count)
                upd = {"state": "done", "run_dir": r.get("run_dir") if isinstance(r, dict) else None}
            except Exception as e:  # noqa: BLE001 - shown on the page; the server keeps going
                upd = {"state": "failed", "error": f"{type(e).__name__}: {e}"}
                imagegen.log(f"board serve: generate for {slug} failed: {upd['error']}")
            with self._lock:
                job.update(upd, finished=time.time())

        threading.Thread(target=work, name=f"board-gen-{slug}", daemon=True).start()
        return dict(job)


def stamp(slug: str) -> str:
    """Changes whenever project.json or board.json does: the page re-fetches its data when it moves."""
    parts = []
    for f in (projects.project_dir(slug) / "project.json", boards.board_file(slug)):
        try:
            parts.append(str(f.stat().st_mtime_ns))
        except OSError:
            parts.append("-")
    return ".".join(parts)


def _index_page(port: int) -> str:
    rows = []
    for slug in projects.list_projects():
        try:
            p = projects.load(slug)
            title, v = p["title"], p["version"]
        except (OSError, ValueError, KeyError):
            title, v = slug, "?"
        rows.append(f'<li><a href="/projects/{quote(slug)}/">{html.escape(title)}</a> '
                    f'<span>{html.escape(slug)} · v{v}</span></li>')
    body = "<ul>" + "".join(rows) + "</ul>" if rows else "<p>No projects yet: gen project new SLUG --prompt '...'</p>"
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>imagegen boards</title><style>body{margin:0;padding:16px;background:#0b0b0c;color:#ddd;"
            "font:14px/1.5 system-ui,sans-serif}a{color:#7aa2ff}span{color:#8a8a90;margin-left:6px}</style>"
            f"<h1 style='font-size:18px'>Boards</h1>{body}")


class Handler(BaseHTTPRequestHandler):
    server_version = "imagegen-board"
    jobs: Jobs  # set by make_server
    root: Path

    def log_message(self, fmt, *args):  # quiet: one line per image request is noise
        pass

    # ------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host')}"

    def _slug(self, slug: str) -> str | None:
        slug = unquote(slug)
        return slug if slug in projects.list_projects() else None

    def _state(self, slug: str, data: bool = False) -> dict:
        out = {"stamp": stamp(slug), "job": self.jobs.get()}
        if data:
            out["data"] = boards.page_data(projects.load(slug), boards.load(slug))
            out["data"]["live"] = True
        return out

    # ------------------------------------------------------------ GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "bad Host header")
        path = urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]
        if path == "/favicon.ico":  # browsers ask for it; a 404 would be console noise
            return self._send(204, b"", "image/x-icon")
        if not parts:
            return self._send(200, _index_page(self.server.server_port).encode(), "text/html; charset=utf-8")
        if parts[0] == "api" and len(parts) == 4 and parts[1] == "projects":
            slug = self._slug(parts[2])
            if not slug:
                return self._err(404, f"no project '{unquote(parts[2])}'")
            if parts[3] == "stamp":
                return self._json(200, self._state(slug))
            if parts[3] == "board":
                return self._json(200, self._state(slug, data=True))
            return self._err(404, "unknown endpoint")
        if parts[0] == "projects" and len(parts) == 2 and not path.endswith("/"):
            return self._send(301, b"", "text/plain", {"Location": path + "/"})
        if parts[0] == "projects" and (len(parts) == 2 or (len(parts) == 3 and parts[2] == "index.html")):
            slug = self._slug(parts[1])
            if not slug:
                return self._send(404, f"no project '{html.escape(unquote(parts[1]))}'".encode(), "text/plain; charset=utf-8")
            page = boards.render_page(slug, live=True, extra={"stamp": stamp(slug)})
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        return self._file(path)

    def _file(self, path: str) -> None:
        try:
            f = (self.root / unquote(path).lstrip("/")).resolve()
            f.relative_to(self.root)
        except (ValueError, OSError):
            return self._err(404, "not found")
        if f.suffix.lower() not in FILE_EXTS or not f.is_file():
            return self._err(404, "not found")
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                 ".gif": "image/gif", ".bmp": "image/bmp", ".html": "text/html; charset=utf-8"
                 }.get(f.suffix.lower(), "application/octet-stream")
        try:
            body = f.read_bytes()
        except OSError:
            return self._err(404, "not found")
        self._send(200, body, ctype)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "bad Host header")
        if not self._same_origin():
            return self._err(403, "cross-origin request refused")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._err(415, "send JSON (Content-Type: application/json)")
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if not (len(parts) == 4 and parts[:2] == ["api", "projects"]):
            return self._err(404, "unknown endpoint")
        slug = self._slug(parts[2])
        if not slug:
            return self._err(404, f"no project '{unquote(parts[2])}'")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            return self._err(413, "body too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
        except ValueError as e:
            return self._err(400, f"bad JSON: {e}")
        action = parts[3]
        try:
            if action == "rate":
                images = body.get("images")
                if not isinstance(images, list) or not all(isinstance(i, str) for i in images):
                    raise ValueError("images: a list of P:N ids or paths")
                result = boards.rate(slug, images, str(body.get("rating", "")), body.get("note"), "human")
            elif action == "note":
                image = body.get("image")
                result = boards.note(slug, str(body.get("text") or ""), str(image) if image else None, "human")
            elif action == "gen":
                count = body.get("count", 4)
                if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= GEN_MAX:
                    raise ValueError(f"count: a whole number from 1 to {GEN_MAX}")
                st = projects.load(slug)["state"]
                if not projects.full_prompt(st) and not st.get("cast"):
                    raise ValueError(f"project '{slug}' has no prompt yet: gen project set {slug} --prompt '...'")
                try:
                    result = self.jobs.start(slug, count)
                except RuntimeError as e:
                    return self._err(409, str(e))
                return self._json(202, {"result": result, **self._state(slug)})
            else:
                return self._err(404, "unknown endpoint")
        except (ValueError, FileNotFoundError, KeyError) as e:
            return self._err(400, str(e) if not isinstance(e, FileNotFoundError) else f"no such image: {e}")
        return self._json(200, {"result": result, **self._state(slug, data=True)})


def make_server(port: int = DEFAULT_PORT, host: str = "127.0.0.1", jobs: Jobs | None = None) -> ThreadingHTTPServer:
    """A ready server (not yet serving); port 0 picks a free one (tests)."""
    root = Path(imagegen.load_config()["output_dir"]).expanduser().resolve()
    handler = type("BoardHandler", (Handler,), {"jobs": jobs or Jobs(), "root": root})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def open_url(url: str) -> None:
    """The Windows default browser (explorer.exe opens URLs too); nothing outside WSL."""
    if shutil.which("explorer.exe"):
        subprocess.run(["explorer.exe", url])  # explorer returns 1 even on success


USAGE = """usage: gen board serve [SLUG] [--port N] [--open]
  serve the project boards on http://127.0.0.1:PORT (default {port}) until Ctrl+C:
  rate with clicks or keys (1 pick, 2 maybe, 3 reject, 0 clear, n note, j/k move),
  "generate 4 more", and the page refreshes itself when the project or board changes.
  SLUG: the board to open with --open (default: the list of projects)."""


def main(argv: list[str]) -> int:
    import argparse
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE.format(port=DEFAULT_PORT))
        return 0 if argv else 2
    if argv[0] != "serve":
        print(f"unknown board command '{argv[0]}'\n" + USAGE.format(port=DEFAULT_PORT), file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="gen board serve")
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="open it in the Windows browser")
    a = ap.parse_args(argv[1:])
    if a.slug and a.slug not in projects.list_projects():
        print(f"no project '{a.slug}' (gen project list)", file=sys.stderr)
        return 1
    try:
        srv = make_server(a.port)
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{a.port} ({e.strerror or e}); try --port {a.port + 1}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{srv.server_port}/" + (f"projects/{quote(a.slug)}/" if a.slug else "")
    print(f"board: {url}  (Ctrl+C stops)", flush=True)
    if a.open:
        open_url(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    return 0
