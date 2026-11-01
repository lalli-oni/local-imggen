"""Per-stage timing (#2): ComfyUI's websocket events -> seconds per stage in sidecars, run.json, gen and MCP.

The fake ComfyUI plays each job's nodes in dependency order with `node_s` sleeps, so stage times are
checked against those sleeps with some slack (scheduling noise), never exact values.
"""
import json
import time
from pathlib import Path

import pytest

import imagegen

SLACK = 0.15


def _sidecar(im: dict) -> dict:
    p = Path(im["path"])
    return json.loads(p.with_name(p.stem.removesuffix("-transparent") + ".json").read_text())


def _closed(comfy, timeout=2.0):
    """The client's frames, once the fake has read its close frame (it arrives just after generate returns)."""
    deadline = time.time() + timeout
    while not (comfy.ws_received and comfy.ws_received[-1][0] == 8) and time.time() < deadline:
        time.sleep(0.01)
    return comfy.ws_received


def _near(value, expected):
    assert expected - 0.02 <= value <= expected + SLACK, (value, expected)


def test_sdxl_base_stages(comfy):
    comfy.node_s = {"KSampler": 0.3, "VAEDecode": 0.1, "CheckpointLoaderSimple": 0.1}
    run = imagegen.generate("a fox", "draft", seed=1)
    t = _sidecar(run["images"][0])["timing"]
    _near(t["sample_s"], 0.3)
    _near(t["decode_s"], 0.1)
    _near(t["load_s"], 0.1)
    assert t["comfy_s"] >= 0.5 and t["queue_s"] >= 0 and t["fetch_s"] >= 0
    assert t["total_s"] == pytest.approx(t["comfy_s"] + t["fetch_s"], abs=0.02)
    # nodes in the order ComfyUI ran them: the loader before the sampler that uses it
    classes = [n["class"] for n in t["nodes"]]
    assert classes.index("CheckpointLoaderSimple") < classes.index("KSampler") < classes.index("VAEDecode")
    assert classes[-1] == "SaveImage"
    assert sum(n["s"] for n in t["nodes"]) == pytest.approx(t["comfy_s"], abs=0.05)


def test_run_json_timing_block(comfy):
    comfy.node_s = {"KSampler": 0.1}
    run = imagegen.generate("a fox", "draft", count=2, seed=1)
    saved = json.loads((Path(run["run_dir"]) / "run.json").read_text())
    rt = saved["timing"]
    assert rt == run["timing"]
    assert rt["total_s"] >= rt["setup_s"] >= 0 and "queue_s" in rt
    per = [im["timing"] for im in saved["images"]]
    assert rt["sample_s"] == pytest.approx(sum(t["sample_s"] for t in per), abs=0.02)
    assert rt["fetch_s"] == pytest.approx(sum(t["fetch_s"] for t in per), abs=0.02)
    assert "nodes" not in per[0]  # node lists live in the sidecars only
    assert rt["summary"].startswith("2 images in ") and "(first " in rt["summary"] and "sample " in rt["summary"]
    assert "note" not in rt


def test_upscale_workflow(comfy):
    comfy.node_s = {"KSampler": 0.1, "ImageUpscaleWithModel": 0.2}
    run = imagegen.generate("fjord", "wallpaper", seed=1)
    t = _sidecar(run["images"][0])["timing"]
    _near(t["upscale_s"], 0.2)
    assert "UpscaleModelLoader" in [n["class"] for n in t["nodes"]] and "load_s" in t
    assert "upscale " in run["timing"]["summary"]


def test_post_step_is_timed(comfy):
    run = imagegen.generate("rocket", "icon", seed=1)
    t = _sidecar(run["images"][0])["timing"]
    assert "remove_bg_s" in t and "remove_bg_s" in run["timing"]
    assert t["total_s"] == pytest.approx(t["comfy_s"] + t["fetch_s"] + t["remove_bg_s"], abs=0.02)


def test_cached_nodes(comfy):
    comfy.cached = ["4", "6", "7"]  # loader and prompts unchanged since the last job
    run = imagegen.generate("a fox", "draft", seed=1)
    t = _sidecar(run["images"][0])["timing"]
    assert t["cached"] == ["4", "6", "7"]
    assert not {"4", "6", "7"} & {n["node"] for n in t["nodes"]}
    assert "load_s" not in t


def test_without_websocket_falls_back_to_history(comfy):
    comfy.ws = False
    run = imagegen.generate("a fox", "draft", seed=1)
    t = _sidecar(run["images"][0])["timing"]
    assert {"queue_s", "comfy_s", "fetch_s", "total_s"} <= set(t)
    assert "sample_s" not in t and "nodes" not in t
    assert "no websocket" in run["timing"]["note"]


def test_socket_end_arriving_after_history_keeps_the_split(comfy):
    """/history can show the job done before the socket's last frames are read."""
    comfy.node_s = {"KSampler": 0.2}
    comfy.ws_end_lag = 1.2  # past imagegen's 1s /history poll, so the poll sees it first
    run = imagegen.generate("a fox", "draft", seed=1)
    t = _sidecar(run["images"][0])["timing"]
    assert "nodes" in t and t["sample_s"] >= 0.18


def test_job_with_two_outputs_counts_comfy_once(comfy):
    comfy.node_s = {"KSampler": 0.3}
    comfy.outputs_per_job = 2
    run = imagegen.generate("a fox", "draft", seed=1)
    first, second = (im["timing"] for im in run["images"])
    assert first["total_s"] >= 0.3 > second["total_s"]
    assert run["timing"]["sample_s"] == first["sample_s"]  # once per job, not per image


