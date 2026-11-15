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
        [f"{p} · {n}" for n in (1, 2) for p in ("FLUX.1 Kontext dev · 20 steps", "FLUX.2 klein 4B · 4 steps",
                                                 "Qwen-Image 2.1 · 25 steps") for _ in range(2)]
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
    assert '"bakeoff": {' in html and '"tag": "FLUX.2 klein 4B · 4 steps · 2"' in html
    rebuilt = imagegen.rebuild_contact_sheets(str(out))  # gen sheet works on a bake-off folder too
    assert rebuilt == [out / "index.html"] and '"tag": "Qwen-Image 2.1 · 25 steps · 1"' in rebuilt[0].read_text()


def test_a_failing_preset_is_recorded_and_the_rest_still_run(comfy, make_image, fake_proc):
    comfy.fail_preset = "klein"
    run = bakeoff.bakeoff(str(make_image()), ["make it wave"], ["klein", "qwen"], count=1, seed=1, interval=0.01)
    klein, qwen = run["bakeoff"]["presets"]
    assert "ComfyUI rejected the workflow" in klein["error"] and klein["images"] == 0
    assert klein["first_seconds"] is None
    assert qwen["error"] is None and qwen["images"] == 1
    assert [im["tag"] for im in run["images"]] == ["Qwen-Image 2.1 · 25 steps · 1"]
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
    assert out.splitlines()[0].split()[:5] == ["model", "images", "size", "steps", "first"]
    assert "Kontext" in out and "klein" in out and "contact sheet:" in out and "bakeoff.json" in out
    comfy.fail_preset = "kontext"
    assert imagegen.main(["bakeoff", str(make_image()), "x", "-p", "kontext", "-n", "1"]) == 1


def test_table_handles_missing_numbers():
    run = {"bakeoff": {"presets": [{"preset": "qwen", "error": "boom", "peak": {}}]}}
    line = bakeoff.table(run).splitlines()[1]
    assert line.split()[:5] == ["qwen", "0", "-", "-", "-"] and line.endswith("boom")


# ---------------------------------------------------------------- text-to-image (#51)

def _ckpt(wf):
    n = _node(wf, "CheckpointLoaderSimple")
    return n["ckpt_name"] if n else None


def _latent_size(wf):
    n = _node(wf, "EmptyLatentImage") or _node(wf, "EmptyFlux2LatentImage")
    return n["width"], n["height"]


def _steps(wf):
    n = _node(wf, "KSampler") or _node(wf, "Flux2Scheduler")
    return n["steps"]


SDXL, DS, KLEIN, QWEN = "SDXL base 1.0", "DreamShaper XL Lightning", "FLUX.2 klein 4B", "Qwen-Image 2.1"


def test_fresh_prompts_go_through_every_model_at_one_size(comfy, fake_proc):
    run = bakeoff.t2i_bakeoff(["a misty fjord", "a tide pool"], count=2, seed=7, interval=0.01)
    wfs = comfy.workflows()
    assert len(wfs) == 4 * 2 * 2 and comfy.frees == 4
    models = [_ckpt(wf) or _unet(wf) for wf in wfs[::4]]  # preset-major in ComfyUI
    assert models == ["sd_xl_base_1.0.safetensors", "DreamShaperXL_Lightning.safetensors",
                      "flux-2-klein-4b-fp8.safetensors", "qwen_image_2.1_bf16.safetensors"]
    assert {_latent_size(wf) for wf in wfs} == {(1344, 768)}
    assert [_seed(wf) for wf in wfs] == [7, 8] * 8
    assert _node(wfs[4], "KSampler")["sampler_name"] == "dpmpp_sde"  # Lightning's sampler
    assert [_steps(wf) for wf in wfs[::4]] == [25, 6, 4, 25]
    assert all(_node(wf, "LoadImage") is None for wf in wfs)

    labels = [f"{SDXL} · 25 steps", f"{DS} · 6 steps", f"{KLEIN} · 4 steps", f"{QWEN} · 25 steps"]
    assert [im["tag"] for im in run["images"]] == [f"{lb} · {n}" for n in (1, 2) for lb in labels for _ in range(2)]
    out = Path(run["run_dir"])
    assert "-bakeoff-t2i-a-misty-fjord" in out.name
    stats = json.loads((out / "bakeoff.json").read_text())
    assert stats["mode"] == "t2i" and stats["size"] == "1344x768" and stats["ref"] is None
    assert [r["label"] for r in stats["presets"]] == labels
    assert all(r["size"] == "1344x768" and r["images"] == 4 and r["error"] is None for r in stats["presets"])
    assert [r["steps"] for r in stats["presets"]] == [25, 6, 4, 25]
    assert run["source_image"] is None and not (out / imagegen.SHEET_SOURCE).exists()
    assert bakeoff.table(run).splitlines()[1].startswith(SDXL)
    assert f'"tag": "{DS} · 6 steps · 2"' in (out / "index.html").read_text()


