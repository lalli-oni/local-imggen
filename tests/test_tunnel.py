"""tunnel.py (#71): the MCP tools behind a bearer token over HTTP, the request log, idle close, cloudflared."""
import asyncio
import http.client
import json
import os
import socket
import stat
import threading
import time

import pytest

import imagegen
import mcp_server
import tunnel

TOKEN = "test-token-0123456789-0123456789-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fresh_jobs(monkeypatch):
    monkeypatch.setattr(mcp_server, "JOBS", {})


@pytest.fixture
def server():
    """A running tunnel server (no cloudflared): yields (url, log lines)."""
    log: list[str] = []
    port = free_port()
    srv = tunnel.make_server(port, TOKEN, idle_s=3600, log=log.append, check_every=0.1)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}/mcp", log
    srv.should_exit = True
    t.join(10)


def post(url, body: bytes, auth: str | None = None, via_cloudflare=False):
    host, port = url.split("//")[1].split("/")[0].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if auth is not None:
        headers["Authorization"] = auth
    if via_cloudflare:
        headers["CF-Connecting-IP"] = "203.0.113.7"
    c.request("POST", "/mcp", body=body, headers=headers)
    r = c.getresponse()
    return r.status, r.read()


INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}).encode()


# ---------------------------------------------------------------- the log line

def test_describe_tool_call_and_methods():
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "generate", "arguments": {"prompt": "a fox"}}}).encode()
    assert tunnel.describe(body) == 'generate {"prompt": "a fox"}'
    assert tunnel.describe(INIT) == "initialize"
    batch = json.dumps([{"method": "tools/list"}, {"method": "ping"}]).encode()
    assert tunnel.describe(batch) == "tools/list, ping"
    assert tunnel.describe(b"not json").startswith("unparsed body")


def test_describe_truncates_long_arguments():
    body = json.dumps({"method": "tools/call", "params": {"name": "generate", "arguments": {"prompt": "x" * 500}}})
    line = tunnel.describe(body.encode())
    assert line.endswith("...") and len(line) < 200


# ---------------------------------------------------------------- auth

@pytest.mark.parametrize("auth", [None, "Bearer wrong", f"Basic {TOKEN}", TOKEN, f"Bearer {TOKEN}x"])
def test_requests_without_the_token_are_refused_and_logged(server, auth):
    url, log = server
    status, body = post(url, INIT, auth)
    assert status == 401 and b"token" in body
    assert any("REJECTED" in line for line in log)
    assert not any("initialize" in line for line in log)  # never reached the MCP app


def test_the_token_lets_requests_through(server):
    url, log = server
    status, body = post(url, INIT, f"Bearer {TOKEN}")
    assert status == 200 and b'"serverInfo"' in body
    assert any("initialize" in line and "-> 200" in line for line in log)


def test_oversized_body_is_refused(server):
    url, log = server
    status, _ = post(url, b"{" + b" " * (tunnel.MAX_BODY + 10) + b"}", f"Bearer {TOKEN}")
    assert status == 413 and any("over" in line for line in log)


def test_tunnel_accepts_any_host_header(server):
    """The Host is the tunnel's hostname (xyz.trycloudflare.com); the token replaces the rebinding check."""
    url, _ = server
    port = int(url.split(":")[2].split("/")[0])
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/mcp", body=INIT, headers={
        "Host": "abc-def.trycloudflare.com", "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    assert c.getresponse().status == 200


# ---------------------------------------------------------------- the thread-side client, end to end

def test_only_the_allow_listed_tools_are_served(server):
    url, _ = server
    names = {n for n, _ in tunnel.list_tools(url, TOKEN)}
    assert names == set(tunnel.TOOLS)
    for risky in ("set_wallpaper", "open_contact_sheet", "project_import", "project_update", "character_new"):
        assert risky not in names


def test_allow_listed_tools_keep_their_schema():
    full = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    for t in asyncio.run(tunnel.make_mcp().list_tools()):
        assert t.input_schema == full[t.name].input_schema and t.description == full[t.name].description


def test_a_tool_off_the_list_cannot_be_called(server, tmp_path):
    url, _ = server
    ok, lines = tunnel.call("set_wallpaper", {"path": "/etc/passwd"}, url, TOKEN, tmp_path)
    assert not ok and "set_wallpaper" in "\n".join(lines)


