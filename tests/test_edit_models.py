"""FLUX.2 klein and Qwen-Image-2.1 presets (#40): workflow filling for text-to-image and edit,
no img2img for unet presets, overrides a workflow can't use, doctor's opt-in handling."""
import io

import pytest
from PIL import Image

import doctor
import imagegen


def _node(wf, class_type):
    return next(n["inputs"] for n in wf.values() if n["class_type"] == class_type)


def test_klein_edit_fills_workflow(comfy, make_image):
    run = imagegen.edit(str(make_image(size=(1920, 1080))), "make it wave", count=2, preset="klein", seed=5)
    wfs = comfy.workflows()
    assert [_node(wf, "RandomNoise")["noise_seed"] for wf in wfs] == [5, 6]
    wf = wfs[0]
    assert _node(wf, "UNETLoader")["unet_name"] == "flux-2-klein-4b-fp8.safetensors"
    assert _node(wf, "CLIPLoader") == {"clip_name": "qwen_3_4b.safetensors", "type": "flux2", "device": "default"}
    assert _node(wf, "CLIPTextEncode")["text"] == "make it wave"
    assert _node(wf, "Flux2Scheduler") == {"steps": 4, "width": 1392, "height": 752}
    assert _node(wf, "EmptyFlux2LatentImage")["width"] == 1392
    # the source goes in as a reference latent on both the prompt and the zeroed negative
    assert sum(n["class_type"] == "ReferenceLatent" for n in wf.values()) == 2
    uploaded = comfy.uploads[_node(wf, "LoadImage")["image"]]
    assert Image.open(io.BytesIO(uploaded)).size == (1392, 752)
    assert "-klein-edit-src-make-it-wave" in run["run_dir"]
    assert all(im["seconds"] >= 0 for im in run["images"])


def test_klein_text_to_image(comfy):
    imagegen.generate("a lighthouse", "klein", seed=1, width=1344, height=768)
    wf = comfy.workflows()[0]
    assert _node(wf, "Flux2Scheduler") == {"steps": 4, "width": 1344, "height": 768}
    assert _node(wf, "CLIPTextEncode")["text"] == "a lighthouse"
    assert not any(n["class_type"] == "CheckpointLoaderSimple" for n in wf.values())


def test_qwen_edit_fills_workflow(comfy, make_image):
    imagegen.edit(str(make_image()), "make it wave", preset="qwen", seed=3, steps=30)
    wf = comfy.workflows()[0]
    assert _node(wf, "UNETLoader") == {"unet_name": "qwen_image_2.1_bf16.safetensors", "weight_dtype": "fp8_e4m3fn"}
    assert _node(wf, "CLIPLoader")["type"] == "qwen_image"
    enc = _node(wf, "TextEncodeQwenImage21")
    assert enc["prompt"] == "make it wave" and enc["resolution"] == 1024 and enc["negative_prompt"] == ""
    assert enc["images.image_1"][0] in wf and wf[enc["images.image_1"][0]]["class_type"] == "LoadImage"
    ks = _node(wf, "KSampler")
    assert (ks["seed"], ks["steps"], ks["cfg"]) == (3, 30, 1.0)
    assert ks["latent_image"] == [next(k for k, n in wf.items() if n["class_type"] == "TextEncodeQwenImage21"), 2]


def test_qwen_text_to_image(comfy):
    imagegen.generate("a lighthouse", "qwen", seed=1, cfg=2.5, negative="fog")
    wf = comfy.workflows()[0]
    assert _node(wf, "KSampler")["cfg"] == 2.5
    assert _node(wf, "TextEncodeQwenImage21")["negative_prompt"].startswith("fog")
    assert _node(wf, "EmptyLatentImage")["width"] == 1024


def test_unet_presets_have_no_vary(comfy, make_image):
    src = str(make_image())
    for preset in ("klein", "qwen"):
        with pytest.raises(ValueError, match=f"no img2img.*-p {preset}"):
            imagegen.vary(src, prompt="x", preset=preset)
        with pytest.raises(ValueError, match="no inpaint"):
            imagegen.vary(src, prompt="x", preset=preset, mask=src)
    assert comfy.workflows() == []
    assert imagegen.mode_workflow({"workflow": "a.json"}, "img2img_workflow") == "sdxl_img2img.json"
    assert imagegen.mode_workflow({"workflow": "a.json", "unet": "u"}, "img2img_workflow") is None
    assert imagegen.mode_workflow({"workflow": "a.json", "unet": "u", "img2img_workflow": "b.json"},
                                  "img2img_workflow") == "b.json"


def test_overrides_the_workflow_cannot_use_are_errors(comfy, make_image):
    src = str(make_image())
    with pytest.raises(ValueError, match="'klein'.*does not take guidance"):
        imagegen.edit(src, "x", preset="klein", guidance=3.0)
    with pytest.raises(ValueError, match="does not take cfg"):
        imagegen.generate("x", "klein", cfg=3.0)
    assert comfy.workflows() == []


def test_cli_edit_with_preset(comfy, make_image, capsys):
    assert imagegen.main(["edit", str(make_image()), "make it wave", "-p", "qwen", "--steps", "40"]) == 0
    assert _node(comfy.workflows()[0], "KSampler")["steps"] == 40


def test_doctor_opt_in_presets(comfy):
    conf = imagegen.load_config()
    comfy.models["diffusion_models"] = ["flux1-dev-kontext_fp8_scaled.safetensors"]
    comfy.models["text_encoders"] = ["clip_l.safetensors", "t5xxl_fp8_e4m3fn_scaled.safetensors"]
    checks = {c["name"]: c for c in doctor.check_models(conf, api_up=True)}
    klein = checks["model flux-2-klein-4b-fp8.safetensors"]
    assert klein["status"] == "warn" and klein["fix"] == "opt-in download: ~/imagegen/models/flux2-klein.sh"
    qwen = checks["model qwen3vl_8b_bf16.safetensors"]
    assert qwen["status"] == "warn" and "qwen-image-2.1.sh" in qwen["fix"]
    assert checks["model sd_xl_base_1.0.safetensors"]["status"] == "ok"
    # nodes only opt-in presets use: a warning, even in their text-to-image workflows
    comfy.nodes -= {"TextEncodeQwenImage21", "Flux2Scheduler"}
    c = doctor.check_nodes(conf, comfy.url)
    assert c["status"] == "warn" and "TextEncodeQwenImage21 (in qwen_image_21_edit.json, qwen_image_21_t2i.json)" \
        in c["detail"]


def test_every_download_script_exists():
    for name, p in imagegen.list_presets().items():
        if "download" in p:
            assert (imagegen.ROOT / "models" / p["download"]).is_file(), name

