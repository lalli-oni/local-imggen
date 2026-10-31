"""The gen CLI: imagegen.main() in-process, plus one real subprocess run for the exit code path."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import imagegen


def run_main(argv, capsys):
    code = imagegen.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_presets(capsys):
    code, out, _ = run_main(["presets"], capsys)
    assert code == 0
    assert {line.split()[0] for line in out.splitlines()} == set(imagegen.list_presets())


def test_generate_prints_paths(comfy, capsys):
    code, out, _ = run_main(["a fox", "-n", "2", "--seed", "4", "--size", "512x256"], capsys)
    lines = out.splitlines()
    assert code == 0 and len(lines) == 3
    assert lines[-1].startswith("contact sheet: \\\\wsl.localhost\\")  # fake wslpath
    latent = next(n["inputs"] for n in comfy.workflows()[0].values() if n["class_type"] == "EmptyLatentImage")
    assert (latent["width"], latent["height"]) == (512, 256)


def test_open_calls_explorer(comfy, capsys, stub_calls):
    code, out, _ = run_main(["a fox", "--open"], capsys)
    assert code == 0
    assert any(c.startswith("explorer.exe \\\\wsl.localhost\\") and c.endswith("index.html") for c in stub_calls())


def test_set_wallpaper_refuses_wsl_paths(make_image):
    with pytest.raises(RuntimeError, match="Windows drive"):
        imagegen.set_wallpaper(str(make_image()))


def test_set_wallpaper_on_windows_drive(make_image, monkeypatch, stub_calls):
    img = make_image()
    monkeypatch.setattr(imagegen, "to_windows_path", lambda p: "C:\\Users\\lalli\\it's.png")
    assert imagegen.set_wallpaper(str(img)) == "C:\\Users\\lalli\\it's.png"
    call = next(c for c in stub_calls() if c.startswith("powershell.exe"))
    import base64
    script = base64.b64decode(call.split("-EncodedCommand ")[1]).decode("utf-16-le")
    assert "SystemParametersInfo(20, 0, 'C:\\Users\\lalli\\it''s.png', 3)" in script  # quote doubled


def test_recent(make_run, capsys):
    make_run(prompt="older", time="100000")
    make_run(prompt="newer", time="110000")
    code, out, _ = run_main(["recent", "1"], capsys)
    assert code == 0 and "newer" in out and "older" not in out


def test_sheet_rebuilds_latest_and_date(make_run, capsys, out_dir):
    a = make_run(prompt="one", time="100000")
    b = make_run(prompt="two", time="110000")
    code, out, _ = run_main(["sheet"], capsys)
    assert code == 0 and (Path(b["run_dir"]) / "index.html").is_file()
    assert not (Path(a["run_dir"]) / "index.html").exists()
    code, out, _ = run_main(["sheet", "2026-10-31"], capsys)
    assert code == 0 and len(out.splitlines()) == 2


def test_sheet_errors(capsys, out_dir):
    code, _, err = run_main(["sheet"], capsys)
    assert code == 1 and "no runs yet" in err
    code, _, err = run_main(["sheet", "C:Users"], capsys)
    assert code == 1 and "quotes" in err


def test_cutout(make_image, fake_rembg, capsys):
    code, out, _ = run_main(["cutout", str(make_image("logo.png"))], capsys)
    assert code == 0 and out.strip().endswith("logo-transparent.png")
    code, _, err = run_main(["cutout", "/nope.png"], capsys)
    assert code == 1


def test_vary_cli(comfy, capsys, make_image):
    code, out, _ = run_main(["vary", str(make_image()), "--prompt", "cat", "-n", "1", "-s", "0.3"], capsys)
    assert code == 0
    ks = next(n["inputs"] for n in comfy.workflows()[0].values() if n["class_type"] == "KSampler")
    assert ks["denoise"] == 0.3


def test_errors_exit_1_without_traceback():
    # a real process: the __main__ handler turns known errors into "error: ..." and exit 1
    r = subprocess.run([sys.executable, str(imagegen.ROOT / "imagegen.py"), "x", "-p", "nope"],
                       capture_output=True, text=True, env=os.environ)
    assert r.returncode == 1
    assert r.stderr.strip().startswith("error: unknown preset") and "Traceback" not in r.stderr
