"""Shared fixtures. Nothing here needs tulu, a GPU, ComfyUI, WSL or Windows.

Every test runs isolated (autouse `isolated`):
- IMAGEGEN_OUTPUT_DIR points at a temp folder, and IMAGEGEN_COMFY_URL at a closed port, so a test
  can never write to the real Pictures folder or queue a job on the real ComfyUI (even on tulu);
- rembg is replaced by a stub (`fake_rembg`), so the real model is never loaded or downloaded;
- tests/stubs is first on PATH: fake wslpath, powershell.exe, explorer.exe, systemctl and sudo
  that only record their calls (read them with `stub_calls()`), so nothing touches Windows or systemd.
Use the `comfy` fixture for a fake ComfyUI server (tests/fake_comfy.py).
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
STUBS = TESTS / "stubs"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(TESTS))

from fake_comfy import FakeComfy, png_bytes  # noqa: E402

CLOSED_URL = "http://127.0.0.1:9"  # discard port: connection refused straight away


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("IMAGEGEN_OUTPUT_DIR", str(out))
    monkeypatch.setenv("IMAGEGEN_COMFY_URL", CLOSED_URL)
    monkeypatch.setenv("PATH", f"{STUBS}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "stub-calls.log"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return out


@pytest.fixture
def out_dir(isolated) -> Path:
    return isolated


@pytest.fixture
def stub_calls(tmp_path):
    """Lines like "powershell.exe -NoProfile ..." for each stub call so far."""
    def read() -> list[str]:
        f = tmp_path / "stub-calls.log"
        return f.read_text().splitlines() if f.exists() else []
    return read


@pytest.fixture
def comfy(monkeypatch):
    """A running fake ComfyUI; IMAGEGEN_COMFY_URL points at it."""
    fake = FakeComfy().start()
    monkeypatch.setenv("IMAGEGEN_COMFY_URL", fake.url)
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def fake_rembg(monkeypatch):
    """rembg is heavy (onnxruntime + a 170 MB model, a download on first use): every test gets a stand-in
    module whose remove() returns an RGBA PNG. The list it returns records each call's input size."""
    calls = []
    mod = types.ModuleType("rembg")

    def remove(data: bytes) -> bytes:
        calls.append(len(data))
        return png_bytes((0, 0, 0, 0), mode="RGBA")

    mod.remove = remove
    monkeypatch.setitem(sys.modules, "rembg", mod)
    return calls


@pytest.fixture
def make_image(tmp_path):
    """Write a PNG (or other PIL format) and return its path."""
    def make(name="src.png", size=(300, 200), color=(10, 120, 200), mode="RGB") -> Path:
        from PIL import Image
        p = tmp_path / "inputs" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new(mode, size, color).save(p)
        return p
    return make


@pytest.fixture
def make_run(out_dir):
    """A finished run folder like generate() leaves it, without ComfyUI: PNGs, sidecars, run.json.

    Returns the run dict (as in run.json). No index.html: call write_contact_sheet for that.
    """
    def make(n=4, preset="draft", prompt="a misty fjord", date="2026-10-31", time="120000",
             transparent=False, base_seed=100, **extra) -> dict:
        run_dir = out_dir / date / f"{time}-{preset}-{prompt.replace(' ', '-')}"
        run_dir.mkdir(parents=True)
        images = []
        for i in range(n):
            seed = base_seed + i
            raw = f"{i + 1:02d}-seed{seed}.png"
            (run_dir / raw).write_bytes(png_bytes((i * 40, 100, 200)))
            final = raw
            if transparent:
                final = raw.removesuffix(".png") + "-transparent.png"
                (run_dir / final).write_bytes(png_bytes((0, 0, 0, 0), mode="RGBA"))
            meta = {"prompt": prompt, "full_prompt": prompt, "negative": "blurry", "user_negative": None,
                    "preset": preset, "seed": seed, "file": final, "raw_file": raw,
                    "params": {"width": 1024, "height": 1024, "steps": 25, "cfg": 7.0}}
            (run_dir / raw).with_suffix(".json").write_text(json.dumps(meta))
            images.append({"path": str(run_dir / final), "windows_path": None, "seed": seed})
        run = {"preset": preset, "prompt": prompt, "base_seed": base_seed, "count": n,
               "run_dir": str(run_dir), "images": images, "transparent": transparent, **extra}
        (run_dir / "run.json").write_text(json.dumps(run))
        return run
    return make