def test_generate_through_the_tunnel_saves_thumbnails(server, comfy, tmp_path):
    url, log = server
    ok, lines = tunnel.call("generate", {"prompt": "a fox", "count": 2, "seed": 5}, url, TOKEN, tmp_path / "imgs")
    assert ok
    summary = json.loads(lines[0])
    assert summary["status"] == "done" and summary["base_seed"] == 5 and len(summary["images"]) == 2
    saved = sorted((tmp_path / "imgs").glob("*-generate-*.jpg"))
    assert len(saved) == 2 and saved[0].read_bytes()[:2] == b"\xff\xd8"
    assert any(f"[image saved: {saved[0]}]" == line for line in lines)
    assert any(line.split("  ", 1)[1].startswith('generate {"prompt": "a fox"') for line in log)
    assert len(comfy.workflows()) == 2


def test_tool_error_comes_back_as_not_ok(server, tmp_path):
    url, _ = server
    ok, lines = tunnel.call("get_job", {"job_id": "nope"}, url, TOKEN, tmp_path)
    assert not ok and "get_job" in "\n".join(lines)  # the MCP layer hides the exception text


def test_cli_call_and_tools(server, monkeypatch, capsys, tmp_path):
    url, _ = server
    monkeypatch.setenv("IMAGEGEN_TUNNEL_URL", url)
    monkeypatch.setenv("IMAGEGEN_TUNNEL_TOKEN", TOKEN)
    assert imagegen.main(["tunnel", "tools"]) == 0
    assert "generate" in capsys.readouterr().out
    assert imagegen.main(["tunnel", "call", "list_styles", "--save", str(tmp_path)]) == 0
    assert "synthwave" in capsys.readouterr().out


def test_cli_wrong_token_says_so(server, monkeypatch):
    url, _ = server
    monkeypatch.setenv("IMAGEGEN_TUNNEL_URL", url)
    monkeypatch.setenv("IMAGEGEN_TUNNEL_TOKEN", "old-token-0123456789-0123456789-0123456789")
    with pytest.raises(SystemExit, match="wrong or old token"):
        tunnel.main(["tools"])


def test_cli_without_url_or_token(monkeypatch):
    monkeypatch.delenv("IMAGEGEN_TUNNEL_URL", raising=False)
    monkeypatch.delenv("IMAGEGEN_TUNNEL_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="IMAGEGEN_TUNNEL_URL"):
        tunnel.main(["call", "doctor"])


@pytest.mark.parametrize("args", ["{not json", "[1, 2]"])
def test_cli_call_needs_a_json_object(monkeypatch, args):
    monkeypatch.setenv("IMAGEGEN_TUNNEL_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setenv("IMAGEGEN_TUNNEL_TOKEN", TOKEN)
    with pytest.raises(SystemExit, match="JSON object"):
        tunnel.main(["call", "generate", args])


# ---------------------------------------------------------------- argument checks

def test_view_image_inside_output_dir(server, make_run, tmp_path):
    url, _ = server
    img = make_run()["images"][0]["path"]
    ok, lines = tunnel.call("view_image", {"path": str(img), "size": 64}, url, TOKEN, tmp_path / "v")
    assert ok and lines[0].startswith("[image saved:")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../etc/hosts", "{out}/../secret.png", "{out}/link.png",
                                 "{out}/notes.txt", "{out}/missing.png"])
def test_paths_outside_output_dir_are_refused(server, out_dir, make_image, tmp_path, bad):
    url, _ = server
    out_dir.mkdir(parents=True, exist_ok=True)
    outside = make_image("secret.png")
    (out_dir.parent / "secret.png").write_bytes(outside.read_bytes())
    (out_dir / "link.png").symlink_to(outside)  # a symlink inside pointing out
    (out_dir / "notes.txt").write_text("x")
    ok, lines = tunnel.call("view_image", {"path": bad.format(out=out_dir)}, url, TOKEN, tmp_path / "v")
    assert not ok and "inside the imagegen output folder" in "\n".join(lines)
    assert not (tmp_path / "v").exists()


def test_vary_mask_is_checked_too(server, make_run, tmp_path):
    url, _ = server
    img = make_run()["images"][0]["path"]
    ok, lines = tunnel.call("vary", {"path": str(img), "mask": "/etc/passwd"}, url, TOKEN, tmp_path)
    assert not ok and "mask:" in "\n".join(lines)