def test_broken_timing_does_not_lose_images(comfy, monkeypatch):
    def boom(*a):
        raise TypeError("odd event")
    monkeypatch.setattr(imagegen, "job_timing", boom)
    run = imagegen.generate("a fox", "draft", seed=1)
    assert Path(run["images"][0]["path"]).is_file() and (Path(run["run_dir"]) / "run.json").is_file()


def test_websocket_ping_is_answered_and_socket_closed(comfy):
    imagegen.generate("a fox", "draft", seed=1)
    received = _closed(comfy)
    assert (10, b"ping") in received  # pong with the ping's payload
    assert received[-1][0] == 8  # a close frame when generate() is done


def test_failed_job_still_closes_websocket(comfy):
    comfy.fail_jobs = True
    with pytest.raises(RuntimeError, match="ComfyUI failed"):
        imagegen.generate("a fox", "draft", seed=1)
    assert _closed(comfy)[-1][0] == 8


# ---------------------------------------------------------------- job_timing / summary, no server

WF = {"3": {"class_type": "KSampler"}, "4": {"class_type": "CheckpointLoaderSimple"},
      "8": {"class_type": "VAEDecode"}, "9": {"class_type": "SaveImage"}}


def _ev(*items):
    return [(t, kind, data) for t, kind, data in items]


def test_job_timing_from_events():
    ev = _ev((10.0, "execution_start", {}), (10.0, "execution_cached", {"nodes": []}),
             (10.1, "executing", {"node": "4"}), (12.1, "executing", {"node": "3"}),
             (52.1, "executing", {"node": "8"}), (54.1, "executing", {"node": "9"}),
             (54.3, "executing", {"node": None}), (54.3, "execution_success", {}))
    t = imagegen.job_timing(WF, 9.5, ev, {})
    assert t["queue_s"] == 0.5 and t["comfy_s"] == 44.3
    assert (t["load_s"], t["sample_s"], t["decode_s"], t["save_s"]) == (2.0, 40.0, 2.0, 0.2)
    assert [n["class"] for n in t["nodes"]] == ["CheckpointLoaderSimple", "KSampler", "VAEDecode", "SaveImage"]


def test_job_timing_socket_dropped_uses_history():
    """The socket died mid-job: its node times would be cut off, so only /history's totals are used."""
    ev = _ev((10.0, "execution_start", {}), (10.1, "executing", {"node": "4"}))
    hist = {"status": {"messages": [["execution_start", {"timestamp": 10_000}],
                                    ["execution_success", {"timestamp": 60_000}]]}}
    t = imagegen.job_timing(WF, 9.0, ev, hist)
    assert t == {"queue_s": 1.0, "comfy_s": 50.0}


def test_job_timing_odd_history_shapes():
    hist = {"status": {"messages": [["execution_start"], "junk", ["execution_start", {"timestamp": None}],
                                    ["execution_success", {"timestamp": 5000}]]}}
    assert imagegen.job_timing(WF, 0, None, hist) == {}


def test_job_timing_unknown_node_is_other():
    ev = _ev((0, "execution_start", {}), (0, "executing", {"node": "77"}), (1, "execution_success", {}))
    assert imagegen.job_timing(WF, 0, ev, {})["other_s"] == 1


def test_job_timing_nothing_known():
    assert imagegen.job_timing(WF, 0, None, {}) == {}


def test_stage_of():
    assert imagegen.stage_of("UNETLoader") == imagegen.stage_of("DualCLIPLoader") == "load"
    assert imagegen.stage_of("KSamplerAdvanced") == "sample"
    assert imagegen.stage_of("VAEEncodeForInpaint") == "encode"
    assert imagegen.stage_of("ImageCompositeMasked") == "other"
    assert imagegen.stage_of("FluxKontextImageScale") == "other"  # a resize of the input, not an upscale
    assert imagegen.stage_of("LoadImage") == "load" and imagegen.stage_of("ImageScale") == "upscale"
    # the bake-off graphs (FLUX.2 klein, Qwen-Image-2.1)
    assert imagegen.stage_of("SamplerCustomAdvanced") == "sample" and imagegen.stage_of("KSamplerSelect") == "other"
    assert imagegen.stage_of("TextEncodeQwenImage21") == "encode" and imagegen.stage_of("CLIPLoader") == "load"


def test_summary_line():
    run = {"total_s": 210.3, "setup_s": 1.0, "comfy_s": 200, "sample_s": 160.2, "upscale_s": 30.0,
           "load_s": 3.04, "fetch_s": 0.01}
    images = [{"total_s": 80.1}, {"total_s": 43.0}, {"total_s": 43.8}]
    assert imagegen.timing_summary(run, images) == \
        "3 images in 210.3s (first 80.1s, then ~43.4s each): load 3.0s, sample 160.2s, upscale 30.0s"
    assert imagegen.timing_summary({"total_s": 5.0}, [{"total_s": 4.0}]) == "1 image in 5.0s"


# ---------------------------------------------------------------- where it is shown

def test_sheet_and_mcp_show_timing(comfy):
    import mcp_server
    from test_contact_sheet import sheet_data
    comfy.node_s = {"KSampler": 0.1}
    out = mcp_server.generate("a fox", preview=False)[0]
    assert out["timing"].startswith("1 image in ")
    run_dir = Path(out["images"][0]["path"]).parent
    d = sheet_data(run_dir)
    assert d["timing"] == out["timing"]
    assert d["images"][0]["time_s"] > 0 and "sample " in d["images"][0]["timing"]


def test_old_run_without_timing_still_builds(make_run):
    from test_contact_sheet import sheet_data
    run = make_run(n=1)
    imagegen.write_contact_sheet(Path(run["run_dir"]), run)
    d = sheet_data(run["run_dir"])
    assert d["timing"] is None and "time_s" not in d["images"][0]
