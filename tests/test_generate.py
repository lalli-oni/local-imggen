"""generate() / vary() end to end against the fake ComfyUI."""
import io
import json
from pathlib import Path

import pytest
from PIL import Image

import imagegen
from fake_comfy import png_bytes


def _node(wf, class_type):
    return next(n["inputs"] for n in wf.values() if n["class_type"] == class_type)


def test_txt2img_writes_run_folder(comfy, out_dir):
    run = imagegen.generate("a cozy cabin", "draft", count=3, seed=10)
    run_dir = Path(run["run_dir"])
    assert run_dir.parent.parent == out_dir and run_dir.name.endswith("-draft-a-cozy-cabin")
    assert [im["seed"] for im in run["images"]] == [10, 11, 12]
    seeds = [_node(wf, "KSampler")["seed"] for wf in comfy.workflows()]
    assert seeds == [10, 11, 12]
    for im in run["images"]:
        p = Path(im["path"])
        assert Image.open(p).size == (64, 48)
        meta = json.loads(p.with_suffix(".json").read_text())
        assert meta["seed"] == im["seed"] and meta["preset"] == "draft" and meta["file"] == p.name
        assert meta["params"]["width"] == 1024 and "prompt" not in meta["params"]
    saved = json.loads((run_dir / "run.json").read_text())
    assert saved["count"] == 3 and saved["transparent"] is False
    assert (run_dir / "index.html").is_file()
    # only "output" images are fetched, not ComfyUI's temp previews
    assert len(list(run_dir.glob("*.png"))) == 3


def test_prompt_suffix_negative_and_overrides(comfy):
    imagegen.generate("fjord", "wallpaper_sdxl", seed=1, negative="people", width=512, height=256, steps=5, cfg=3.0,
                      styles=[])  # the preset's house style: test_styles.py
    wf = comfy.workflows()[0]
    texts = [n["inputs"]["text"] for n in wf.values() if n["class_type"] == "CLIPTextEncode"]
    preset = imagegen.list_presets()["wallpaper_sdxl"]
    assert "fjord" + preset["suffix"] in texts
    assert "people" in texts  # user negative replaces default_negative
    latent = _node(wf, "EmptyLatentImage")
    assert (latent["width"], latent["height"]) == (512, 256)
    ks = _node(wf, "KSampler")
    assert (ks["steps"], ks["cfg"]) == (5, 3.0)


def test_default_negative_when_none_given(comfy):
    imagegen.generate("x", "icon", seed=1)
    conf = imagegen.load_config()
    texts = [n["inputs"]["text"] for n in comfy.workflows()[0].values() if n["class_type"] == "CLIPTextEncode"]
    assert conf["default_negative"] + conf["presets"]["icon"]["negative_extra"] in texts


def test_wrapped_seed(comfy):
    run = imagegen.generate("x", count=2, seed=imagegen.MAX_SEED - 1)
    assert [im["seed"] for im in run["images"]] == [imagegen.MAX_SEED - 1, 0]


def test_icon_runs_remove_bg(comfy, fake_rembg):
    run = imagegen.generate("rocket", "icon", count=2, seed=5)
    assert len(fake_rembg) == 2
    for im in run["images"]:
        assert im["path"].endswith("-transparent.png")
        assert Image.open(im["path"]).mode == "RGBA"
    assert run["transparent"] is True


def test_windows_paths_via_wslpath():
    assert imagegen.to_windows_path("/mnt/c") == "C:\\"
    assert imagegen.to_linux_path("\\\\wsl.localhost\\Ubuntu-24.04\\tmp\\a b.png") == Path("/tmp/a b.png")
    assert imagegen.to_linux_path("\\\\wsl$\\Ubuntu-24.04\\tmp") == Path("/tmp")
    assert imagegen.to_windows_path("/mnt/c/Users/lalli/x.png") == "C:\\Users\\lalli\\x.png"
    assert imagegen.to_linux_path("C:\\Users\\lalli\\x.png") == Path("/mnt/c/Users/lalli/x.png")


def test_img2img_uploads_source_and_saves_it(comfy, make_image):
    src = make_image("photo.png", size=(1600, 900))
    run = imagegen.generate("x", "draft", count=1, seed=3, source_image=str(src), denoise=0.3)
    wf = comfy.workflows()[0]
    assert _node(wf, "LoadImage")["image"] in comfy.uploads
    assert _node(wf, "KSampler")["denoise"] == 0.3
    uploaded = Image.open(Path(run["run_dir"]) / imagegen.SHEET_SOURCE)
    w, h = uploaded.size
    assert w % 64 == 0 and h % 64 == 0 and abs(w / h - 16 / 9) < 0.1
    assert run["source_image"] == str(src) and run["denoise"] == 0.3
    meta = json.loads(Path(run["images"][0]["path"]).with_suffix(".json").read_text())
    assert meta["source_image"] == str(src)


