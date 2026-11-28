"""gen tunnel (#71): the imagegen MCP tools over a temporary HTTPS tunnel, for project threads.

Project threads run in the cloud and can't reach tulu (trusted devices + projects isn't supported yet).
`gen tunnel` serves an allow-list of mcp_server.py's tools (TOOLS: render, poll, read results; no shell,
nothing that changes the desktop or project state) over MCP's streamable HTTP on 127.0.0.1, and opens
a Cloudflare quick tunnel (`cloudflared`, no account) to it. Image paths must be image files inside
output_dir, and arguments are range-checked (LIMITS) before a tool runs. It prints the URL and a fresh
random token; Þór pastes both into the thread. The token is checked before a request body is read.
Every request is logged in his terminal; Ctrl-C closes it, and so do `--idle-min` minutes without an
authorised request, or MAX_BAD rejected requests (someone is guessing). Nothing starts it automatically.
The threat model is in docs/tunnel.md.

Thread side (the token comes from Þór's message, never from the repo or memory):
    export IMAGEGEN_TUNNEL_URL=https://....trycloudflare.com/mcp IMAGEGEN_TUNNEL_TOKEN=...
    python tunnel.py tools
    python tunnel.py call doctor
    python tunnel.py call generate '{"prompt": "a fox", "preset": "draft"}'

Remove when project threads can use trusted devices: delete this file, tests/test_tunnel.py, the
`tunnel` line in imagegen.main and the CLAUDE.md note, tunnel-cloudflared.sh and docs/tunnel.md,
after `./tunnel-cloudflared.sh remove` on tulu.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hmac
import inspect
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

DEFAULT_PORT = 8191
IDLE_MIN = 120
MAX_BODY = 64 * 1024  # tool arguments are small; images only ever go out
MAX_BAD = 20  # rejected requests (wrong token, too large) before the tunnel closes itself
RATE_PER_MIN = 120  # authorised requests per minute (a tool call is ~3 requests)

# The tools a thread may call. Read and render only: nothing that touches the Windows desktop
# (set_wallpaper, open_contact_sheet), reads an arbitrary folder (project_import) or changes project or
# character state. Adding one is a deliberate edit here, reviewed like any change.
TOOLS = ("doctor", "list_presets", "list_styles", "recent_runs", "view_image", "generate", "get_job",
         "vary", "edit", "cutout", "project_list", "project_get", "character_list", "character_get")
PATH_ARGS = {"path", "mask"}  # must be image files inside output_dir
LIMITS = {  # argument name -> (min, max); anything outside is refused before the tool runs
    "count": (1, 8), "width": (256, 2048), "height": (256, 2048), "steps": (1, 100), "seed": (0, 2**63 - 1),
    "strength": (0.0, 1.0), "guidance": (0.0, 20.0), "size": (64, 1536), "n": (1, 50), "wait": (0, 45),
    "history": (0, 20)}
MAX_TEXT = 4000  # characters in any one string argument
URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")  # not the API host in error lines
URL_WAIT_S = 60
INSTALL_HINT = """cloudflared is not installed. On tulu (WSL), a pinned, checksum-checked binary in ~/.local/bin:
  ~/imagegen/tunnel-cloudflared.sh install