def test_fresh_and_ref_tests_together(comfy, make_image, fake_proc):
    """The fresh prompt runs on every model without the ref; the lil tulu prompt runs only with the ref
    (SDXL models through their ref_preset: IP-Adapter) and is never drawn from text alone."""
    ref = make_image(size=(512, 512))
    run = bakeoff.t2i_bakeoff(["a misty fjord"], ["draft", "dreamshaper", "klein", "qwen"], ref=str(ref),
                              ref_prompts=["lil tulu reading in a window nook"], count=1, seed=3,
                              steps={"klein": 16}, interval=0.01)
    res = {r["preset"]: r for r in run["bakeoff"]["presets"]}
    assert list(res) == ["draft", "draft+ref", "dreamshaper", "dreamshaper+ref", "klein", "klein+ref", "qwen",
                         "qwen+ref"]
    assert all(r["error"] is None and r["images"] == 1 for r in res.values())
    assert comfy.frees == 4  # once per model: its ref column runs with the model still loaded

    wfs = comfy.workflows()
    assert len(wfs) == 8
    fresh, with_ref = wfs[0::2], wfs[1::2]
    for wf in fresh:
        assert _node(wf, "LoadImage") is None and _latent_size(wf) == (1344, 768)
        assert "fjord" in json.dumps(wf) and "tulu" not in json.dumps(wf)
    for wf in with_ref:
        assert _node(wf, "LoadImage") is not None
        assert "tulu" in json.dumps(wf) and "fjord" not in json.dumps(wf)
    # SDXL + IP-Adapter: the same checkpoint as the fresh column, the ref steering the model
    for wf, ckpt, sampler in ((wfs[1], "sd_xl_base_1.0.safetensors", "dpmpp_2m"),
                              (wfs[3], "DreamShaperXL_Lightning.safetensors", "dpmpp_sde")):
        assert _ckpt(wf) == ckpt and _node(wf, "KSampler")["sampler_name"] == sampler
        ipa = _node(wf, "IPAdapterAdvanced")
        assert ipa["weight"] == 0.55 and ipa["end_at"] == 0.7 and ipa["model"] == ["4", 0]
        assert "white background" in json.dumps(wf["7"])  # the ref's sticker background, kept out
        assert _node(wf, "KSampler")["model"] == ["23", 0]
        assert _node(wf, "IPAdapterModelLoader")["ipadapter_file"] == "ip-adapter-plus_sdxl_vit-h.safetensors"
        assert _node(wf, "CLIPVisionLoader")["clip_name"] == "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
        assert _latent_size(wf) == (1024, 1024)  # the ref test's size: the (square) ref's, like klein's
    klein_ref = _node(wfs[5], "CLIPTextEncode")["text"]
    assert klein_ref.startswith("lil tulu reading") and klein_ref.endswith(bakeoff.REF_SUFFIX)
    assert [_steps(wf) for wf in wfs] == [25, 25, 6, 6, 16, 16, 25, 25]  # --steps klein=16 in both tests
    assert {res[k]["size"] for k in res if k.endswith("+ref")} == {"1024x1024"}
    assert res["klein"]["size"] == "1344x768"
    assert res["draft+ref"]["label"] == "SDXL base 1.0 + IP-Adapter · 25 steps · ref"
    assert res["klein+ref"]["label"] == f"{KLEIN} · 16 steps · ref"

    # prompt 1 = the fresh one, prompt 2 = the ref one
    assert [im["tag"] for im in run["images"]][:4] == [
        f"{SDXL} · 25 steps · 1", f"{DS} · 6 steps · 1", f"{KLEIN} · 16 steps · 1", f"{QWEN} · 25 steps · 1"]
    assert run["images"][4]["tag"] == "SDXL base 1.0 + IP-Adapter · 25 steps · ref · 2"
    assert run["bakeoff"]["instructions"] == ["a misty fjord", "with the ref: lil tulu reading in a window nook"]
    out = Path(run["run_dir"])
    assert (out / imagegen.SHEET_SOURCE).is_file() and run["bakeoff"]["ref"] == str(ref)
    assert any(Path(im["path"]).name.startswith("02-klein+ref-") for im in run["images"])