def test_same_source_gets_same_upload_name(comfy, make_image):
    src = make_image()
    imagegen.generate("x", seed=1, source_image=str(src))
    imagegen.generate("x", seed=1, source_image=str(src))
    assert len(comfy.uploads) == 1


def test_inpaint_uploads_mask(comfy, make_image):
    src = make_image()
    mask = make_image("mask.png", color=(255, 255, 255))
    run = imagegen.generate("x", seed=1, source_image=str(src), mask=str(mask))
    wf = comfy.workflows()[0]
    assert _node(wf, "LoadImageMask")["image"] in comfy.uploads
    assert _node(wf, "KSampler")["denoise"] == 0.85  # inpaint default
    assert run["mask"] == str(mask)


def test_transparent_source_is_flattened_on_white(make_image):
    src = make_image("t.png", color=(0, 0, 0, 0), mode="RGBA")
    data, _, _ = imagegen.prepare_source(src)
    import io
    assert Image.open(io.BytesIO(data)).convert("RGB").getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("kwargs, err", [
    ({"preset": "nope"}, "unknown preset"),
    ({"mask": "/x.png"}, "needs a source"),
    ({"source_image": "/does/not/exist.png"}, None),
])
def test_bad_arguments(comfy, kwargs, err):
    exc = ValueError if err else FileNotFoundError
    with pytest.raises(exc, match=err):
        imagegen.generate("x", **kwargs)
    assert not comfy.prompts


def test_bad_denoise(comfy, make_image):
    with pytest.raises(ValueError, match="denoise"):
        imagegen.generate("x", source_image=str(make_image()), denoise=1.5)


def test_comfy_rejects_workflow(comfy):
    comfy.reject = "invalid prompt: node 4 missing"
    with pytest.raises(RuntimeError, match="node 4 missing"):
        imagegen.generate("x", seed=1)


def test_comfy_job_error(comfy):
    comfy.fail_jobs = True
    with pytest.raises(RuntimeError, match="ComfyUI failed"):
        imagegen.generate("x", seed=1)


def test_comfy_unreachable_gives_hint(monkeypatch):
    c = imagegen.Comfy("http://127.0.0.1:9")
    with pytest.raises(RuntimeError, match="systemctl status comfyui"):
        c.wait_ready(timeout=0)


def test_vary_reuses_sidecar(comfy, fake_rembg):
    first = imagegen.generate("a red fox", "icon", seed=1, negative="dogs")
    img = first["images"][0]["path"]  # the -transparent.png: its sidecar is found via the raw name
    run = imagegen.vary(img, count=2, strength=0.5)
    assert run["prompt"] == "a red fox" and run["preset"] == "icon"
    assert Path(run["run_dir"]).name.endswith("-vary-01-seed1-transparent")
    texts = [n["inputs"]["text"] for n in comfy.workflows()[-1].values() if n["class_type"] == "CLIPTextEncode"]
    assert any(t.startswith("dogs") for t in texts)


def test_vary_outside_image_needs_prompt(comfy, make_image):
    with pytest.raises(ValueError, match="pass a prompt"):
        imagegen.vary(str(make_image()))
    run = imagegen.vary(str(make_image()), count=1, prompt="a cat")
    assert run["preset"] == "draft"


def test_wallpapers_use_qwen(comfy):
    """#51: the bake-off winner draws the wallpapers, at an exact 16:9 / 21:9 before the 4x upscale."""
    for preset, size, target in (("wallpaper", (1536, 864), (3840, 2160)),
                                 ("wallpaper_ultrawide", (1792, 768), (3440, 1440))):
        imagegen.generate("a fjord at dawn", preset, seed=1)
        wf = comfy.workflows()[-1]
        assert _node(wf, "UNETLoader")["unet_name"] == "qwen_image_2.1_bf16.safetensors"
        assert _node(wf, "TextEncodeQwenImage21")["prompt"].startswith("a fjord at dawn, ")
        latent = _node(wf, "EmptyLatentImage")
        assert (latent["width"], latent["height"]) == size
        assert (_node(wf, "ImageScale")["width"], _node(wf, "ImageScale")["height"]) == target