(remove it again with: ~/imagegen/tunnel-cloudflared.sh remove)"""


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _log(line: str) -> None:
    print(line, flush=True)


def _clean(text: str) -> str:
    """Printable only: a request can't put terminal escape sequences into Þór's log."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def _leaf(e: BaseException) -> BaseException:
    """The first real error inside (nested) exception groups, for a one-line message."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def describe(body: bytes) -> str:
    """One log line for a JSON-RPC body: the tool and its arguments for tools/call, else the method."""
    try:
        msgs = json.loads(body)
    except ValueError:
        return f"unparsed body ({len(body)} bytes)"
    parts = []
    for m in msgs if isinstance(msgs, list) else [msgs]:
        if not isinstance(m, dict):
            continue
        method = str(m.get("method", "response"))
        params = m.get("params") if isinstance(m.get("params"), dict) else {}
        if method == "tools/call":
            args = json.dumps(params.get("arguments") or {}, ensure_ascii=False)
            parts.append(f"{params.get('name')} {args if len(args) <= 160 else args[:157] + '...'}")
        else:
            parts.append(method)
    return _clean(", ".join(parts) or "empty")[:400]


# ---------------------------------------------------------------- the allow-listed tools

def _image_in_output(value: str) -> str:
    import imagegen
    import projects
    root = Path(imagegen.load_config()["output_dir"]).expanduser().resolve()
    try:
        p = imagegen.to_linux_path(value).resolve()
    except Exception:  # noqa: BLE001 - wslpath failing on a bad Windows path
        p = None
    if p is None or not p.is_relative_to(root) or p.suffix.lower() not in projects.IMAGE_EXTS or not p.is_file():
        raise ValueError("must be an existing image file inside the imagegen output folder "
                         "(paths from recent_runs, get_job or project_get)")
    return str(p)


def check_args(arguments: dict) -> dict:
    """Refuse out-of-range numbers, long strings and paths outside output_dir. Returns the arguments
    with paths resolved (symlinks followed), so the tool opens exactly the file that was checked."""
    out = {}
    for name, v in arguments.items():
        if isinstance(v, str):
            if len(v) > MAX_TEXT or "\x00" in v:
                raise ValueError(f"{name}: at most {MAX_TEXT} characters, no NUL")
            if name in PATH_ARGS:
                try:
                    v = _image_in_output(v)
                except ValueError as e:
                    raise ValueError(f"{name}: {e}") from None
        elif isinstance(v, list):
            if len(v) > 20 or not all(isinstance(x, str) and len(x) <= 200 for x in v):
                raise ValueError(f"{name}: at most 20 short strings")
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and name in LIMITS:
            lo, hi = LIMITS[name]
            if not lo <= v <= hi:
                raise ValueError(f"{name}: must be between {lo} and {hi}")
        out[name] = v
    return out


def _guarded(fn):
    """fn with check_args in front; same signature, so the MCP schema is unchanged."""
    from mcp.server.mcpserver.exceptions import ToolError
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        try:
            checked = check_args(bound.arguments)
        except ValueError as e:
            raise ToolError(str(e)) from None  # ToolError text reaches the caller; other errors don't
        return fn(**checked)
    return wrapper


def make_mcp():
    """A separate MCP server holding only TOOLS (wrapped), with mcp_server's instructions."""
    from mcp.server.mcpserver import MCPServer

    import mcp_server
    mcp_server.SEEN_AS = "thread"  # project_get's new_since_last_look stays separate from the desktop app's
    server = MCPServer("imagegen-tunnel", instructions=mcp_server.mcp.instructions)
    for name in TOOLS:
        fn = getattr(mcp_server, name)
        server.add_tool(_guarded(fn), name=name)
    return server


class Guard:
    """ASGI middleware in front of the MCP app: bearer token, request log, idle clock, body size cap."""

    def __init__(self, app, token: str, log=_log, clock=time.monotonic, on_close=None):
        if len(token) < 32:
            raise ValueError("token too short")
        self.app, self.log, self.clock, self.on_close = app, log, clock, on_close
        self._expected = f"Bearer {token}".encode()
        self.last = clock()  # last authorised request (idle timeout)
        self.bad = 0
        self._recent: deque[float] = deque()

    def _reject(self, send, status: int, text: bytes, why: str, counts: bool):
        """counts: the request came through Cloudflare. Local ones (a browser tab on localhost) are refused
        too, but can't close the tunnel."""
        self.bad += counts
        self.log(f"{_now()}  REJECTED {why}" + (f" ({self.bad}/{MAX_BAD})" if counts else " (local)"))
        if counts and self.bad >= MAX_BAD and self.on_close:
            self.log(f"{_now()}  {MAX_BAD} rejected requests: closing the tunnel")
            self.on_close()
        return _plain(send, status, text)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # starts the MCP session manager
            return await self.app(scope, receive, send)
        if scope["type"] != "http":  # websockets: nothing here uses them, so nothing gets past without a token
            return
        headers = dict(scope["headers"])
        remote = b"cf-connecting-ip" in headers  # set by Cloudflare on every tunnelled request
        client = scope.get("client") or ("?", 0)
        who = _clean((headers.get(b"cf-connecting-ip") or str(client[0]).encode()).decode(errors="replace"))[:60]
        if not hmac.compare_digest(headers.get(b"authorization", b""), self._expected):
            return await self._reject(send, 401, b"missing or wrong token\n",
                                      f"{_clean(scope['method'])[:10]} {_clean(scope['path'])[:80]} from {who}: "
                                      "missing or wrong token", remote)
        now = self.clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= RATE_PER_MIN:
            self.log(f"{_now()}  REFUSED over {RATE_PER_MIN} requests/min")
            return await _plain(send, 429, b"too many requests\n")
        self._recent.append(now)
        self.last = now

        body, more = b"", True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > MAX_BODY:
                return await self._reject(send, 413, b"request too large\n", f"body over {MAX_BODY} bytes from {who}", remote)
        what = describe(body) if scope["method"] == "POST" else _clean(f"{scope['method']} {scope['path']}")[:100]
        start, status, logged = time.monotonic(), [0], [False]

        def log_once():
            if not logged[0] and not what.startswith("notifications/"):
                self.log(f"{_now()}  {what}  -> {status[0]} in {time.monotonic() - start:.1f}s")
            logged[0] = True

        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def watch(msg):
            if msg["type"] == "http.response.start":
                status[0] = msg["status"]
            elif msg["type"] == "http.response.body" and not msg.get("more_body"):
                log_once()  # before the last byte goes out, so the log never lags the reply
            await send(msg)

        try:
            await self.app(scope, replay, watch)
        finally:
            log_once()
            self.last = self.clock()


