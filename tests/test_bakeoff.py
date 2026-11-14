"""gen bakeoff (#40 edits, #51 --t2i): same seeds per preset, models unloaded between presets, peak memory sampling,
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
    assert out.splitlines()[0].split()[:4] == ["preset", "images", "size", "first"]
    assert "kontext" in out and "klein" in out and "contact sheet:" in out and "bakeoff.json" in out
    comfy.fail_preset = "kontext"
    assert imagegen.main(["bakeoff", str(make_image()), "x", "-p", "kontext", "-n", "1"]) == 1


def test_table_handles_missing_numbers():
    run = {"bakeoff": {"presets": [{"preset": "qwen", "error": "boom", "peak": {}}]}}
    line = bakeoff.table(run).splitlines()[1]
    assert line.split()[:4] == ["qwen", "0", "-", "-"] and line.endswith("boom")


# ---------------------------------------------------------------- text-to-image (#51)

def _ckpt(wf):
    n = _node(wf, "CheckpointLoaderSimple")
    return n["ckpt_name"] if n else None


def _latent_size(wf):
    n = _node(wf, "EmptyLatentImage") or _node(wf, "EmptyFlux2LatentImage")
    return n["width"], n["height"]


def test_t2i_runs_every_prompt_through_every_model_at_one_size(comfy, fake_proc):
    run = bakeoff.t2i_bakeoff(["a cosy window nook", "a tide pool"], count=2, seed=7, interval=0.01)
    wfs = comfy.workflows()
    assert len(wfs) == 4 * 2 * 2 and comfy.frees == 4
    # default order: SDXL base, the SDXL finetune, klein, qwen; preset-major in ComfyUI
    models = [_ckpt(wf) or _unet(wf) for wf in wfs[::4]]
    assert models == ["sd_xl_base_1.0.safetensors", "DreamShaperXL_Lightning.safetensors",
                      "flux-2-klein-4b-fp8.safetensors", "qwen_image_2.1_bf16.safetensors"]
    assert {_latent_size(wf) for wf in wfs} == {(1344, 768)}
    assert [_seed(wf) for wf in wfs] == [7, 8] * 8
    assert _node(wfs[4], "KSampler")["sampler_name"] == "dpmpp_sde"  # Lightning's sampler
    assert _node(wfs[4], "KSampler")["steps"] == 6 and _node(wfs[4], "KSampler")["cfg"] == 2.0
    assert all(_node(wf, "LoadImage") is None for wf in wfs)

    out = Path(run["run_dir"])
    assert "-bakeoff-t2i-a-cosy-window-nook" in out.name
    assert [im["tag"] for im in run["images"]] == \
        [f"{p} · {n}" for n in (1, 2) for p in bakeoff.T2I_PRESETS for _ in range(2)]
    stats = json.loads((out / "bakeoff.json").read_text())
    assert stats["mode"] == "t2i" and stats["size"] == "1344x768" and stats["ref"] is None
    assert stats["instructions"] == ["a cosy window nook", "a tide pool"]
    assert [r["preset"] for r in stats["presets"]] == list(bakeoff.T2I_PRESETS)
    assert all(r["size"] == "1344x768" and r["images"] == 4 and r["error"] is None for r in stats["presets"])
    assert run["source_image"] is None and not run["edit"] and not (out / imagegen.SHEET_SOURCE).exists()
    html = (out / "index.html").read_text()
    assert '"tag": "dreamshaper · 2"' in html and '"size": "1344x768"' in html


def test_t2i_with_ref_adds_ref_columns_where_the_model_can_edit(comfy, make_image, fake_proc):
    ref = make_image(size=(512, 512))
    run = bakeoff.t2i_bakeoff(["reading in a window nook"], ["draft", "klein", "qwen"], ref=str(ref), count=1,
                              seed=3, size=(1536, 864), interval=0.01)
    res = {r["preset"]: r for r in run["bakeoff"]["presets"]}
    assert list(res) == ["draft", "draft+ref", "klein", "klein+ref", "qwen", "qwen+ref"]
    assert res["draft+ref"]["note"] == bakeoff.NO_REF and res["draft+ref"]["images"] == 0
    assert res["draft+ref"]["error"] is None
    assert comfy.frees == 3  # once per model: the +ref column runs with the model still loaded

    wfs = comfy.workflows()
    assert len(wfs) == 5
    text_wfs, ref_wfs = [wfs[0], wfs[1], wfs[3]], [wfs[2], wfs[4]]
    assert all(_node(wf, "LoadImage") is None for wf in text_wfs)
    assert {_latent_size(wf) for wf in text_wfs} == {(1536, 864)}
    for wf in ref_wfs:
        assert _node(wf, "LoadImage") is not None
    klein_ref = _node(wfs[2], "CLIPTextEncode")["text"]
    assert klein_ref.startswith("reading in a window nook") and klein_ref.endswith(bakeoff.REF_SUFFIX)
    assert res["klein+ref"]["size"] == "1024x1024"  # an edit's size follows the (square) ref
    assert [_seed(wf) for wf in wfs] == [3] * 5

    assert [im["tag"] for im in run["images"]] == ["draft · 1", "klein · 1", "klein+ref · 1", "qwen · 1",
                                                   "qwen+ref · 1"]
    assert any("klein+ref" in Path(im["path"]).name for im in run["images"])
    out = Path(run["run_dir"])
    assert "-bakeoff-t2i-ref-src" in out.name and (out / imagegen.SHEET_SOURCE).is_file()
    assert run["bakeoff"]["ref"] == str(ref) and run["bakeoff"]["ref_suffix"] == bakeoff.REF_SUFFIX
    assert run["preset"] == "bakeoff: draft vs klein vs klein+ref vs qwen vs qwen+ref"
    table = bakeoff.table(run)
    assert "draft+ref" in table and "no ref input" in table
    html = (out / "index.html").read_text()
    assert '"note": "no ref input' in html


def test_t2i_failing_model_and_bad_args(comfy, make_image, fake_proc):
    comfy.fail_preset = "DreamShaper"  # the finetune not downloaded yet
    run = bakeoff.t2i_bakeoff(["x"], ["dreamshaper", "klein"], count=1, seed=1, interval=0.01)
    ds, klein = run["bakeoff"]["presets"]
    assert "rejected" in ds["error"] and ds["images"] == 0 and klein["images"] == 1
    with pytest.raises(ValueError, match="not a text-to-image preset: kontext"):
        bakeoff.t2i_bakeoff(["x"], ["klein", "kontext"])  # edit-only: fine with a ref, not without
    with pytest.raises(ValueError, match="twice"):
        bakeoff.t2i_bakeoff(["x"], ["klein", "klein"])
    with pytest.raises(ValueError, match="multiple of 16"):
        bakeoff.t2i_bakeoff(["x"], ["klein"], size=(1000, 770))
    with pytest.raises(ValueError, match="prompt"):
        bakeoff.t2i_bakeoff([" "], ["klein"])
    with pytest.raises(FileNotFoundError):
        bakeoff.t2i_bakeoff(["x"], ["klein"], ref=str(make_image()) + ".missing")


def test_t2i_cli(comfy, make_image, fake_proc, capsys):
    code = imagegen.main(["bakeoff", "--t2i", "a tide pool", "a lighthouse", "-p", "draft,klein", "-n", "1",
                          "--size", "1024x576", "--seed", "5"])
    out = capsys.readouterr().out
    assert code == 0
    assert "draft" in out and "klein" in out and "1024x576" in out and "contact sheet:" in out
    assert len(comfy.workflows()) == 4
    comfy.fail_preset = "klein"
    assert imagegen.main(["bakeoff", "--t2i", "x", "-p", "klein", "-n", "1", "--ref", str(make_image())]) == 1
    for bad in (["bakeoff", str(make_image()), "x", "--ref", "y.png"],  # --ref without --t2i
                ["bakeoff", str(make_image()), "x", "--size", "1344x768"],  # --size without --t2i, even the default
                ["bakeoff", str(make_image())],                          # edit mode without instructions
                ["bakeoff", "--t2i", "x", "--size", "big"]):
        with pytest.raises(SystemExit):
            imagegen.main(bad)


def test_t2i_ref_takes_an_edit_only_model(comfy, make_image, fake_proc):
    run = bakeoff.t2i_bakeoff(["x"], ["klein", "kontext"], ref=str(make_image()), count=1, seed=1, interval=0.01)
    res = {r["preset"]: r for r in run["bakeoff"]["presets"]}
    assert list(res) == ["klein", "klein+ref", "kontext", "kontext+ref"]
    assert res["kontext"]["note"] == bakeoff.NO_TEXT and res["kontext+ref"]["images"] == 1
    assert [_unet(wf) for wf in comfy.workflows()][-1] == "flux1-dev-kontext_fp8_scaled.safetensors"
    assert run["edit"] is False and run["source_image"]
    # file names keep the column name, so "klein+ref" can't collide with a preset called klein-ref
    assert any(Path(im["path"]).name.startswith("01-klein+ref-") for im in run["images"])