def test_is_blank():
    assert imagegen.is_blank(png_bytes((0, 0, 0)))
    assert imagegen.is_blank(png_bytes((8, 8, 8)))
    assert not imagegen.is_blank(png_bytes((9, 0, 0)))
    assert not imagegen.is_blank(png_bytes((255, 255, 255)))  # a plain white image is not a NaN render
    assert not imagegen.is_blank(b"not a png")
    img = Image.new("RGB", (64, 48))
    img.putpixel((3, 3), (200, 10, 10))  # a dark scene with one light is not blank
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert not imagegen.is_blank(buf.getvalue())


def test_blank_image_is_rendered_again(comfy, capsys):
    comfy.blank_jobs = 1  # the first job comes out black, as Qwen did under memory pressure
    run = imagegen.generate("x", count=2, seed=1)
    wfs = comfy.workflows()
    assert len(wfs) == 3
    assert [_node(wf, "KSampler")["seed"] for wf in wfs] == [1, 2, 1]  # the same seed again
    assert _node(wfs[2], "KSampler")["imagegen_retry"] == 1  # else ComfyUI's cache hands back the black image
    assert "imagegen_retry" not in _node(wfs[0], "KSampler")
    assert comfy.front == [comfy.order[2]]  # ahead of the run's other waiting jobs
    assert [im["seed"] for im in run["images"]] == [1, 2]
    for im in run["images"]:
        assert "blank" not in im and not imagegen.is_blank(Path(im["path"]).read_bytes())
        assert "blank" not in json.loads(Path(im["path"]).with_suffix(".json").read_text())
    assert "seed 1 came out blank (black), rendering it again" in capsys.readouterr().err


def test_still_blank_is_kept_and_marked(comfy, capsys):
    comfy.blank_jobs = 5
    run = imagegen.generate("x", seed=1)
    assert len(comfy.workflows()) == 1 + imagegen.BLANK_RETRIES
    [im] = run["images"]
    assert im["blank"] is True
    assert json.loads(Path(im["path"]).with_suffix(".json").read_text())["blank"] is True
    assert "still blank" in capsys.readouterr().err


def test_stop_during_the_rerender_cancels_it(comfy, monkeypatch):
    comfy.blank_jobs = 1
    real_wait = imagegen.Comfy.wait_entry
    waits = []

    def wait(self, pid, **kw):
        waits.append(pid)
        if len(waits) == 2:  # waiting on the re-render
            raise KeyboardInterrupt
        return real_wait(self, pid, **kw)
    monkeypatch.setattr(imagegen.Comfy, "wait_entry", wait)
    with pytest.raises(KeyboardInterrupt):
        imagegen.generate("x", seed=1)
    assert waits[1] != waits[0] and comfy.deleted == [waits[1]]  # the re-render, not the finished first try


def test_failed_rerender_keeps_the_run(comfy, monkeypatch, capsys):
    comfy.blank_jobs = 1
    real_queue = imagegen.Comfy.queue

    def queue(self, wf, front=False):
        if front:
            raise RuntimeError("ComfyUI rejected the workflow: boom")
        return real_queue(self, wf)
    monkeypatch.setattr(imagegen.Comfy, "queue", queue)
    run = imagegen.generate("x", count=2, seed=1)
    assert [im.get("blank", False) for im in run["images"]] == [True, False]
    assert "the re-render failed (RuntimeError" in capsys.readouterr().err


def test_wallpaper_flag_skips_blank_images(comfy, monkeypatch):
    comfy.blank_jobs = 3  # both first tries and the re-render of seed 1 come out black; seed 2's re-render is fine
    set_to = []
    monkeypatch.setattr(imagegen, "set_wallpaper", lambda p: set_to.append(p) or p)
    assert imagegen.main(["x", "--count", "2", "--seed", "1", "--wallpaper"]) == 0
    assert [Path(p).name for p in set_to] == ["02-seed2.png"]
    comfy.blank_jobs = 99
    assert imagegen.main(["x", "--seed", "1", "--wallpaper"]) == 1
    assert len(set_to) == 1


# ---------------------------------------------------------------- scene + cast (#52)