def test_a_model_without_a_ref_input_is_noted(comfy, make_image, fake_proc):
    run = bakeoff.t2i_bakeoff(["x"], ["icon", "klein"], ref=str(make_image()), ref_prompts=["y"], count=1,
                              interval=0.01)
    res = {r["preset"]: r for r in run["bakeoff"]["presets"]}
    assert res["icon+ref"]["note"] == bakeoff.NO_REF and res["icon+ref"]["images"] == 0
    assert res["icon+ref"]["error"] is None and "no ref input" in bakeoff.table(run)


def test_ref_test_alone_and_edit_only_models(comfy, make_image, fake_proc):
    run = bakeoff.t2i_bakeoff(None, ["klein", "kontext"], ref=str(make_image()), ref_prompts=["x"], count=1,
                              seed=1, interval=0.01)
    assert [r["preset"] for r in run["bakeoff"]["presets"]] == ["klein+ref", "kontext+ref"]
    assert all(_node(wf, "LoadImage") is not None for wf in comfy.workflows())
    with pytest.raises(ValueError, match="no ref input: icon"):
        bakeoff.t2i_bakeoff(None, ["icon", "klein"], ref=str(make_image()), ref_prompts=["x"])


def test_t2i_bad_args_and_a_failing_model(comfy, make_image, fake_proc):
    comfy.fail_preset = "DreamShaper"  # not downloaded yet
    run = bakeoff.t2i_bakeoff(["x"], ["dreamshaper", "klein"], count=1, seed=1, interval=0.01)
    ds, klein = run["bakeoff"]["presets"]
    assert "rejected" in ds["error"] and ds["images"] == 0 and klein["images"] == 1
    ref = str(make_image())
    with pytest.raises(ValueError, match="not a text-to-image preset: kontext"):
        bakeoff.t2i_bakeoff(["x"], ["klein", "kontext"])  # edit-only: only in a ref test
    with pytest.raises(ValueError, match="twice"):
        bakeoff.t2i_bakeoff(["x"], ["klein", "klein"])
    with pytest.raises(ValueError, match="multiple of 16"):
        bakeoff.t2i_bakeoff(["x"], ["klein"], size=(1000, 770))
    with pytest.raises(ValueError, match="at least one prompt"):
        bakeoff.t2i_bakeoff([], ["klein"])
    with pytest.raises(ValueError, match="go together"):
        bakeoff.t2i_bakeoff(["x"], ["klein"], ref=ref)
    with pytest.raises(ValueError, match="go together"):
        bakeoff.t2i_bakeoff(["x"], ["klein"], ref_prompts=["y"])
    with pytest.raises(ValueError, match="isn't in the bake-off: qwen"):
        bakeoff.t2i_bakeoff(["x"], ["klein"], steps={"qwen": 30})
    with pytest.raises(ValueError, match="1-150"):
        bakeoff.t2i_bakeoff(["x"], ["klein"], steps={"klein": 0})
    with pytest.raises(FileNotFoundError):
        bakeoff.t2i_bakeoff(["x"], ["klein"], ref=ref + ".missing", ref_prompts=["y"])


