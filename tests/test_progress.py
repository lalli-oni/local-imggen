"""Live progress in gen (#61): preset header, stages, ETA, memory bars (progress.py + generate())."""
from __future__ import annotations

import json
import re

import pytest

import imagegen
import progress

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def filled(preset: str, **extra) -> dict:
    p = imagegen.list_presets()[preset]
    wf = p.get("workflow") or p["edit_workflow"]
    params = {k: v for k, v in p.items() if k not in imagegen.NOT_PARAMS}
    params.update(prompt="x", negative="", seed=1, checkpoint=imagegen.load_config()["checkpoint"], **extra)
    return imagegen.fill(json.loads((imagegen.ROOT / "workflows" / wf).read_text()), params)


def test_describe_sdxl_wallpaper():
    line = progress.describe(filled("wallpaper_sdxl"), "wallpaper_sdxl", 4, imagegen.stage_of)
    assert line == ("wallpaper_sdxl: sd_xl_base_1.0 · 1 pass, 30 steps · 1344x768 -> 3840x2160 (RealESRGAN_x4plus)"
                    " · 4 images")


def test_describe_klein_edit_reads_steps_from_the_scheduler():
    wf = imagegen.list_presets()["klein"]["edit_workflow"]
    p = imagegen.list_presets()["klein"]
    params = {k: v for k, v in p.items() if k not in imagegen.NOT_PARAMS}
    params.update(prompt="x", seed=1, image="src.png", width=1024, height=1024)
    line = progress.describe(imagegen.fill(json.loads((imagegen.ROOT / "workflows" / wf).read_text()), params),
                             "klein", 1, imagegen.stage_of, "edit")
    assert line.startswith("klein edit: flux-2-klein")
    assert f"1 pass, {p['steps']} steps" in line and line.endswith("· 1 image")


def test_describe_odd_workflow_does_not_crash():
    assert progress.describe({}, "x", 2, imagegen.stage_of) == "x: ? · 2 images"


def _timed_run(make_run, out_dir, preset, timings, time="120000", **extra):
    run = make_run(n=len(timings), preset=preset, time=time, **extra)
    for im, t in zip(run["images"], timings):
        im["timing"] = t
    (out_dir / "2026-10-31" / run["run_dir"].rsplit("/", 1)[1] / "run.json").write_text(json.dumps(run))
    return run


def test_history_estimate_medians_same_preset_and_mode(make_run, out_dir):
    # a cold first image loads; the rest have no load_s (cached), which counts as 0, not as missing
    _timed_run(make_run, out_dir, "wallpaper", [{"load_s": 9.0, "sample_s": 40.0}, {"sample_s": 30.0},
                                                {"sample_s": 30.0}], "100000")
    _timed_run(make_run, out_dir, "wallpaper", [{"sample_s": 20.0, "upscale_s": 8.0}], "110000")
    _timed_run(make_run, out_dir, "draft", [{"sample_s": 999.0}], "120000")          # other preset
    _timed_run(make_run, out_dir, "wallpaper", [{"sample_s": 999.0}], "130000", edit=True)  # other mode
    est = progress.history_estimate(out_dir, "wallpaper", "", imagegen.COMFY_STAGES)
    assert est == {"load": 0.0, "sample": 30.0, "upscale": 0.0}
    assert progress.history_estimate(out_dir, "icon", "", imagegen.COMFY_STAGES) == {}


def test_history_estimate_counts_each_job_once_and_skips_junk(make_run, out_dir):
    run = _timed_run(make_run, out_dir, "draft", [{"sample_s": 10.0}, {"sample_s": 10.0}, {"sample_s": 40.0}])
    run["images"][1]["seconds"] = None  # a job's second output repeats its times
    run["images"].append({"timing": []})  # junk from a hand edit
    (out_dir / "2026-10-31" / run["run_dir"].rsplit("/", 1)[1] / "run.json").write_text(json.dumps(run))
    (out_dir / "not-a-date").mkdir()
    assert progress.history_estimate(out_dir, "draft", "", imagegen.COMFY_STAGES) == {"sample": 25.0}
    assert progress.history_estimate(out_dir / "missing", "draft", "", imagegen.COMFY_STAGES) == {}