def test_cast_preset_runs_scene_then_cast_in_one_job(comfy, make_image, out_dir):
    tulu, cat = make_image("tulu.png", (600, 800)), make_image("cat.png", (512, 512))
    run = imagegen.generate("a taco truck on the moon", "wallpaper_cast", count=2, seed=5, refs=[str(tulu), str(cat)])
    wfs = comfy.workflows()
    assert len(wfs) == 2  # one job per image: both passes in one graph, the model loaded once
    wf = wfs[0]
    scene_enc, cast_enc = (n["inputs"] for n in wf.values() if n["class_type"] == "TextEncodeQwenImage21")
    assert scene_enc["prompt"].startswith("a taco truck on the moon, wallpaper")
    assert cast_enc["prompt"].startswith("Put the characters from images 2 and 3 into the scene from image 1")
    assert wf[cast_enc["images.image_1"][0]]["class_type"] == "VAEDecode"
    ups = [wf[cast_enc[f"images.image_{k}"][0]]["inputs"]["image"] for k in (2, 3)]
    assert len(set(ups)) == 2 and set(ups) == set(comfy.uploads)
    assert Image.open(io.BytesIO(comfy.uploads[ups[0]])).size == imagegen.kontext_size(600, 800)
    samplers = [n["inputs"] for n in wf.values() if n["class_type"] == "KSampler"]
    assert [s["seed"] for s in samplers] == [5, imagegen.cast_seed(5)] and samplers[1]["steps"] == 25
    rd = Path(run["run_dir"])
    assert {p.name for p in rd.glob("*.png")} == {"01-seed5.png", "01-seed5-scene.png", "02-seed6.png",
                                                  "02-seed6-scene.png", "ref-1.png", "ref-2.png"}
    assert [Path(im["path"]).name for im in run["images"]] == ["01-seed5.png", "02-seed6.png"]
    meta = json.loads((rd / "01-seed5.json").read_text())
    assert meta["scene"] == "01-seed5-scene.png" and meta["refs"][1]["path"] == str(cat)
    assert meta["cast_prompt"] == cast_enc["prompt"]
    from fake_comfy import SCENE_COLOR
    assert Image.open(rd / "01-seed5-scene.png").getpixel((0, 0)) == SCENE_COLOR
    sheet = (rd / "index.html").read_text()
    assert '"caption": "ref 1 · tulu.png"' in sheet and '"scene": "01-seed5-scene.png"' in sheet


def test_cast_prompt_override_and_errors(comfy, make_image):
    ref = str(make_image("tulu.png"))
    imagegen.generate("a beach", "wallpaper_cast", refs=[ref], cast_prompt="Swap the crab for the one in {images}.")
    cast_enc = [n["inputs"] for n in comfy.workflows()[0].values() if n["class_type"] == "TextEncodeQwenImage21"][1]
    assert cast_enc["prompt"] == "Swap the crab for the one in image 2."
    assert "images.image_3" not in cast_enc
    with pytest.raises(ValueError, match="give it ref images"):
        imagegen.generate("a beach", "wallpaper_cast")
    with pytest.raises(ValueError, match="takes no refs; cast presets: wallpaper_cast"):
        imagegen.generate("a beach", "draft", refs=[ref])
    with pytest.raises(FileNotFoundError):
        imagegen.generate("a beach", "wallpaper_cast", refs=["/no/such.png"])
    with pytest.raises(ValueError, match="at most 15"):
        imagegen.generate("a beach", "wallpaper_cast", refs=[ref] * 16)
    with pytest.raises(ValueError, match=r"has no img2img \(gen vary\)"):
        imagegen.generate("a beach", "wallpaper_cast", refs=[ref], source_image=ref)
    assert len(comfy.workflows()) == 1


def test_cast_run_blank_retry_keeps_the_scene(comfy, make_image):
    comfy.blank_jobs = 1
    run = imagegen.generate("a beach", "wallpaper_cast", refs=[str(make_image("t.png"))], seed=3)
    assert len(comfy.workflows()) == 2 and not run["images"][0].get("blank")
    assert Path(run["images"][0]["scene"]).is_file()


def test_expand_refs_clones_the_loader():
    tmpl = {"5": {"class_type": "LoadImage", "inputs": {"image": "{{ref}}"}},
            "6": {"class_type": "Enc", "inputs": {"images.image_1": ["4", 0], "images.image_2": ["5", 0]}}}
    wf = imagegen.expand_refs(tmpl, 3)
    assert wf["6"]["inputs"] == {"images.image_1": ["4", 0], "images.image_2": ["5", 0],
                                 "images.image_3": ["502", 0], "images.image_4": ["503", 0]}
    assert [wf[k]["inputs"]["image"] for k in ("5", "502", "503")] == ["{{ref_1}}", "{{ref_2}}", "{{ref_3}}"]
    assert tmpl["5"]["inputs"]["image"] == "{{ref}}"  # template untouched
    with pytest.raises(ValueError, match="image_3 is taken"):
        imagegen.expand_refs({**tmpl, "6": {"class_type": "Enc", "inputs": {"images.image_2": ["5", 0],
                                                                            "images.image_3": ["4", 0]}}}, 2)
    assert imagegen.image_list(1) == "image 2" and imagegen.image_list(3) == "images 2, 3 and 4"


