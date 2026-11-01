"""mcp_server.py tools: called directly, plus through the MCP layer for listing and result conversion."""
import asyncio
import time

import pytest

import imagegen
import mcp_server

EXPECTED_TOOLS = {"generate", "get_job", "vary", "edit", "cutout", "doctor", "list_presets", "recent_runs",
                  "view_image", "set_wallpaper", "open_contact_sheet", "project_list", "project_get",
                  "project_new", "project_update", "project_generate", "project_promote", "project_keep",
                  "project_doc", "project_import", "character_list", "character_get", "character_new",
                  "character_update"}


@pytest.fixture(autouse=True)
def fresh_jobs(monkeypatch):
    monkeypatch.setattr(mcp_server, "JOBS", {})


def test_tool_list():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t.description, f"{t.name} has no description"


def test_list_presets_through_mcp():
    res = asyncio.run(mcp_server.mcp.call_tool("list_presets", {}))
    assert not res.is_error
    assert '"wallpaper"' in res.content[0].text and "3840x2160" in res.content[0].text


def test_generate_done_within_wait(comfy):
    out = mcp_server.generate("a fox", count=2, seed=1)
    summary, *thumbs = out
    assert summary["status"] == "done" and len(summary["images"]) == 2 and summary["base_seed"] == 1
    assert len(thumbs) == 2 and thumbs[0].data[:2] == b"\xff\xd8"  # JPEG thumbnails


def test_generate_without_preview(comfy):
    assert len(mcp_server.generate("a fox", preview=False)) == 1


def test_count_is_capped(comfy):
    out = mcp_server.generate("a fox", count=50, preview=False)
    assert len(out[0]["images"]) == mcp_server.MAX_COUNT


def test_slow_job_returns_job_id_then_get_job(comfy, monkeypatch):
    """The bridge times out at ~60s, so generate hands back a job_id and get_job keeps waiting."""
    monkeypatch.setattr(mcp_server, "WAIT_S", 0.2)
    comfy.delay = 1.5  # imagegen polls /history every 1s
    first = mcp_server.generate("a fox", preview=False)[0]
    assert first["status"] == "running" and "get_job" in first["next"]
    again = mcp_server.get_job(first["job_id"], wait=0)[0]
    assert again["status"] == "running"
    deadline = time.time() + 10
    while (res := mcp_server.get_job(first["job_id"])[0])["status"] == "running":
        assert time.time() < deadline, "job never finished"
    assert res["status"] == "done" and res["images"]


def test_job_error_is_reported(comfy):
    comfy.fail_jobs = True
    out = mcp_server.generate("a fox")[0]
    assert out["status"] == "error" and "ComfyUI failed" in out["error"]


def test_unknown_job():
    with pytest.raises(ValueError, match="unknown job"):
        mcp_server.get_job("nope")


def test_vary_and_cutout(comfy, make_image):
    src = make_image()
    out = mcp_server.vary(str(src), count=1, prompt="a cat", preview=False)[0]
    assert out["status"] == "done"
    cut = mcp_server.cutout(str(src))
    assert cut["path"].endswith("src-transparent.png")


def test_view_image_flattens_transparency(make_image):
    img = mcp_server.view_image(str(make_image("t.png", color=(0, 0, 0, 0), mode="RGBA")), size=64)
    assert img.data[:2] == b"\xff\xd8"


def test_recent_runs_and_open_sheet(make_run, stub_calls):
    run = make_run(n=1)
    assert mcp_server.recent_runs(1)[0]["run_dir"] == run["run_dir"]
    assert mcp_server.open_contact_sheet(run["run_dir"] + "/index.html") == "opened"
    assert any(c.startswith("explorer.exe") for c in stub_calls())


def test_project_tools(comfy, make_image):
    s = mcp_server.project_new("fox", prompt="a fox", palette=["#ff8800"])
    assert s["version"] == 1
    v = mcp_server.project_update("fox", "more steps", params={"steps": 10})
    assert v["version"] == 2
    out = mcp_server.project_generate("fox", count=1, preview=False)[0]
    assert out["status"] == "done"
    r = mcp_server.project_promote("fox", out["images"][0]["path"], "fox", "the pick")
    assert r["asset_version"] == 1
    mcp_server.project_update("fox", "new look", prompt="a red fox")
    got = mcp_server.project_get("fox", preview=True)
    assert got[0]["assets"]["fox"]["stale"] and len(got) == 2  # summary + 1 asset thumbnail
    assert mcp_server.project_keep("fox", "fox", "fine")["version"] == 5
    assert mcp_server.project_list() == [{"slug": "fox", "title": "fox", "version": 5}]


def test_doctor_tool_skips_mcp_import(monkeypatch):
    seen = {}
    import doctor
    monkeypatch.setattr(doctor, "run_checks", lambda include_mcp: seen.setdefault("mcp", include_mcp) or [])
    mcp_server.doctor()
    assert seen == {"mcp": False}


def test_stdio_server_end_to_end(comfy, tmp_path):
    """The way the Claude app runs it: mcp_server.py as a subprocess over stdio. Catches stray prints
    to stdout (they corrupt the protocol) and start-up errors, then runs one real generate."""
    import os
    import sys

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(imagegen.ROOT / "mcp_server.py")],
                                   env=dict(os.environ), cwd=str(tmp_path))

    async def go():
        with open(tmp_path / "server-stderr.log", "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    names = {t.name for t in (await s.list_tools()).tools}
                    res = await s.call_tool("generate", {"prompt": "a fox", "seed": 3, "preview": True})
                    return names, res

    names, res = asyncio.run(asyncio.wait_for(go(), 60))
    assert names == EXPECTED_TOOLS
    assert not res.is_error, res.content
    kinds = [c.type for c in res.content]
    assert kinds[0] == "text" and '"status": "done"' in res.content[0].text and "image" in kinds