@pytest.mark.parametrize("args, msg", [
    ({"prompt": "a fox", "count": 9}, "count"),
    ({"prompt": "a fox", "width": 100000}, "width"),
    ({"prompt": "a fox", "steps": 0}, "steps"),
    ({"prompt": "x" * (tunnel.MAX_TEXT + 1)}, "characters"),
    ({"prompt": "a fox", "styles": ["s"] * 21}, "short strings"),
])
def test_out_of_range_arguments_are_refused_before_rendering(server, comfy, tmp_path, args, msg):
    url, _ = server
    ok, lines = tunnel.call("generate", args, url, TOKEN, tmp_path)
    assert not ok and msg in "\n".join(lines)
    assert comfy.workflows() == []


def test_check_args_passes_normal_values():
    assert tunnel.check_args({"prompt": "a fox", "count": 8, "seed": 0, "preview": True, "styles": []}) == \
        {"prompt": "a fox", "count": 8, "seed": 0, "preview": True, "styles": []}


def test_check_args_limits_recent_runs_paging():
    assert tunnel.check_args({"match": "arcade", "offset": 40, "n": 50})["offset"] == 40
    with pytest.raises(ValueError, match="offset"):
        tunnel.check_args({"offset": 10001})
    with pytest.raises(ValueError, match="offset"):
        tunnel.check_args({"offset": -1})


# ---------------------------------------------------------------- guessing, flooding

def test_closes_after_too_many_rejected_requests(monkeypatch):
    monkeypatch.setattr(tunnel, "MAX_BAD", 3)
    log: list[str] = []
    port = free_port()
    srv = tunnel.make_server(port, TOKEN, idle_s=3600, log=log.append, check_every=0.05)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    for _ in range(3):
        assert post(f"http://127.0.0.1:{port}/mcp", INIT, "Bearer guess", via_cloudflare=True)[0] == 401
    t.join(10)
    assert not t.is_alive()
    assert any("closing the tunnel" in line for line in log)


def test_local_bad_requests_do_not_close_it(server, monkeypatch):
    url, log = server
    monkeypatch.setattr(tunnel, "MAX_BAD", 2)
    for _ in range(4):
        assert post(url, INIT, "Bearer guess")[0] == 401
    assert post(url, INIT, f"Bearer {TOKEN}")[0] == 200
    assert all("(local)" in line for line in log if "REJECTED" in line)


def test_websocket_scope_never_reaches_the_app():
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    guard = tunnel.Guard(app, TOKEN, log=lambda line: None)
    asyncio.run(guard({"type": "websocket", "headers": [], "path": "/mcp"}, None, None))
    asyncio.run(guard({"type": "lifespan"}, None, None))
    assert reached == ["lifespan"]


def test_project_get_marks_the_thread_as_reader_not_the_desktop_app(server, out_dir):
    import projects
    url, _ = server
    projects.new("fjord", "Fjord", "moody fjord", prompt="a fjord")
    try:
        ok, _ = tunnel.call("project_get", {"slug": "fjord"}, url, TOKEN, out_dir)
        assert ok
        seen = json.loads((projects.project_dir("fjord") / "seen.json").read_text())
        assert "thread" in seen and "claude" not in seen
    finally:
        mcp_server.SEEN_AS = "claude"


def test_rate_limit(server, monkeypatch):
    url, log = server
    monkeypatch.setattr(tunnel, "RATE_PER_MIN", 2)
    assert post(url, INIT, f"Bearer {TOKEN}")[0] == 200
    assert post(url, INIT, f"Bearer {TOKEN}")[0] == 200
    assert post(url, INIT, f"Bearer {TOKEN}")[0] == 429


def test_log_lines_are_printable():
    body = json.dumps({"method": "tools/call", "params": {"name": "gen\x1b[2Jerate", "arguments": {}}})
    assert "\x1b" not in tunnel.describe(body.encode())


def test_short_tokens_are_refused():
    with pytest.raises(ValueError):
        tunnel.Guard(None, "short")


# ---------------------------------------------------------------- idle close

def test_closes_itself_when_idle():
    log: list[str] = []
    srv = tunnel.make_server(free_port(), TOKEN, idle_s=0.3, log=log.append, check_every=0.05)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert any("no requests" in line for line in log)


# ---------------------------------------------------------------- cloudflared