def test_gen_cli_ref_flags(comfy, make_image):
    ref = str(make_image("t.png"))
    assert imagegen.main(["a beach", "-p", "wallpaper_cast", "--ref", ref, "--ref", ref, "--cast-prompt", "x {images}"]) == 0
    cast_enc = [n["inputs"] for n in comfy.workflows()[0].values() if n["class_type"] == "TextEncodeQwenImage21"][1]
    assert cast_enc["prompt"] == "x images 2 and 3"


def test_cast_blank_retry_redraws_only_the_edit(comfy, make_image):
    comfy.blank_jobs = 1
    imagegen.generate("a beach", "wallpaper_cast", refs=[str(make_image("t.png"))], seed=3)
    retry = comfy.workflows()[1]
    samplers = [(nid, "imagegen_retry" in n["inputs"]) for nid, n in retry.items() if n["class_type"] == "KSampler"]
    assert dict(samplers) == {"7": False, "13": True}  # the scene (7) comes from ComfyUI's cache


def test_sheet_lists_a_shared_scene_once(comfy, make_image):
    comfy.outputs_per_job = 2
    run = imagegen.generate("a beach", "wallpaper_cast", refs=[str(make_image("t.png"))], seed=3)
    assert len(run["images"]) == 2 and run["images"][0]["scene"] == run["images"][1]["scene"]
    assert (Path(run["run_dir"]) / "index.html").read_text().count('"caption": "scene · seed 3"') == 1


def test_cast_edit_reruns_only_the_cast_pass(comfy, make_image):
    """gen edit SCENE "..." -p wallpaper_cast --ref: the scene as image 1 at 16:9, the ref as image 2, no upscale."""
    scene, ref = make_image("scene.png", (1536, 864)), make_image("tulu.png", (600, 800))
    assert imagegen.main(["edit", str(scene), "Replace the creature with the character from {images}.",
                          "-p", "wallpaper_cast", "--ref", str(ref), "--seed", "4"]) == 0
    [wf] = comfy.workflows()
    enc = _node(wf, "TextEncodeQwenImage21")
    assert enc["prompt"] == "Replace the creature with the character from image 2."  # no suffix, no style
    up1, up2 = (wf[enc[f"images.image_{k}"][0]]["inputs"]["image"] for k in (1, 2))
    assert Image.open(io.BytesIO(comfy.uploads[up1])).size == (1536, 864)
    assert up2 in comfy.uploads and up1 != up2
    assert not any(n["class_type"] == "ImageUpscaleWithModel" for n in wf.values())
    [run] = imagegen.recent_runs(1)
    meta = json.loads((Path(run["run_dir"]) / "run.json").read_text())
    assert meta["source_image"] == str(scene) and meta["refs"][0]["path"] == str(ref) and meta["edit"] is True
    with pytest.raises(ValueError, match="give it ref images"):
        imagegen.edit(str(scene), "x", preset="wallpaper_cast")


def test_cast_pass_has_its_own_qwen_cache():
    # #52: with the scene's QwenImage21Cache (a KV cache) shared, the cast pass gave back the scene unchanged
    wf = json.loads((imagegen.ROOT / "workflows" / "qwen_image_21_scene_cast_upscale.json").read_text())
    scene, cast = wf["7"]["inputs"]["model"][0], wf["13"]["inputs"]["model"][0]
    assert scene != cast and wf[cast]["class_type"] == wf[scene]["class_type"] == "QwenImage21Cache"


def test_cast_pass_does_not_reuse_the_scene_seed():
    # #52: same seed + same latent size = the scene's noise, and the edit gave the scene back unchanged
    wf = json.loads((imagegen.ROOT / "workflows" / "qwen_image_21_scene_cast_upscale.json").read_text())
    assert wf["7"]["inputs"]["seed"] == "{{seed}}" and wf["13"]["inputs"]["seed"] == "{{cast_seed}}"
    assert imagegen.cast_seed(5) != 5
    assert 0 <= imagegen.cast_seed(imagegen.MAX_SEED - 1) < imagegen.MAX_SEED
