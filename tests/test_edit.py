"""gen edit (FLUX.1 Kontext): edit-only preset, workflow filling, doctor's opt-in handling, crash handling."""
import io
import json
from pathlib import Path

import pytest
from PIL import Image

import doctor
import imagegen


def _node(wf, class_type):
    return next(n["inputs"] for n in wf.values() if n["class_type"] == class_type)


def test_edit_fills_kontext_workflow(comfy, make_image):
    src = make_image(size=(1920, 1080))
    run = imagegen.edit(str(src), "make it night", count=2, seed=7, guidance=3.0)
    wfs = comfy.workflows()
    assert [_node(wf, "KSampler")["seed"] for wf in wfs] == [7, 8]
    wf = wfs[0]
    assert _node(wf, "CLIPTextEncode")["text"] == "make it night"
    assert _node(wf, "FluxGuidance")["guidance"] == 3.0
    assert _node(wf, "UNETLoader")["unet_name"] == "flux1-dev-kontext_fp8_scaled.safetensors"
    assert not any(n["class_type"] == "CheckpointLoaderSimple" for n in wf.values())
    # the source is pre-sized to the Kontext size closest to 16:9, so FluxKontextImageScale is a no-op
    uploaded = comfy.uploads[_node(wf, "LoadImage")["image"]]
    assert Image.open(io.BytesIO(uploaded)).size == (1392, 752)
    meta = json.loads(Path(run["images"][0]["path"]).with_suffix(".json").read_text())
    assert meta["edit"] is True and meta["denoise"] is None and meta["negative"] == ""
    assert meta["params"]["width"] == 1392 and "checkpoint" not in meta["params"]
    assert "-kontext-edit-src-make-it-night" in run["run_dir"]
    html = (Path(run["run_dir"]) / "index.html").read_text()
    assert '"edit": true' in html


def test_kontext_size_picks_closest_aspect():
    assert imagegen.kontext_size(1000, 1000) == (1024, 1024)
    assert imagegen.kontext_size(900, 1200) == (880, 1184)
    assert imagegen.kontext_size(4000, 1000) == (1568, 672)


def test_edit_only_preset_and_bad_args(comfy, make_image):
    src = str(make_image())
    with pytest.raises(ValueError, match="edit-only"):
        imagegen.generate("x", "kontext")
    with pytest.raises(ValueError, match="edit-only"):
        imagegen.vary(src, prompt="x", preset="kontext")
    with pytest.raises(ValueError, match="can't edit; edit presets: kontext"):
        imagegen.edit(src, "x", preset="draft")
    with pytest.raises(ValueError, match="does not take cfg"):
        imagegen.generate("x", "kontext", source_image=src, edit=True, cfg=3)
    with pytest.raises(ValueError, match="no mask"):
        imagegen.generate("x", "kontext", source_image=src, mask=src, edit=True)
    with pytest.raises(ValueError, match="instruction"):
        imagegen.edit(src, "  ")
    with pytest.raises(FileNotFoundError):
        imagegen.edit(src + ".missing", "x")
    assert comfy.workflows() == []


def test_vary_of_an_edit_needs_a_prompt(comfy, make_image):
    run = imagegen.edit(str(make_image()), "make it night", seed=1)
    out = run["images"][0]["path"]
    with pytest.raises(ValueError, match="came from gen edit"):
        imagegen.vary(out)
    run2 = imagegen.vary(out, count=1, prompt="a cat")
    assert run2["preset"] == "draft"


def test_cli_edit(comfy, make_image, capsys):
    assert imagegen.main(["edit", str(make_image()), "make it wave", "-n", "1", "-g", "2.0"]) == 0
    assert "contact sheet:" in capsys.readouterr().out
    assert _node(comfy.workflows()[0], "FluxGuidance")["guidance"] == 2.0
    imagegen.main(["presets"])
    assert "kontext                (gen edit only)" in capsys.readouterr().out


def test_doctor_kontext_models_are_opt_in(comfy):
    conf = imagegen.load_config()
    comfy.models["diffusion_models"] = []
    checks = {c["name"]: c for c in doctor.check_models(conf, api_up=True)}
    c = checks["model flux1-dev-kontext_fp8_scaled.safetensors"]
    assert c["status"] == "warn" and "flux-kontext.sh" in c["fix"]
    # the same model used by a normal (non-edit) workflow would be required
    assert checks["model sd_xl_base_1.0.safetensors"]["status"] == "ok"


def test_doctor_nodes(comfy):
    conf = imagegen.load_config()
    assert doctor.check_nodes(conf, comfy.url)["status"] == "ok"
    comfy.nodes -= {"ReferenceLatent"}
    c = doctor.check_nodes(conf, comfy.url)
    assert c["status"] == "warn" and "ReferenceLatent (in flux_kontext.json)" in c["detail"]
    comfy.nodes -= {"KSampler"}
    assert doctor.check_nodes(conf, comfy.url)["status"] == "fail"


def test_wait_reports_a_crash_that_lost_the_job(monkeypatch):
    """ComfyUI OOM-killed mid-job: the connection drops, systemd restarts it, the job is gone."""
    c = imagegen.Comfy("http://x")
    answers = [ConnectionResetError(104, "reset"), b"{}", b'{"queue_running": [], "queue_pending": []}']

    def get(path, timeout=30):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(c, "_get", get)
    monkeypatch.setattr(imagegen.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="restarted and lost the job.*out of RAM"):
        c.wait("p1")
