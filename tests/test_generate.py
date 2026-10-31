"""generate() / vary() end to end against the fake ComfyUI."""
import json
from pathlib import Path

import pytest
from PIL import Image

import imagegen


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
    imagegen.generate("fjord", "wallpaper", seed=1, negative="people", width=512, height=256, steps=5, cfg=3.0)
    wf = comfy.workflows()[0]
    texts = [n["inputs"]["text"] for n in wf.values() if n["class_type"] == "CLIPTextEncode"]
    preset = imagegen.list_presets()["wallpaper"]
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