async def _plain(send, status: int, text: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(text)).encode())]})
    await send({"type": "http.response.body", "body": text})


def make_app(token: str, log=_log):
    """The guarded ASGI app: the TOOLS at /mcp (stateless: no sessions to hijack; plain JSON responses)."""
    from mcp.server.transport_security import TransportSecuritySettings
    # The Host header is the tunnel's hostname, so the DNS-rebinding check (Host = 127.0.0.1) can't apply;
    # the bearer token does that job (a rebinding page can't send it).
    app = make_mcp().streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return Guard(app, token, log=log)


def make_server(port: int, token: str, idle_s: float, log=_log, check_every: float = 15.0):
    """A uvicorn server on 127.0.0.1:port (not started) that stops itself after idle_s without requests."""
    import uvicorn
    guard = make_app(token, log)
    server = uvicorn.Server(uvicorn.Config(guard, host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False, timeout_graceful_shutdown=3, server_header=False,
                                           limit_concurrency=8))
    guard.on_close = lambda: setattr(server, "should_exit", True)

    def watchdog():
        while not server.should_exit:
            time.sleep(check_every)
            if guard.clock() - guard.last > idle_s and not server.should_exit:
                log(f"{_now()}  no requests for {idle_s / 60:g} min: closing the tunnel")
                server.should_exit = True

    threading.Thread(target=watchdog, daemon=True).start()
    return server


def start_cloudflared(port: int, exe: str | None = None, wait_s: float = URL_WAIT_S, on_exit=None):
    """Start a quick tunnel to 127.0.0.1:port. Returns (process, public https URL).
    on_exit(last output lines) runs if cloudflared stops after giving the URL."""
    exe = exe or shutil.which("cloudflared") or str(Path.home() / ".local/bin/cloudflared")
    if not Path(exe).is_file():
        raise SystemExit(INSTALL_HINT)
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail: deque[str] = deque(maxlen=15)
    found = threading.Event()
    url: list[str] = []

    def drain():  # keep reading so cloudflared never blocks on a full pipe
        for line in proc.stdout:
            tail.append(line.rstrip())
            if not found.is_set() and (m := URL_RE.search(line)):
                url.append(m.group(0))
                found.set()
        if found.is_set() and on_exit:
            on_exit(list(tail))

    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + wait_s
    while not found.wait(0.2):
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            raise SystemExit("cloudflared did not give a tunnel URL:\n  " + "\n  ".join(tail))
    return proc, url[0]


def serve(port: int = DEFAULT_PORT, idle_min: float = IDLE_MIN, local: bool = False,
          cloudflared: str | None = None) -> int:
    import logging
    logging.getLogger("mcp").setLevel(logging.WARNING)  # "Terminating session" per stateless request
    import socket
    token = secrets.token_urlsafe(32)
    proc = None
    sock = socket.socket()
    # After Ctrl-C the connections the server closed sit in TIME_WAIT for about a minute, and without
    # SO_REUSEADDR a bind to that port fails ("Address already in use") even though nothing listens.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))  # before cloudflared points at the port: fail here if it's taken
    except OSError as e:
        sock.close()
        raise SystemExit(f"port {port} is in use ({e}): is a gen tunnel already open? Or use --port.")
    try:
        server = make_server(port, token, idle_min * 60)
        if local:
            url = f"http://127.0.0.1:{port}/mcp"
        else:
            def tunnel_gone(tail):
                _log(f"{_now()}  cloudflared stopped, so the tunnel is down: closing.\n  " + "\n  ".join(tail[-5:]))
                server.should_exit = True
            proc, base = start_cloudflared(port, cloudflared, on_exit=tunnel_gone)
            url = f"{base}/mcp"
        print(f"\nimagegen tunnel is open: the imagegen MCP tools only, no shell.\n"
              f"Paste this line into the project thread:\n\n"
              f"  imagegen tunnel: IMAGEGEN_TUNNEL_URL={url} IMAGEGEN_TUNNEL_TOKEN={token}\n\n"
              f"Every request is logged below. Ctrl-C closes it; so do {idle_min:g} min without requests.\n"
              f"The token is new each time, so an old one stops working when this closes.\n", flush=True)
        server.run(sockets=[sock])
    finally:
        sock.close()
        _warn_running_jobs()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print(f"{_now()}  tunnel closed", flush=True)
    return 0


