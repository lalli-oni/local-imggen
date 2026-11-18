"""appserve.py (#82): `gen app serve`, the viewer app's read-only JSON API and its static files."""
import json
import threading

import pytest

import appserve
import boards
import characters
import imagegen
import projects
from test_boardserve import req


@pytest.fixture
def world(out_dir, comfy, make_image, make_run):
    """A character in its converge stage, a project casting it (2 passes, rated), a plain run,
    a daily wallpaper run, and a daily.log."""
    characters.new("tulu", "lil tulu", description="chibi cthulhu", palette=["#33aa77"],
                   refs={"main": str(make_image("tulu.png"))})
    characters.set_state("tulu", "converging", stage="converge")
    projects.new("walls", "Walls", "wallpapers", prompt="misty fjord", cast={"tulu": None})
    projects.run_pass("walls", count=2, seed=1)
    projects.set_state("walls", "warmer", prompt="misty fjord, warm light")
    projects.run_pass("walls", count=2, seed=10)
    boards.rate("walls", ["2:1"], "pick", "the light!")
    boards.rate("walls", ["1:2"], "reject")
    boards.note("walls", "go warmer")
    projects.promote("walls", str(boards._abs(boards.resolve(projects.load("walls"), "2:1")[0])), "hero",
                     shows=["tulu"])
    make_run(n=2, preset="draft", prompt="a red fox", date="2026-11-01", time="090000")
    make_run(n=2, preset="wallpaper_cast", prompt="synthwave beach", date="2026-11-17", time="060000",
             daily={"date": "2026-11-17", "theme": "synthwave beach", "from": "themes", "props": ["a boombox"],
                    "cast": {"tulu": 2}, "styles": ["synthwave"]})
    (out_dir / "daily.log").write_text("2026-11-16 06:00:01 FAILED, wallpaper unchanged: RuntimeError: boom\n"
                                       "2026-11-17 06:03:12 ok: 'synthwave beach' -> C:\\x.png\n")
    return out_dir


@pytest.fixture
def server(out_dir, tmp_path):
    """The app server on a free port, with a stand-in app build: yields the port."""
    build = tmp_path / "build"
    (build / "_app" / "immutable").mkdir(parents=True)
    (build / "index.html").write_text("<!doctype html><title>app</title>")
    (build / "_app" / "immutable" / "x.js").write_text("export {}")
    srv = appserve.make_server(0, app_dir=build)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield srv.server_port
    srv.shutdown()
    srv.server_close()


def get(port, path, **kw):
    return req(port, "GET", path, **kw)


def fetch_ok(port, url):
    st, h, body = get(port, url)
    assert st == 200, (url, st)
    return h, body


def test_overview(world, server):
    st, _, d = get(server, "/api/overview")
    assert st == 200
    assert (d["projects"], d["characters"]) == (1, 1)
    assert d["runs"] == 4 and d["images"] == 8  # 2 passes + 2 runs
    assert d["wallpaper"]["daily"]["theme"] == "synthwave beach"
    assert d["recent"][0]["id"].startswith(d["recent"][0]["date"])
    fetch_ok(server, d["wallpaper"]["images"][0]["url"])


def test_projects_list_and_detail(world, server):
    st, _, d = get(server, "/api/projects")
    assert st == 200
    [p] = d["projects"]
    assert (p["slug"], p["version"], p["cast"], p["passes"], p["assets"]) == ("walls", 3, ["tulu"], 2, 1)  # promote made v3
    assert p["stage"]["stage"] == "converge" and p["stage"]["character"] == "tulu"
    assert p["counts"]["pick"] == 1 and p["counts"]["reject"] == 1
    assert p["stale"] == 0
    h, _ = fetch_ok(server, p["cover"])  # the pick is the cover
    assert h["Content-Type"] == "image/png"

    st, _, d = get(server, "/api/projects/walls")
    assert st == 200
    assert [x["n"] for x in d["passes"]] == [2, 1]  # newest first
    assert [v["version"] for v in d["versions"]] == [3, 2, 1]
    im = d["passes"][0]["images"][0]
    assert im["id"] == "2:1" and im["rating"] == "pick" and "src" not in im and "path" not in im
    assert "dir" not in d  # no tulu paths in the API, only URLs
    fetch_ok(server, im["url"])
    fetch_ok(server, d["assets"][0]["url"])
    fetch_ok(server, d["passes"][0]["sheet"])
    assert d["notes"][0]["text"] == "go warmer"


def test_stale_asset_counts(world, server):
    characters.set_state("tulu", "new look", description="chibi cthulhu, tiny wings")
    _, _, d = get(server, "/api/projects")
    assert d["projects"][0]["stale"] == 1
    _, _, d = get(server, "/api/projects/walls")
    assert d["assets"][0]["stale"] and d["assets"][0]["reasons"]