def test_t2i_cli(comfy, make_image, fake_proc, capsys):
    code = imagegen.main(["bakeoff", "--t2i", "a tide pool", "--presets", "draft,klein", "--count", "1",
                          "--size", "1024x576", "--seed", "5", "--steps", "klein=16",
                          "--ref", str(make_image()), "--ref-prompt", "lil tulu at the beach"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0].split()[:2] == ["model", "images"]
    assert f"{KLEIN} · 16 steps · ref" in out and "1024x576" in out and "contact sheet:" in out
    assert len(comfy.workflows()) == 4  # draft, draft+ref (IP-Adapter), klein, klein+ref
    comfy.fail_preset = "klein"
    assert imagegen.main(["bakeoff", "--t2i", "x", "--presets", "klein", "--count", "1"]) == 1
    img = str(make_image())
    for bad in (["bakeoff", img, "x", "--ref", "y.png"],                 # --ref without --t2i
                ["bakeoff", img, "x", "--size", "1344x768"],             # --size without --t2i, even the default
                ["bakeoff", img, "x", "--steps", "klein=8"],
                ["bakeoff", img],                                        # edit mode without instructions
                ["bakeoff", "--t2i"],                                    # nothing to run
                ["bakeoff", "--t2i", "x", "--ref", img],                 # a ref without a ref prompt
                ["bakeoff", "--t2i", "x", "--ref-prompt", "y"],          # a ref prompt without a ref
                ["bakeoff", "--t2i", "x", "--steps", "klein"],
                ["bakeoff", "--t2i", "x", "--size", "big"]):
        with pytest.raises(SystemExit):
            imagegen.main(bad)


def test_t2i_ref_size_of_a_preset_with_its_own_output_size(comfy, make_image, fake_proc):
    """wallpaper_ref (#3) is edit-only; its size is the saved one (target_*, after the upscale), not the ref's."""
    run = bakeoff.t2i_bakeoff(None, ["wallpaper_ref"], ref=str(make_image(size=(512, 512))), ref_prompts=["x"],
                              count=1, interval=0.01)
    r = run["bakeoff"]["presets"][0]
    assert r["images"] == 1 and r["size"] == "3840x2160" and r["label"] == f"{KLEIN} · 16 steps · ref"


def test_t2i_labels_stay_unique_and_bad_ref_preset(comfy, make_image, fake_proc, tmp_path, monkeypatch):
    run = bakeoff.t2i_bakeoff(["x"], ["wallpaper", "wallpaper_ultrawide"], count=1, steps={"wallpaper": "30"},
                              interval=0.01)
    labels = [r["label"] for r in run["bakeoff"]["presets"]]
    assert labels == ["SDXL base 1.0 · 30 steps (wallpaper)", "SDXL base 1.0 · 30 steps (wallpaper_ultrawide)"]
    assert run["bakeoff"]["steps"] == {"wallpaper": [30]}
    assert _steps(comfy.workflows()[0]) == 30  # "30" from an API caller became a number
    assert run["bakeoff"]["presets"][0]["size"] == "3840x2160"  # after the upscale, as saved

    conf = imagegen.load_config()
    conf["presets"]["draft"]["ref_preset"] = "sdxl_reff"
    monkeypatch.setattr(imagegen, "load_config", lambda: conf)
    with pytest.raises(ValueError, match="ref_preset 'sdxl_reff' is not an edit preset"):
        bakeoff.t2i_bakeoff(["x"], ["draft"], ref=str(make_image()), ref_prompts=["y"])


def test_t2i_missing_custom_node_is_a_note_not_a_comfy_error(comfy, make_image, fake_proc):
    """On tulu the IPAdapter node wasn't loaded: say so in the table instead of a raw ComfyUI rejection."""
    comfy.nodes -= {"IPAdapterModelLoader", "IPAdapterAdvanced"}
    run = bakeoff.t2i_bakeoff(["x"], ["draft", "klein"], ref=str(make_image()), ref_prompts=["y"], count=1,
                              interval=0.01)
    res = {r["preset"]: r for r in run["bakeoff"]["presets"]}
    assert "IPAdapterAdvanced, IPAdapterModelLoader" in res["draft+ref"]["note"]
    assert res["draft+ref"]["images"] == 0 and res["draft+ref"]["error"] is None
    assert res["draft"]["images"] == 1 and res["klein+ref"]["images"] == 1
    assert not any(_node(wf, "IPAdapterAdvanced") for wf in comfy.workflows())  # never sent
    assert "custom node not installed" in bakeoff.table(run)


def test_t2i_step_sweep(comfy, make_image, fake_proc):
    """Find each model's sweet spot: a column per step count, text and ref, the model loaded once."""
    run = bakeoff.t2i_bakeoff(["fox"], ["draft", "klein"], ref=str(make_image()), ref_prompts=["tulu"], count=1,
                              seed=1, steps={"klein": [8, 16, 32]}, interval=0.01)
    res = [r["preset"] for r in run["bakeoff"]["presets"]]
    assert res == ["draft", "draft+ref", "klein@8", "klein@16", "klein@32", "klein@8+ref", "klein@16+ref",
                   "klein@32+ref"]
    assert comfy.frees == 2
    wfs = comfy.workflows()
    assert [_steps(wf) for wf in wfs[2:]] == [8, 16, 32, 8, 16, 32]
    labels = {r["preset"]: r["label"] for r in run["bakeoff"]["presets"]}
    assert labels["klein@32+ref"] == f"{KLEIN} · 32 steps · ref" and labels["klein@8"] == f"{KLEIN} · 8 steps"
    assert run["bakeoff"]["steps"] == {"klein": [8, 16, 32]}
    with pytest.raises(ValueError, match="each value once"):
        bakeoff.t2i_bakeoff(["x"], ["klein"], steps={"klein": [8, 8]})


def test_t2i_step_sweep_cli(comfy, fake_proc):
    assert imagegen.main(["bakeoff", "--t2i", "x", "--presets", "klein,qwen", "--count", "1",
                          "--steps", "klein=8/16, qwen=20"]) == 0
    assert [_steps(wf) for wf in comfy.workflows()] == [8, 16, 20]


def test_t2i_ctrl_c_keeps_a_sheet_of_what_ran(comfy, make_image, fake_proc, monkeypatch):
    """#51: Þór stopped a long Qwen sweep with Ctrl-C and had no sheet. Now one is written after every column,
    and a stop marks the running column and lists the ones that didn't run."""
    real = imagegen.generate

    def gen(*a, **k):
        if k.get("steps") == 25:
            out = Path(imagegen.load_config()["output_dir"]).expanduser()
            sheets = list(out.glob("*/*-bakeoff-stop/index.html"))  # klein@15 is on the sheet already
            assert sheets and "klein@15" in (sheets[0].parent / "run.json").read_text()
            raise KeyboardInterrupt
        return real(*a, **k)
    monkeypatch.setattr(imagegen, "generate", gen)
    with pytest.raises(KeyboardInterrupt):
        bakeoff.t2i_bakeoff(["fox"], ["klein"], count=1, seed=1, steps={"klein": [15, 25, 40]}, interval=0.01,
                            label="stop")
    out = Path(imagegen.load_config()["output_dir"]).expanduser()
    run = json.loads(next(out.glob("*/*-bakeoff-stop/run.json")).read_text())
    res = {r["preset"]: r for r in run["bakeoff"]["presets"]}
    assert res["klein@15"]["images"] == 1 and res["klein@15"]["error"] is None
    assert res["klein@25"]["error"] == "interrupted (Ctrl-C)"
    assert res["klein@40"]["note"] == "not run (bake-off stopped)"
    assert run["bakeoff"]["complete"] is False and run["preset"].endswith(bakeoff.STOPPED)
    assert len(run["images"]) == 1 and (Path(run["run_dir"]) / "index.html").is_file()


def test_a_finished_bakeoff_is_marked_complete(comfy, fake_proc):
    run = bakeoff.t2i_bakeoff(["fox"], ["klein"], count=1, seed=1, interval=0.01)
    on_disk = json.loads((Path(run["run_dir"]) / "run.json").read_text())
    assert on_disk["bakeoff"]["complete"] is True and "so far" not in on_disk["preset"]


def test_rebuild_an_old_stopped_bakeoff_from_its_images(comfy, make_image, fake_proc, capsys):
    """A folder stopped before run.json was written per column: the copies and sidecars are enough."""
    run = bakeoff.t2i_bakeoff(["fox"], ["draft", "klein"], ref=str(make_image()), ref_prompts=["tulu"], count=1,
                              seed=1, steps={"klein": [8, 16]}, interval=0.01, label="old")
    d = Path(run["run_dir"])
    for f in ("run.json", "bakeoff.json", "index.html"):
        (d / f).unlink()
    assert imagegen.main(["bakeoff", "--rebuild", str(d)]) == 0
    assert "rebuilt from the files" in capsys.readouterr().out
    rebuilt = json.loads((d / "run.json").read_text())
    cols = [r["preset"] for r in rebuilt["bakeoff"]["presets"]]
    assert set(cols) == {"draft", "draft+ref", "klein@8", "klein@16", "klein@8+ref", "klein@16+ref"}
    labels = {r["preset"]: r["label"] for r in rebuilt["bakeoff"]["presets"]}
    assert labels["klein@16+ref"] == f"{KLEIN} · 16 steps · ref"
    assert rebuilt["bakeoff"]["instructions"] == ["fox", "with the ref: tulu"]
    assert rebuilt["bakeoff"]["mode"] == "t2i" and rebuilt["source_image"]
    assert len(rebuilt["images"]) == 6 and (d / "index.html").is_file()
    with pytest.raises(FileNotFoundError):
        bakeoff.recover(str(d / "nope"))