WF = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
      "2": {"class_type": "CLIPTextEncode", "inputs": {}},
      "3": {"class_type": "KSampler", "inputs": {}},
      "4": {"class_type": "VAEDecode", "inputs": {}},
      "5": {"class_type": "SaveImage", "inputs": {}}}


def make_progress(count=2, est=None):
    import io
    pr = progress.Progress(WF, count, imagegen.stage_of, imagegen.STAGE_ORDER, "http://127.0.0.1:9",
                           est, stream=io.StringIO(), color=False)
    pr.t0 = 0.0
    return pr


def play(pr, pid, t, *nodes):
    """executing events for nodes, one second each, starting at t."""
    pr.on_event(pid, "execution_start", {}, t)
    for n in nodes:
        pr.on_event(pid, "executing", {"node": n}, t)
        t += 1
    return t


def text(pr, now):
    return "\n".join(ANSI.sub("", t) for t, _ in pr.lines(now))


def test_stages_done_running_pending_and_step_count():
    pr = make_progress()
    t = play(pr, "a", 0.0, "1", "2", "3")
    pr.on_event("a", "progress", {"value": 1, "max": 30}, t)
    pr.on_event("a", "progress", {"value": 11, "max": 30}, t + 5)
    out = text(pr, t + 5)
    assert "image 1/2" in out
    assert "✓ load" in out and "✓ encode" in out and "▶ sample 11/30" in out and "○ decode" in out
    assert "○ save" in out


def test_cached_nodes_count_as_done():
    pr = make_progress()
    pr.on_event("a", "execution_cached", {"nodes": ["1", "2"]}, 0.0)
    play(pr, "a", 0.0, "3")
    out = text(pr, 1.0)
    assert "✓ load" in out and "✓ encode" in out and "▶ sample" in out


def test_eta_from_step_rate_plus_history_for_the_rest():
    pr = make_progress(count=2, est={"load": 5.0, "encode": 1.0, "sample": 30.0, "decode": 2.0, "save": 1.0})
    t = play(pr, "a", 0.0, "1", "2", "3")  # sample starts at t=2
    pr.on_event("a", "progress", {"value": 1, "max": 21}, t)
    pr.on_event("a", "progress", {"value": 11, "max": 21}, t + 10)  # 1 s/step: 10 steps left
    eta = pr.eta(t + 10)
    # 10 s sampling + decode 2 + save 1, then one more image of 39 s
    assert eta == pytest.approx(10 + 3 + 39)
    assert "~0:52 left" in text(pr, t + 10)


def test_eta_step_rate_uses_the_last_report_not_now():
    pr = make_progress(count=1, est={"sample": 30.0})
    t = play(pr, "a", 0.0, "3")
    pr.on_event("a", "progress", {"value": 1, "max": 11}, t)
    pr.on_event("a", "progress", {"value": 6, "max": 11}, t + 10)  # 2 s/step, 5 steps left = 10 s
    assert pr.eta(t + 10) == pytest.approx(10.0)
    assert pr.eta(t + 11.5) == pytest.approx(8.5)  # counts down between reports, doesn't grow


def test_display_node_maps_expanded_nodes():
    pr = make_progress()
    pr.on_event("a", "executing", {"node": "3.0.1", "display_node": "3"}, 0.0)
    assert "▶ sample" in text(pr, 1.0)


def test_eta_learns_from_this_runs_finished_image():
    pr = make_progress(count=3, est={"sample": 100.0})
    t = play(pr, "a", 0.0, "1", "2", "3", "4", "5")
    pr.on_event("a", "executing", {"node": None}, t)  # 5 s for the whole image
    assert pr.eta(t) == pytest.approx(10.0)  # two more of 5 s each, history ignored
    assert "image 2/3" in text(pr, t)


