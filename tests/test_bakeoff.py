"""gen bakeoff (#40): same seeds per preset, models unloaded between presets, peak memory sampling,
a failing preset doesn't stop the rest, one contact sheet with tags and the numbers."""
import json
from pathlib import Path

import pytest

import bakeoff
import imagegen


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch):
    monkeypatch.setattr(bakeoff, "SETTLE_STEP", 0.01)


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """A /proc with meminfo and a comfyui process (pid 4242) at the given kB values."""
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    monkeypatch.setattr(bakeoff, "PROC", proc)
    monkeypatch.setenv("FAKE_COMFYUI_PID", "4242")

    def set_(rss_kib=0, vmswap_kib=0, swap_used_kib=0):
        (proc / "meminfo").write_text(f"MemTotal: 25165824 kB\nSwapTotal:  16777216 kB\n"
                                      f"SwapFree:  {16777216 - swap_used_kib} kB\n")
        (proc / "4242" / "status").write_text(f"Name:\tpython\nVmRSS:\t{rss_kib} kB\nVmSwap:\t{vmswap_kib} kB\n")
    set_()
    return set_


def _node(wf, class_type):
    return next((n["inputs"] for n in wf.values() if n["class_type"] == class_type), None)


def _seed(wf):  # Kontext and Qwen use KSampler, klein RandomNoise
    ks = _node(wf, "KSampler")
    return ks["seed"] if ks else _node(wf, "RandomNoise")["noise_seed"]


def _unet(wf):
    return _node(wf, "UNETLoader")["unet_name"]


def test_bakeoff_runs_every_instruction_through_every_preset(comfy, make_image, fake_proc):
    fake_proc(rss_kib=8 * 1024 ** 2, vmswap_kib=1024 ** 2, swap_used_kib=2 * 1024 ** 2)
    comfy.vram_free = 4 * 1024 ** 3
    run = bakeoff.bakeoff(str(make_image()), ["make it wave", "side view"], ["kontext", "klein", "qwen"],
                          count=2, seed=10, interval=0.01)
    wfs = comfy.workflows()
    assert len(wfs) == 3 * 2 * 2
    # preset-major in ComfyUI (each model loads once), the same seeds for every preset and instruction
    assert [_unet(wf).split("-")[0].split("_")[0] for wf in wfs[::4]] == ["flux1", "flux", "qwen"]
    assert [_seed(wf) for wf in wfs] == [10, 11] * 6
    assert comfy.frees == 3

    out = Path(run["run_dir"])
    assert "-bakeoff-src" in out.name
    # instruction-major on the sheet: instruction 1 from each preset, then instruction 2
    assert [im["tag"] for im in run["images"]] == \
        [f"{p} · {n}" for n in (1, 2) for p in ("kontext", "klein", "qwen") for _ in range(2)]
    for im in run["images"]:
        assert Path(im["path"]).is_file() and Path(im["path"]).parent == out
        assert Path(im["path"]).with_suffix(".json").is_file()
    assert (out / imagegen.SHEET_SOURCE).is_file()

    assert run["count"] == 12  # images on the sheet; per instruction and preset it is 2
    stats = json.loads((out / "bakeoff.json").read_text())
    assert stats["instructions"] == ["make it wave", "side view"] and stats["count"] == 2
    k = stats["presets"][0]
    assert k["preset"] == "kontext" and k["error"] is None and k["images"] == 4
    assert k["first_seconds"] is not None and k["median_seconds"] is not None
    assert k["peak"]["vram_gib"] == 12.0 and k["peak"]["vram_total_gib"] == 16.0
    assert k["peak"]["ram_gib"] == 4.0
    assert k["peak"]["comfy_rss_gib"] == 8.0 and k["peak"]["comfy_swap_gib"] == 1.0
    assert k["peak"]["swap_gib"] == 2.0
    assert len(k["runs"]) == 2 and all(Path(r).is_dir() for r in k["runs"])

    html = (out / "index.html").read_text()
    assert '"bakeoff": {' in html and '"tag": "klein · 2"' in html
    rebuilt = imagegen.rebuild_contact_sheets(str(out))  # gen sheet works on a bake-off folder too
    assert rebuilt == [out / "index.html"] and '"tag": "qwen · 1"' in rebuilt[0].read_text()


def test_a_failing_preset_is_recorded_and_the_rest_still_run(comfy, make_image, fake_proc):
    comfy.fail_preset = "klein"
    run = bakeoff.bakeoff(str(make_image()), ["make it wave"], ["klein", "qwen"], count=1, seed=1, interval=0.01)
    klein, qwen = run["bakeoff"]["presets"]
    assert "ComfyUI rejected the workflow" in klein["error"] and klein["images"] == 0
    assert klein["first_seconds"] is None
    assert qwen["error"] is None and qwen["images"] == 1
    assert [im["tag"] for im in run["images"]] == ["qwen · 1"]
    assert "klein" in bakeoff.table(run) and "rejected" in bakeoff.table(run)


def test_no_process_and_no_free(comfy, make_image, monkeypatch, tmp_path):
    monkeypatch.setattr(bakeoff, "PROC", tmp_path / "no-proc")  # no /proc, MainPID 0: only ComfyUI's numbers
    run = bakeoff.bakeoff(str(make_image()), ["x"], ["klein"], count=1, free=False, interval=0.01)
    peak = run["bakeoff"]["presets"][0]["peak"]
    assert comfy.frees == 0
    assert set(peak) == {"vram_gib", "vram_total_gib", "ram_gib"}


def test_unload_waits_until_vram_stops_falling(comfy, monkeypatch):
    seen = []
    levels = iter([10, 6, 2, 2, 2])  # GiB free goes up as ComfyUI unloads, then holds

    def stats():
        free = next(levels, 2)
        seen.append(free)
        return {"devices": [{"vram_total": 16 * bakeoff.GIB, "vram_free": (16 - free) * bakeoff.GIB}]}
    c = imagegen.Comfy(comfy.url)
    monkeypatch.setattr(c, "system_stats", stats)
    bakeoff._unload(c)
    assert comfy.frees == 1 and seen[:4] == [10, 6, 2, 2]


def test_bad_args(comfy, make_image):
    src = str(make_image())
    with pytest.raises(ValueError, match="not an edit preset: draft"):
        bakeoff.bakeoff(src, ["x"], ["kontext", "draft"])
    with pytest.raises(ValueError, match="twice"):
        bakeoff.bakeoff(src, ["x"], ["klein", "klein"])
    with pytest.raises(ValueError, match="instruction"):
        bakeoff.bakeoff(src, ["x", " "], ["klein"])
    with pytest.raises(FileNotFoundError):
        bakeoff.bakeoff(src + ".missing", ["x"], ["klein"])
    assert comfy.workflows() == []


def test_cli(comfy, make_image, fake_proc, capsys):
    code = imagegen.main(["bakeoff", str(make_image()), "make it wave", "-p", "kontext, klein", "-n", "1",
                          "--seed", "3"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0].split()[:3] == ["preset", "images", "first"]
    assert "kontext" in out and "klein" in out and "contact sheet:" in out and "bakeoff.json" in out
    comfy.fail_preset = "kontext"
    assert imagegen.main(["bakeoff", str(make_image()), "x", "-p", "kontext", "-n", "1"]) == 1


def test_table_handles_missing_numbers():
    run = {"bakeoff": {"presets": [{"preset": "qwen", "error": "boom", "peak": {}}]}}
    line = bakeoff.table(run).splitlines()[1]
    assert line.split()[:3] == ["qwen", "0", "-"] and line.endswith("boom")