def fake_cloudflared(tmp_path, body: str):
    p = tmp_path / "cloudflared"
    p.write_text("#!/usr/bin/env bash\n" f'echo "$@" > "{tmp_path}/cf-args"\n' + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_start_cloudflared_reads_the_quick_tunnel_url(tmp_path):
    exe = fake_cloudflared(tmp_path, 'echo "INF Requesting new quick Tunnel" >&2\n'
                                     'echo "INF |  https://brave-fox-12.trycloudflare.com   |" >&2\n'
                                     "exec sleep 30\n")
    proc, url = tunnel.start_cloudflared(8191, exe, wait_s=10)
    try:
        assert url == "https://brave-fox-12.trycloudflare.com"
        assert (tmp_path / "cf-args").read_text().split() == [
            "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8191"]
    finally:
        proc.kill()
        proc.wait()


def test_cloudflared_api_host_in_an_error_is_not_the_tunnel(tmp_path):
    exe = fake_cloudflared(tmp_path, 'echo "ERR failed to request quick Tunnel: Post https://api.trycloudflare.com/tunnel: dial tcp" >&2\nexit 1\n')
    with pytest.raises(SystemExit, match="did not give a tunnel URL"):
        tunnel.start_cloudflared(8191, exe, wait_s=10)


def test_cloudflared_exiting_later_is_reported(tmp_path):
    exe = fake_cloudflared(tmp_path, 'echo "https://brave-fox-12.trycloudflare.com" >&2\nsleep 0.3\necho "ERR connection lost" >&2\n')
    gone = threading.Event()
    seen = []
    proc, _ = tunnel.start_cloudflared(8191, exe, wait_s=10, on_exit=lambda tail: seen.extend(tail) or gone.set())
    assert gone.wait(10) and "ERR connection lost" in seen
    proc.wait()


def test_port_in_use_stops_before_cloudflared(tmp_path):
    exe = fake_cloudflared(tmp_path, "exit 0\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        with pytest.raises(SystemExit, match="in use"):
            tunnel.serve(port=s.getsockname()[1], cloudflared=exe)
    assert not (tmp_path / "cf-args").exists()


def test_closing_with_renders_running_says_how_to_clear_the_queue(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "JOBS", {"ab12": {"status": "running"}, "cd34": {"status": "done"}})
    tunnel._warn_running_jobs()
    out = capsys.readouterr().out
    assert "1 render(s)" in out and '{"clear": true}' in out and "/interrupt" in out


def test_start_cloudflared_failure_shows_its_output(tmp_path):
    exe = fake_cloudflared(tmp_path, 'echo "ERR failed to request quick Tunnel: 429" >&2\nexit 1\n')
    with pytest.raises(SystemExit, match="429"):
        tunnel.start_cloudflared(8191, exe, wait_s=10)


def test_missing_cloudflared_prints_the_install_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # HOME is a temp dir too, so no ~/.local/bin/cloudflared
    with pytest.raises(SystemExit, match="tunnel-cloudflared.sh install"):
        tunnel.start_cloudflared(8191)


def test_serve_prints_url_and_token_and_stops_cloudflared(tmp_path, monkeypatch, capsys):
    exe = fake_cloudflared(tmp_path, f'echo $$ > "{tmp_path}/cf-pid"\n'
                                     'echo "https://brave-fox-12.trycloudflare.com" >&2\nexec sleep 30\n')
    seen = {}

    class Srv:
        def run(self, sockets=None):  # Ctrl-C while serving
            raise KeyboardInterrupt

    def fake_make_server(port, token, idle_s, **_):
        seen.update(port=port, token=token, idle_s=idle_s)
        return Srv()

    monkeypatch.setattr(tunnel, "make_server", fake_make_server)
    with pytest.raises(KeyboardInterrupt):
        tunnel.serve(port=8199, idle_min=5, cloudflared=exe)
    out = capsys.readouterr().out
    assert "IMAGEGEN_TUNNEL_URL=https://brave-fox-12.trycloudflare.com/mcp" in out
    assert f"IMAGEGEN_TUNNEL_TOKEN={seen['token']}" in out and len(seen["token"]) >= 40
    assert seen["port"] == 8199 and seen["idle_s"] == 300
    pid = int((tmp_path / "cf-pid").read_text())
    for _ in range(50):  # terminated by serve's finally
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("cloudflared still running")
    assert "tunnel closed" in out


def test_each_serve_gets_a_new_token(monkeypatch):
    tokens = []
    monkeypatch.setattr(tunnel, "make_server", lambda port, token, idle_s, **_: tokens.append(token) or
                        type("S", (), {"run": lambda self, sockets=None: None})())
    tunnel.serve(local=True)
    tunnel.serve(local=True)
    assert len(set(tokens)) == 2