def test_no_eta_without_anything_to_go_on():
    pr = make_progress(count=2)
    play(pr, "a", 0.0, "1")
    assert pr.eta(0.5) is None
    assert "left" not in text(pr, 0.5)
    pr2 = make_progress(count=2)
    assert "waiting for ComfyUI" in text(pr2, 0.0)


def test_lines_never_wrap(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    pr = make_progress()
    pr.color = True
    play(pr, "a", 0.0, "1", "2", "3")
    pr.mem = {"vram": 15.5, "vram_total": 16.0, "ram": 10.0, "ram_total": 24.0, "swap": 2.0}
    for t, vis in pr.lines(3.0):
        assert len(ANSI.sub("", t)) == vis <= 39


def test_memory_snapshot_and_colours(comfy, tmp_path, monkeypatch):
    (tmp_path / "meminfo").write_text("MemTotal: 1 kB\nSwapTotal:  4194304 kB\nSwapFree:   3145728 kB\n")
    monkeypatch.setattr(progress, "PROC", tmp_path)
    comfy.vram_free = 1 * 1024 ** 3
    snap = progress.memory_snapshot(comfy.url)
    assert snap == {"vram": 15.0, "vram_total": 16.0, "ram": 4.0, "ram_total": 24.0, "swap": 1.0}
    pr = make_progress()
    pr.color = True
    pr.mem = snap
    third = pr.lines(0.0)[-1][0]
    assert "\x1b[31;1m 15.0/16.0 GiB" in third  # red: 94% of VRAM
    assert "4.0/24.0 GiB" in ANSI.sub("", third) and "swap 1.0 GiB" in third
    assert progress.peak_summary(snap) == "peak memory: VRAM 15.0/16.0 GiB (94%), RAM 4.0/24.0 GiB (17%), swap 1.0 GiB"


def test_memory_snapshot_keeps_ram_when_vram_is_odd(comfy, tmp_path, monkeypatch, ):
    monkeypatch.setattr(progress, "PROC", tmp_path)
    monkeypatch.setattr(progress.urllib.request, "urlopen", lambda *a, **k: __import__("io").BytesIO(json.dumps(
        {"system": {"ram_total": 24 * 1024 ** 3, "ram_free": 12 * 1024 ** 3},
         "devices": [{"name": "gpu", "vram_total": 16 * 1024 ** 3}]}).encode()))
    assert progress.memory_snapshot("http://x") == {"ram": 12.0, "ram_total": 24.0}


def test_memory_snapshot_without_comfy_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "PROC", tmp_path)  # no meminfo either
    assert progress.memory_snapshot("http://127.0.0.1:9") == {}


def test_enabled(monkeypatch):
    import io
    monkeypatch.setenv("IMAGEGEN_PROGRESS", "1")
    assert progress.enabled(io.StringIO())
    monkeypatch.setenv("IMAGEGEN_PROGRESS", "0")
    tty = type("T", (), {"isatty": lambda self: True})()
    assert not progress.enabled(tty)
    monkeypatch.delenv("IMAGEGEN_PROGRESS")
    monkeypatch.setenv("TERM", "xterm")
    assert progress.enabled(tty) and not progress.enabled(io.StringIO())


# ---------------------------------------------------------------- through generate()

