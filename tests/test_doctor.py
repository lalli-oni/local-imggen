"""doctor.py against the fake ComfyUI, the fake systemctl and a fake ~/venvs/comfy python."""
import json
import stat

import pytest

import doctor
import imagegen


@pytest.fixture
def comfy_venv(tmp_path, monkeypatch):
    """A fake ~/venvs/comfy/bin/python that answers doctor's version query (never a real python3)."""
    py = tmp_path / "comfy-venv" / "python"
    py.parent.mkdir()

    def set_versions(**versions):
        py.write_text(f"#!/bin/sh\necho '{json.dumps(versions)}'\n")
        py.chmod(py.stat().st_mode | stat.S_IEXEC)

    set_versions(torch=doctor.EXPECTED_TORCH, torchvision="0.24.0+rocm7.2.0", torchaudio="2.9.0+rocm7.2.0")
    monkeypatch.setattr(doctor, "COMFY_VENV_PY", py)
    return set_versions


@pytest.fixture
def healthy(comfy, comfy_venv, tmp_path, monkeypatch):
    u2 = tmp_path / "u2net"
    u2.mkdir()
    (u2 / "u2net.onnx").write_bytes(b"x")
    monkeypatch.setenv("U2NET_HOME", str(u2))
    (tmp_path / "dropin.conf").write_text("")  # healthy = limits on: the wallpapers use Qwen (#51)
    monkeypatch.setattr(doctor, "LIMITS_DROPIN", tmp_path / "dropin.conf")
    return comfy


def by_name(checks):
    return {c["name"]: c for c in checks}


def test_all_good(healthy):
    checks = doctor.run_checks()
    bad = [c for c in checks if c["status"] in ("warn", "fail")]
    assert not bad, bad
    names = by_name(checks)
    assert "RX 9070 XT" in names["ComfyUI API"]["detail"] and "12.0/16.0 GiB" in names["ComfyUI API"]["detail"]
    assert names["MCP server"]["status"] == "ok"
    assert names["resource limits"]["detail"].startswith("ON")


def test_main_exit_codes(healthy, capsys, monkeypatch):
    assert doctor.main() == 0
    assert "all good" in capsys.readouterr().out
    monkeypatch.setenv("FAKE_COMFYUI_STATE", "failed")
    assert doctor.main() == 1
    out = capsys.readouterr().out
    assert "✗ comfyui service: failed" in out and "fix: sudo systemctl start comfyui" in out


def test_api_down():
    c, stats = doctor.check_comfy_api("http://127.0.0.1:9")
    assert c["status"] == "fail" and stats is None


@pytest.mark.parametrize("wsl, dxg, status", [(False, False, "info"), (True, True, "ok"), (True, False, "fail")])
def test_gpu_device(monkeypatch, tmp_path, wsl, dxg, status):
    node = tmp_path / "dxg"
    if dxg:
        node.write_text("")
    monkeypatch.setattr(doctor, "DXG", node)
    monkeypatch.setattr(doctor, "_is_wsl", lambda: wsl)
    c = doctor.check_gpu_device()
    assert c["status"] == status
    assert ("wsl --shutdown" in c.get("fix", "")) == (status == "fail")


def test_no_gpu(comfy):
    comfy.no_gpu = True
    c, _ = doctor.check_comfy_api(comfy.url)
    assert c["status"] == "fail" and "no GPU" in c["detail"]


def test_generic_torch_is_a_fail(comfy_venv):
    comfy_venv(torch="2.9.1+cu128", torchvision="0.24.0+rocm7.2.0", torchaudio=None)
    c = doctor.check_torch()
    assert c["status"] == "fail" and "torch 2.9.1+cu128" in c["detail"] and "fix" in c


def test_other_rocm_torch_is_a_warning(comfy_venv):
    comfy_venv(torch="2.10.0+rocm7.3.0")
    assert doctor.check_torch()["status"] == "warn"


@pytest.mark.parametrize("torch, status", [
    ("2.9.1+rocm7.2.0.lw.git7e1940d4", "ok"),  # AMD's build tag on the expected wheel (#76)
    ("2.9.2+rocm7.2.0", "warn"),
    ("2.9.1+rocm7.2.01", "warn"),  # a different local version, not a build tag
])
def test_torch_build_tag(comfy_venv, torch, status):
    comfy_venv(torch=torch, torchvision="0.24.0+rocm7.2.0", torchaudio="2.9.0+rocm7.2.0")
    assert doctor.check_torch()["status"] == status


def test_missing_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "COMFY_VENV_PY", tmp_path / "nope")
    assert doctor.check_torch()["status"] == "fail"


def test_missing_model_from_api(comfy):
    comfy.models["upscale_models"] = []
    checks = by_name(doctor.check_models(imagegen.load_config(), api_up=True))
    assert checks["model RealESRGAN_x4plus.pth"]["status"] == "fail"
    assert checks["model sd_xl_base_1.0.safetensors"]["status"] == "ok"


def test_models_from_disk_when_api_down(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "COMFY_DIR", tmp_path / "comfyui")
    (tmp_path / "comfyui/models/checkpoints").mkdir(parents=True)
    (tmp_path / "comfyui/models/checkpoints/sd_xl_base_1.0.safetensors").write_bytes(b"")
    checks = by_name(doctor.check_models(imagegen.load_config(), api_up=False))
    assert checks["model sd_xl_base_1.0.safetensors"]["status"] == "ok"
    assert checks["model RealESRGAN_x4plus.pth"]["status"] == "fail"
    assert checks["model rembg u2net"]["status"] == "warn"


def test_workflow_placeholder_without_value(monkeypatch):
    conf = imagegen.load_config()
    del conf["presets"]["wallpaper"]["upscale_model"]
    conf["presets"]["draft"]["post"] = ["sparkle"]
    checks = by_name(doctor.check_workflows(conf))
    assert "{{upscale_model}}" in checks["preset wallpaper"]["detail"]
    assert "sparkle" in checks["preset draft"]["detail"]


def test_output_dir(out_dir, tmp_path):
    assert "created on the first run" in doctor.check_output_dir({"output_dir": str(out_dir)})["detail"]
    out_dir.mkdir()
    assert doctor.check_output_dir({"output_dir": str(out_dir)})["detail"].endswith("writable")
    assert not list(out_dir.iterdir())  # the probe file is gone


def test_limits_on(monkeypatch, tmp_path):
    f = tmp_path / "dropin.conf"
    f.write_text("")
    monkeypatch.setattr(doctor, "LIMITS_DROPIN", f)
    assert doctor.check_limits()["detail"].startswith("ON")


def test_limits_off_warns_when_wallpapers_use_qwen(monkeypatch, tmp_path):
    """#51: a Qwen run without the limits stalled Windows during a video."""
    monkeypatch.setattr(doctor, "LIMITS_DROPIN", tmp_path / "none.conf")
    conf = imagegen.load_config()
    c = doctor.check_limits(conf)
    assert c["status"] == "warn" and "wallpaper" in c["detail"] and c["fix"] == "~/imagegen/comfy/limits.sh on"
    sdxl = {"presets": {"wallpaper": {"checkpoint": "x"}}, "daily": {"preset": "wallpaper"}}
    assert doctor.check_limits(sdxl)["status"] == "info"


def test_bad_config(monkeypatch):
    monkeypatch.setattr(imagegen, "load_config", lambda: (_ for _ in ()).throw(ValueError("bad toml")))
    assert doctor.run_checks() == [{"name": "config.toml", "status": "fail", "detail": "bad toml",
                                    "fix": "fix the TOML syntax in config.toml"}]
