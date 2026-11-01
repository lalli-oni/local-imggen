"""The opt-in model download scripts (models/*.sh) with a fake wget/curl and a temp models folder:
nothing reaches Hugging Face."""
import hashlib
import os
import re

import pytest

from conftest import REPO
from shell_helpers import run_script

SCRIPTS = sorted(p.name for p in (REPO / "models").glob("*.sh") if not p.name.startswith("_"))
DATA = b"fake model bytes\n"


@pytest.fixture
def env(tmp_path):
    """Fake wget (writes DATA, or fails with FAKE_WGET_FAIL) and curl (sends FAKE_SHA as X-Linked-Etag)."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "wget").write_text('#!/bin/sh\necho "wget $*" >> "$STUB_LOG"\n[ -n "$FAKE_WGET_FAIL" ] && exit 8\n'
                               'while [ $# -gt 0 ]; do [ "$1" = -O ] && { shift; printf "fake model bytes\\n" > "$1"; }; '
                               'shift; done\n')
    (bin_ / "curl").write_text('#!/bin/sh\n[ -n "$FAKE_SHA" ] && printf \'x-linked-etag: "%s"\\r\\n\' "$FAKE_SHA"\n'
                               'exit 0\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    models = tmp_path / "models"
    models.mkdir()
    return dict(os.environ, PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", COMFY_MODELS=str(models),
                FAKE_SHA=hashlib.sha256(DATA).hexdigest(), HF_TOKEN="")


def _files(script):
    text = (REPO / "models" / script).read_text()
    return re.findall(r'^\s*"(\w+)\|([^|]+)\|([^|]+)\|([\d.]+)"$', text, re.M)


@pytest.mark.parametrize("script", SCRIPTS)
def test_lists_missing_and_says_no_without_a_terminal(script, env):
    r = run_script([str(REPO / "models" / script)], env)
    assert r.code == 1 and "Nothing downloaded." in r.out
    files = _files(script)
    assert files, "no FILES entries"
    for folder, name, path, _ in files:
        assert f"missing  {folder}/{name}" in r.out
        assert path.endswith("/" + name) and "/resolve/main/" in path
        assert "int8" not in name  # int8_convrot renders black on the RX 9070 XT (#40)
    assert "To download: about" in r.out


@pytest.mark.parametrize("script", SCRIPTS)
def test_downloads_verifies_and_is_then_done(script, env, tmp_path):
    r = run_script([str(REPO / "models" / script), "-y"], env)
    assert r.code == 0, r.out
    assert "Done. Check with: gen doctor" in r.out and "verifying sha256" in r.out
    models = tmp_path / "models"
    for folder, name, _, _ in _files(script):
        assert (models / folder / name).read_bytes() == DATA
        assert not (models / folder / (name + ".part")).exists()
    again = run_script([str(REPO / "models" / script)], env)
    assert again.code == 0 and "All there. Try: gen edit" in again.out


def test_checksum_mismatch_deletes_the_file(env, tmp_path):
    r = run_script([str(REPO / "models" / "flux2-klein.sh"), "-y"], {**env, "FAKE_SHA": "0" * 64})
    assert r.code == 1 and "checksum mismatch" in r.out
    assert not any((tmp_path / "models").rglob("*.safetensors*"))


def test_failed_download_keeps_the_part_for_resume(env, tmp_path):
    r = run_script([str(REPO / "models" / "qwen-image-2.1.sh"), "--yes"], {**env, "FAKE_WGET_FAIL": "1"})
    assert r.code == 1 and "download failed. Re-run to resume" in r.out


def test_help_and_bad_args(env):
    r = run_script([str(REPO / "models" / "flux2-klein.sh"), "--help"], env)
    assert r.code == 0 and r.out.startswith("Opt-in download of FLUX.2 [klein]") and "set -" not in r.out
    assert run_script([str(REPO / "models" / "flux2-klein.sh"), "--nope"], env).code == 2


def test_presets_and_scripts_agree():
    """Every model file a preset with `download` names is in that script's FILES."""
    import imagegen
    for name, p in imagegen.list_presets().items():
        if "download" not in p:
            continue
        names = {n for _, n, _, _ in _files(p["download"])}
        for key in ("unet", "text_encoder", "clip_l", "t5", "vae"):
            if key in p:
                assert p[key] in names, f"{name}.{key} = {p[key]} not in {p['download']}"