def test_characters(world, server):
    st, _, d = get(server, "/api/characters")
    [c] = d["characters"]
    assert (c["name"], c["stage"], c["projects"], c["palette"]) == ("tulu", "converge", ["walls"], ["#33aa77"])
    fetch_ok(server, c["ref"])
    st, _, d = get(server, "/api/characters/tulu")
    assert st == 200 and d["version"] == 2
    assert [v["stage"] for v in d["versions"]] == ["converge", "explore"]
    assert d["used_by"] == {"walls": {"pin": None, "assets": ["hero"]}}
    fetch_ok(server, d["versions"][1]["refs"][0]["url"])


def test_runs_paging_and_filters(world, server):
    _, _, d = get(server, "/api/runs?limit=2")
    assert d["total"] == 4 and len(d["runs"]) == 2
    dates = [r["date"] for r in get(server, "/api/runs")[2]["runs"]]
    assert dates == sorted(dates, reverse=True)
    assert "wallpaper_cast" in d["presets"] and "draft" in d["presets"]
    page2 = get(server, "/api/runs?offset=2&limit=2")[2]["runs"]
    assert len(page2) == 2 and not {r["id"] for r in page2} & {r["id"] for r in d["runs"]}
    fox = get(server, "/api/runs?q=FOX")[2]
    assert fox["total"] == 1 and fox["runs"][0]["prompt"] == "a red fox"
    assert get(server, "/api/runs?wallpaper=1")[2]["total"] == 1
    assert get(server, "/api/runs?preset=wallpaper_cast")[2]["total"] == 1
    assert get(server, "/api/runs?limit=x")[0] == 400


def test_run_detail(world, server):
    st, _, d = get(server, "/api/runs/2026-11-01/090000-draft-a-red-fox")
    assert st == 200 and d["label"] == "draft-a-red-fox" and d["time"] == "09:00:00"
    assert d["images"][0]["params"]["width"] == 1024 and d["images"][0]["seed"] == 100
    assert get(server, "/api/runs/2026-11-01/nope")[0] == 404
    assert get(server, "/api/runs/2026-11-01/..")[0] == 404
    assert get(server, "/api/runs/../../etc")[0] == 404


def test_run_index_rereads_changed_runs(world, server, out_dir):
    f = out_dir / "2026-11-01" / "090000-draft-a-red-fox" / "run.json"
    r = json.loads(f.read_text())
    r["prompt"] = "a blue fox"
    f.write_text(json.dumps(r))
    import os
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 10_000_000))
    assert get(server, "/api/runs?q=blue")[2]["total"] == 1


def test_wallpapers_and_log(world, server):
    _, _, d = get(server, "/api/wallpapers")
    assert [r["daily"]["theme"] for r in d["runs"]] == ["synthwave beach"]
    assert d["runs"][0]["daily"]["props"] == ["a boombox"]
    assert [x["ok"] for x in d["log"]] == [True, False]  # newest first
    assert d["log"][1]["text"].startswith("wallpaper unchanged")


def test_unknown_and_empty(out_dir, server):
    assert get(server, "/api/projects/nope")[0] == 404
    assert get(server, "/api/projects/..%2F..")[0] == 404
    assert get(server, "/api/characters/nope")[0] == 404
    assert get(server, "/api/nope")[0] == 404
    _, _, d = get(server, "/api/overview")
    assert d == {"projects": 0, "characters": 0, "runs": 0, "images": 0, "wallpaper": None, "recent": []}
    assert get(server, "/api/wallpapers")[2] == {"runs": [], "log": []}


def test_read_only_and_host_check(world, server):
    st, _, d = req(server, "POST", "/api/projects/walls", body={})
    assert st == 405 and "read-only" in d["error"]
    assert req(server, "DELETE", "/api/projects/walls")[0] == 405
    assert get(server, "/api/overview", headers={"Host": "evil.example"})[0] == 403


def test_files_stay_in_output_dir(world, server, tmp_path):
    (tmp_path / "secret.png").write_bytes(b"x")
    assert get(server, "/files/../secret.png")[0] == 404
    assert get(server, "/files/%2e%2e/secret.png")[0] == 404
    assert get(server, "/files/daily.log")[0] == 404  # images and contact sheets only


def test_static_app(server):
    h, body = fetch_ok(server, "/")
    assert b"<title>app</title>" in body and h["Cache-Control"] == "no-cache"
    _, body = fetch_ok(server, "/projects/walls")  # a client-side route gets the app
    assert b"<title>app</title>" in body
    h, _ = fetch_ok(server, "/_app/immutable/x.js")
    assert "immutable" in h["Cache-Control"] and h["Content-Type"].startswith("text/javascript")
    assert get(server, "/_app/missing.js")[0] == 404
    assert get(server, "/../appserve.py")[0] == 404


def test_not_built(out_dir, tmp_path):
    srv = appserve.make_server(0, app_dir=tmp_path / "nothing")
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        st, _, body = get(srv.server_port, "/")
        assert st == 503 and b"npm run build" in body
        assert get(srv.server_port, "/api/overview")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_help(capsys):
    assert imagegen.main(["app", "--help"]) == 0
    assert "gen app serve" in capsys.readouterr().out
    assert imagegen.main(["app", "nope"]) == 2