def test_generate_draws_the_live_block(comfy, capsys, monkeypatch):
    monkeypatch.setenv("IMAGEGEN_PROGRESS", "1")
    monkeypatch.setattr(progress, "TICK", 0.05)
    monkeypatch.setattr(progress, "MEM_EVERY", 0.05)
    comfy.steps = True
    comfy.node_s = {"KSampler": 0.4, "ImageUpscaleWithModel": 0.3}
    run = imagegen.generate("a misty fjord", "wallpaper_sdxl", count=2, seed=5)
    err = capsys.readouterr().err
    plain = ANSI.sub("", err)
    assert "wallpaper_sdxl: sd_xl_base_1.0 · 1 pass, 30 steps" in plain
    assert re.search(r"▶ sample \d+/30", plain)
    assert "✓ sample  ✓ decode  ▶ upscale" in plain
    assert "[1/2] seed 5 done" in plain and "[2/2] seed 6 done" in plain
    assert "peak memory: VRAM 4.0/16.0 GiB (25%), RAM 4.0/24.0 GiB (17%)" in plain
    assert err.count("\x1b[?25l") == 1 and err.count("\x1b[?25h") == 1
    assert err.endswith("\x1b[3F\x1b[J\x1b[?25hpeak memory: VRAM 4.0/16.0 GiB (25%), RAM 4.0/24.0 GiB (17%)\n")
    assert imagegen._LIVE is None
    assert len(run["images"]) == 2 and run["timing"]["sample_s"] > 0.5  # timing still recorded


def test_generate_without_a_terminal_prints_only_the_header(comfy, capsys, monkeypatch):
    monkeypatch.delenv("IMAGEGEN_PROGRESS", raising=False)
    imagegen.generate("a misty fjord", "draft", count=1, seed=5)
    err = capsys.readouterr().err
    assert "\x1b[" not in err
    assert "draft: sd_xl_base_1.0 · 1 pass, 25 steps · 1024x1024 · 1 image" in err
    assert ("GET", "/system_stats") in comfy.requests  # wait_ready only
    assert comfy.requests.count(("GET", "/system_stats")) == 1


def test_no_block_without_the_websocket(comfy, capsys, monkeypatch):
    monkeypatch.setenv("IMAGEGEN_PROGRESS", "1")
    comfy.ws = False
    imagegen.generate("x", "draft", seed=1)
    err = capsys.readouterr().err
    assert "\x1b[" not in err and "draft: sd_xl_base_1.0" in err


def test_a_broken_history_does_not_stop_the_run(comfy, capsys, monkeypatch):
    monkeypatch.setenv("IMAGEGEN_PROGRESS", "1")
    monkeypatch.setattr(progress, "history_estimate", lambda *a: 1 / 0)
    run = imagegen.generate("x", "draft", seed=1)
    assert len(run["images"]) == 1
    assert "note: no live progress (ZeroDivisionError" in capsys.readouterr().err


def test_generate_stops_the_block_when_comfy_rejects(comfy, capsys, monkeypatch):
    monkeypatch.setenv("IMAGEGEN_PROGRESS", "1")
    comfy.reject = "bad"
    with pytest.raises(RuntimeError):
        imagegen.generate("x", "draft", seed=1)
    assert imagegen._LIVE is None
    assert "\x1b[?25h" in capsys.readouterr().err


def test_vary_header_has_source_size(comfy, capsys, make_image, monkeypatch):
    src = make_image(size=(1600, 900))
    imagegen.generate("x", "draft", seed=1, source_image=str(src))
    assert "draft img2img: sd_xl_base_1.0 · 1 pass, 25 steps" in capsys.readouterr().err


def test_a_broken_listener_does_not_lose_timing(comfy, monkeypatch):
    real = imagegen.Events.__init__

    def init(self, *a, **k):
        real(self, *a, **k)
        self.listeners.append(lambda *x: 1 / 0)
    monkeypatch.setattr(imagegen.Events, "__init__", init)
    comfy.node_s = {"KSampler": 0.1}
    run = imagegen.generate("x", "draft", seed=1)
    assert run["timing"]["sample_s"] >= 0.1


def test_forgotten_job_is_not_a_finished_image():
    pr = make_progress(count=1)
    play(pr, "a", 0.0, "1", "2", "3", "4", "5")
    pr.on_event("a", "execution_success", {}, 5.0)
    assert pr._finished() == 1
    pr.forget("a")  # came out black: rendered again as "b"
    play(pr, "b", 6.0, "3")
    assert pr._finished() == 0 and "image 1/1" in text(pr, 7.0) and "saving" not in text(pr, 7.0)
