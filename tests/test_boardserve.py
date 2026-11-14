"""boardserve.py (#55): `gen board serve`, the board on 127.0.0.1 with an API behind its buttons."""
import http.client
import json
import socket
import threading
import time

import pytest

import boards
import boardserve
import imagegen
import projects


@pytest.fixture
def proj(out_dir, comfy):
    """Project "fjord": pass 1 (2 images, v1) and pass 2 (2 images, v2)."""
    projects.new("fjord", "Fjord", "moody fjord", prompt="a fjord")
    projects.run_pass("fjord", count=2, seed=1)
    projects.set_state("fjord", "less fog", "claude", prompt="a fjord, light mist")
    projects.run_pass("fjord", count=2, seed=10)
    return "fjord"


@pytest.fixture
def server(out_dir):
    """A running board server on a free port: yields (port, jobs)."""
    jobs = boardserve.Jobs()
    srv = boardserve.make_server(0, jobs=jobs)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_port, jobs
    srv.shutdown()
    srv.server_close()


def req(port, method, path, body=None, headers=None, raw=None):
    """(status, headers, parsed JSON or bytes)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    if r.getheader("Content-Type", "").startswith("application/json"):
        payload = json.loads(payload)
    return r.status, dict(r.getheaders()), payload


def wait_job(jobs, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = jobs.get()
        if j and j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_pages(proj, server):
    port, _ = server
    st, _, body = req(port, "GET", "/")
    assert st == 200 and b'href="/projects/fjord/"' in body
    st, h, _ = req(port, "GET", "/projects/fjord")
    assert st == 301 and h["Location"] == "/projects/fjord/"
    st, h, body = req(port, "GET", "/projects/fjord/")
    assert st == 200 and h["Content-Type"].startswith("text/html")
    assert b'"live": true' in body and b'"stamp": "' in body
    assert req(port, "GET", "/projects/nope/")[0] == 404
    # the static page on disk stays static
    assert '"live": true' not in boards.page_file("fjord").read_text(encoding="utf-8")


def test_serves_images_and_nothing_else(proj, server, out_dir):
    port, _ = server
    st, _, data = req(port, "GET", "/api/projects/fjord/board")
    src = data["data"]["passes"][0]["images"][0]["src"]  # relative to /projects/fjord/
    url = "/" + (out_dir.resolve() / "projects" / "fjord" / src).resolve().relative_to(out_dir.resolve()).as_posix()
    st, h, body = req(port, "GET", url)
    assert st == 200 and h["Content-Type"] == "image/png" and body[:4] == b"\x89PNG"
    assert req(port, "GET", "/projects/fjord/project.json")[0] == 404  # not an image
    assert req(port, "GET", "/../../etc/passwd")[0] == 404
    assert req(port, "GET", "/%2e%2e/%2e%2e/etc/hostname")[0] == 404


def test_rate_and_note(proj, server):
    port, _ = server
    st, _, r = req(port, "POST", "/api/projects/fjord/rate", {"images": ["2:1"], "rating": "pick"})
    assert st == 200, r
    assert r["result"][0]["rating"] == "pick" and r["data"]["counts"]["pick"] == 1
    assert boards.load("fjord")["log"][-1]["author"] == "human"
    st, _, r = req(port, "POST", "/api/projects/fjord/rate", {"images": ["2:1"], "rating": "clear"})
    assert st == 200 and r["data"]["counts"]["pick"] == 0
    st, _, r = req(port, "POST", "/api/projects/fjord/note", {"text": "the light!", "image": "1:2"})
    assert st == 200 and r["data"]["passes"][0]["images"][1]["notes"][0]["text"] == "the light!"
    st, _, r = req(port, "POST", "/api/projects/fjord/note", {"text": "go warmer"})
    assert st == 200 and r["data"]["notes"][-1]["text"] == "go warmer"


@pytest.mark.parametrize("body, msg", [
    ({"images": ["9:1"], "rating": "pick"}, "no pass 9"),
    ({"images": ["1:1"], "rating": "love"}, "rating must be"),
    ({"images": "1:1", "rating": "pick"}, "images:"),
    ({"images": ["/nope.png"], "rating": "pick"}, "no such image"),
])
def test_rate_errors(proj, server, body, msg):
    st, _, r = req(server[0], "POST", "/api/projects/fjord/rate", body)
    assert st == 400 and msg in r["error"]
    assert boards.load("fjord")["images"] == {}


def test_note_errors_and_unknowns(proj, server):
    port, _ = server
    assert req(port, "POST", "/api/projects/fjord/note", {"text": "  "})[0] == 400
    assert req(port, "POST", "/api/projects/nope/note", {"text": "x"})[0] == 404
    assert req(port, "POST", "/api/projects/fjord/delete", {})[0] == 404
    assert req(port, "GET", "/api/projects/fjord/nope")[0] == 404
    assert req(port, "POST", "/api/projects/fjord/note", raw=b"{nope", headers={"Content-Type": "application/json"})[0] == 400
    assert req(port, "POST", "/api/projects/fjord/note", raw=b"[1]", headers={"Content-Type": "application/json"})[0] == 400


def test_refuses_other_sites(proj, server):
    """Another web page (or a DNS-rebound name) must not rate or start GPU jobs."""
    port, _ = server
    st, _, _ = req(port, "POST", "/api/projects/fjord/note", raw=b'{"text":"x"}',
                   headers={"Content-Type": "text/plain"})  # what a no-preflight cross-site fetch can send
    assert st == 415
    st, _, _ = req(port, "POST", "/api/projects/fjord/note", {"text": "x"}, headers={"Origin": "https://evil.example"})
    assert st == 403
    st, _, _ = req(port, "GET", "/api/projects/fjord/board", headers={"Host": f"evil.example:{port}"})
    assert st == 403
    assert boards.load("fjord")["notes"] == []
    st, _, _ = req(port, "POST", "/api/projects/fjord/note", {"text": "ok"}, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert st == 200


def test_stamp_follows_changes_made_elsewhere(proj, server):
    port, _ = server
    s1 = req(port, "GET", "/api/projects/fjord/stamp")[2]["stamp"]
    time.sleep(0.01)
    boards.rate("fjord", ["1:1"], "reject", author="claude")  # e.g. Claude through MCP
    s2 = req(port, "GET", "/api/projects/fjord/stamp")[2]["stamp"]
    assert s2 != s1
    time.sleep(0.01)
    projects.set_state("fjord", "warmer", prompt="a warm fjord")
    assert req(port, "GET", "/api/projects/fjord/stamp")[2]["stamp"] != s2


def test_generate_more(proj, server, comfy):
    port, jobs = server
    st, _, r = req(port, "POST", "/api/projects/fjord/gen", {"count": 3})
    assert st == 202 and r["job"]["state"] == "running" and r["job"]["slug"] == "fjord"
    j = wait_job(jobs)
    assert j["state"] == "done", j
    p = projects.load("fjord")["passes"][-1]
    assert len(projects.load("fjord")["passes"]) == 3 and p["count"] == 3 and p["author"] == "human"
    st, _, r = req(port, "GET", "/api/projects/fjord/board")
    assert len(r["data"]["passes"]) == 3 and r["job"]["state"] == "done"


def test_generate_one_at_a_time(proj, server, comfy):
    port, jobs = server
    comfy.delay = 1.0
    assert req(port, "POST", "/api/projects/fjord/gen", {"count": 1})[0] == 202
    st, _, r = req(port, "POST", "/api/projects/fjord/gen", {"count": 1})
    assert st == 409 and "already generating" in r["error"]
    assert wait_job(jobs)["state"] == "done"
    assert req(port, "POST", "/api/projects/fjord/gen", {"count": 1})[0] == 202
    wait_job(jobs)


@pytest.mark.parametrize("count", [0, 17, "4", True, 2.5])
def test_generate_count_checked(proj, server, count):
    st, _, r = req(server[0], "POST", "/api/projects/fjord/gen", {"count": count})
    assert st == 400 and "count" in r["error"]


def test_generate_failure_is_reported(proj, server, comfy):
    port, jobs = server
    comfy.fail_jobs = True
    assert req(port, "POST", "/api/projects/fjord/gen", {"count": 1})[0] == 202
    j = wait_job(jobs)
    assert j["state"] == "failed" and j["error"]
    assert req(port, "GET", "/api/projects/fjord/stamp")[2]["job"]["state"] == "failed"


def test_generate_needs_a_prompt(out_dir, server):
    projects.new("empty", "Empty")
    st, _, r = req(server[0], "POST", "/api/projects/empty/gen", {"count": 1})
    assert st == 400 and "no prompt" in r["error"]


# ---------------------------------------------------------------- CLI

def test_cli_help_and_errors(out_dir, capsys):
    assert imagegen.main(["board"]) == 2
    assert imagegen.main(["board", "--help"]) == 0
    assert "gen board serve" in capsys.readouterr().out
    assert imagegen.main(["board", "nope"]) == 2
    assert imagegen.main(["board", "serve", "nope"]) == 1
    assert "no project 'nope'" in capsys.readouterr().err


def test_cli_port_in_use(proj, capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert imagegen.main(["board", "serve", "fjord", "--port", str(port)]) == 1
    finally:
        s.close()
    assert f"--port {port + 1}" in capsys.readouterr().err


def test_cli_serves_and_opens(proj, monkeypatch, capsys, stub_calls):
    def fake_forever(self, *a, **k):
        raise KeyboardInterrupt  # Ctrl+C straight away
    monkeypatch.setattr(boardserve.ThreadingHTTPServer, "serve_forever", fake_forever)
    assert imagegen.main(["board", "serve", "fjord", "--port", "0", "--open"]) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out and "/projects/fjord/" in out
    assert any(c.startswith("explorer.exe http://127.0.0.1:") for c in stub_calls())


def test_images_revalidate(proj, server, out_dir):
    port, _ = server
    st, _, data = req(port, "GET", "/api/projects/fjord/board")
    src = data["data"]["passes"][0]["images"][0]["src"]
    url = "/" + (out_dir.resolve() / "projects" / "fjord" / src).resolve().relative_to(out_dir.resolve()).as_posix()
    st, h, _ = req(port, "GET", url)
    assert st == 200 and h["Cache-Control"] == "no-cache" and h["Last-Modified"]
    st, _, body = req(port, "GET", url, headers={"If-Modified-Since": h["Last-Modified"]})
    assert st == 304 and body == b""
    assert req(port, "GET", "/favicon.ico")[0] == 204


def test_stamp_follows_cast_characters(out_dir, server):
    import characters
    characters.new("tulu", description="a tiny green cthulhu")
    projects.new("wall", "Wall", prompt="a beach", cast={"tulu": None})
    port, _ = server
    s1 = req(port, "GET", "/api/projects/wall/stamp")[2]["stamp"]
    time.sleep(0.01)
    characters.set_state("tulu", description="a tiny teal cthulhu")  # stales assets showing tulu
    assert req(port, "GET", "/api/projects/wall/stamp")[2]["stamp"] != s1


def test_unexpected_errors_get_an_answer(proj, server, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("board.json is read-only")
    monkeypatch.setattr(boards, "rate", boom)
    st, _, r = req(server[0], "POST", "/api/projects/fjord/rate", {"images": ["1:1"], "rating": "pick"})
    assert st == 500 and "read-only" in r["error"]


def test_ctrl_c_waits_for_a_running_pass(proj, monkeypatch, capsys):
    finished = []

    def fake_forever(self, *a, **k):
        def slow(slug, n):
            time.sleep(0.3)
            finished.append(slug)
            return {}
        self.RequestHandlerClass.jobs.start("fjord", 1, run=slow)
        raise KeyboardInterrupt
    monkeypatch.setattr(boardserve.ThreadingHTTPServer, "serve_forever", fake_forever)
    assert imagegen.main(["board", "serve", "--port", "0"]) == 0
    assert finished == ["fjord"]
    assert "still generating" in capsys.readouterr().out