def _warn_running_jobs() -> None:
    """Renders started over the tunnel run in this process; closing it drops them, but ComfyUI keeps
    whatever was already queued. Say so, with the command to clear it (it would clear Þór's own too)."""
    import mcp_server
    running = [j for j, job in mcp_server.JOBS.items() if job.get("status") == "running"]
    if running:
        import imagegen
        url = imagegen.load_config()["comfy_url"].rstrip("/")
        _log(f"{_now()}  {len(running)} render(s) from the thread were still running; ComfyUI may still have "
             f"their jobs queued. To clear the queue (all of it):\n"
             f"  curl --request POST --data '{{\"clear\": true}}' {url}/queue && curl --request POST {url}/interrupt")


# ---------------------------------------------------------------- thread side

def _client_target():
    url, token = os.environ.get("IMAGEGEN_TUNNEL_URL"), os.environ.get("IMAGEGEN_TUNNEL_TOKEN")
    if not url or not token:
        raise SystemExit("set IMAGEGEN_TUNNEL_URL and IMAGEGEN_TUNNEL_TOKEN (from Þór's message); "
                         "without them, hand Þór copy-paste commands instead")
    return url, token


async def _with_client(url: str, token: str, fn):
    from mcp.client.client import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(url, http_client=http), mode="legacy") as c:
        return await fn(c)


def call(tool: str, args: dict, url: str, token: str, save_dir: Path) -> tuple[bool, list[str]]:
    """Call one tool. Returns (ok, printable lines); images are saved under save_dir and listed by path."""
    res = asyncio.run(_with_client(url, token, lambda c: c.call_tool(tool, args)))
    lines = []
    for i, item in enumerate(res.content):
        if item.type == "text":
            lines.append(item.text)
        elif item.type == "image":
            save_dir.mkdir(parents=True, exist_ok=True)
            ext = item.mime_type.split("/")[-1].replace("jpeg", "jpg")
            p = save_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{tool}-{secrets.token_hex(3)}-{i}.{ext}"
            p.write_bytes(base64.b64decode(item.data))
            lines.append(f"[image saved: {p}]")
        else:
            lines.append(f"[{item.type} content]")
    return not res.is_error, lines


def list_tools(url: str, token: str) -> list[tuple[str, str]]:
    res = asyncio.run(_with_client(url, token, lambda c: c.list_tools()))
    return [(t.name, ((t.description or "").strip().splitlines() or [""])[0]) for t in res.tools]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="gen tunnel", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"local port (default {DEFAULT_PORT})")
    ap.add_argument("--idle-min", type=float, default=IDLE_MIN,
                    help=f"close after this many minutes without requests (default {IDLE_MIN})")
    ap.add_argument("--local", action="store_true", help="no cloudflared: serve on 127.0.0.1 only")
    sub = ap.add_subparsers(dest="cmd", title="thread side (no subcommand = open the tunnel, on tulu)")
    sub.add_parser("tools", help="list the tools behind the tunnel")
    c = sub.add_parser("call", help="call one tool")
    c.add_argument("tool")
    c.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")
    c.add_argument("--save", default=str(Path(tempfile.gettempdir()) / "imagegen-tunnel"),
                   help="where images from the result go")
    a = ap.parse_args(argv)
    try:
        return _run(a)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as e:  # noqa: BLE001 - the client wraps HTTP errors in exception groups
        if a.cmd not in ("tools", "call"):
            raise
        leaf = _leaf(e)
        raise SystemExit(f"tunnel call failed ({type(leaf).__name__}: {leaf}). Is the tunnel still open, and are "
                         "the URL and token from Þór's latest message? A 401 means a wrong or old token.")


def _run(a) -> int:
    if a.cmd == "tools":
        for name, desc in list_tools(*_client_target()):
            print(f"{name:20} {desc}")
        return 0
    if a.cmd == "call":
        try:
            args = json.loads(a.args)
        except ValueError as e:
            raise SystemExit(f"args must be a JSON object: {e}")
        if not isinstance(args, dict):
            raise SystemExit("args must be a JSON object")
        ok, lines = call(a.tool, args, *_client_target(), Path(a.save))
        print("\n".join(lines))
        return 0 if ok else 1
    return serve(a.port, a.idle_min, a.local)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
