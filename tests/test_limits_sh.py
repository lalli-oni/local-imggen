"""comfy/limits.sh and comfy/install.sh with the fake systemctl/sudo (tests/stubs) and the fake ComfyUI.

The scripts hard-code /etc/systemd/system/comfyui.service.d; the tests run copies with that one
path pointed at a temp dir (and fail loudly if the line they rewrite ever changes).
"""
import os
import shutil
from pathlib import Path

import pytest

from conftest import REPO
from shell_helpers import run_script

REAL_DIR = "/etc/systemd/system/comfyui.service.d"
BASE = "/home/tulu/venvs/comfy/bin/python /home/tulu/comfyui/main.py --listen 127.0.0.1"


@pytest.fixture
def sh(tmp_path, comfy):
    dropins = tmp_path / "comfyui.service.d"
    comfy_dir = tmp_path / "repo" / "comfy"
    comfy_dir.mkdir(parents=True)
    for f in (REPO / "comfy").iterdir():
        text = f.read_text()
        if f.suffix == ".sh":
            assert REAL_DIR in text, f"{f.name}: drop-in path changed, update this test"
            text = text.replace(REAL_DIR, str(dropins))
        (comfy_dir / f.name).write_text(text)
        (comfy_dir / f.name).chmod(f.stat().st_mode)
    env = dict(os.environ, FAKE_DROPIN_DIR=str(dropins), FAKE_BASE_EXEC=BASE, IMAGEGEN_COMFY_URL=comfy.url)

    class Sh:
        dropin = dropins / "zz-imagegen-limits.conf"
        comfy_srv = comfy

        def run(self, *args, answer=None, script="limits.sh", **extra_env):
            return run_script([str(comfy_dir / script), *args], {**env, **extra_env}, answer=answer)

    return Sh()


def test_status_off(sh):
    r = sh.run("status")
    assert r.code == 0
    assert "Limits: off" in r.out and f"ExecStart: {BASE}" in r.out and "Nice=0" in r.out


def test_on_with_yes_flag(sh, stub_calls):
    r = sh.run("on", "-y")
    assert r.code == 0, r.out
    text = sh.dropin.read_text()
    assert "Nice=10" in text and "CPUWeight=20" in text
    assert f"ExecStart=\nExecStart={BASE} --reserve-vram 2 --disable-smart-memory" in text
    assert "ComfyUI is up" in r.out
    calls = stub_calls()
    assert "sudo systemctl restart comfyui" in calls and "sudo systemctl daemon-reload" in calls
    st = sh.run("status")
    assert "Limits: ON" in st.out and "--reserve-vram 2" in st.out and "Nice=10" in st.out


def test_on_twice_never_stacks_flags(sh):
    sh.run("on", "-y")
    sh.run("on", "-y")
    assert sh.dropin.read_text().count("--reserve-vram") == 1
    assert not sh.dropin.with_name(sh.dropin.name + ".off").exists()


def test_off(sh):
    sh.run("on", "-y")
    r = sh.run("off", "-y")
    assert r.code == 0 and not sh.dropin.exists()
    assert "already off" in sh.run("off", "-y").out


def test_no_terminal_means_no(sh, stub_calls):
    r = sh.run("on")
    assert r.code == 0 and "Nothing changed" in r.out
    assert not sh.dropin.exists() and not any("restart" in c for c in stub_calls())


def test_answer_y_on_terminal(sh):
    r = sh.run("on", answer="y")
    assert r.code == 0, r.out
    assert "Turn resource limits ON? This restarts comfyui. Continue? [y/N]" in r.out
    assert sh.dropin.exists()


def test_answer_n_warns_when_busy(sh):
    sh.comfy_srv.queue_running, sh.comfy_srv.queue_pending = 1, 2
    r = sh.run("on", answer="n")
    assert "ComfyUI is busy: 1 running, 2 queued" in r.out and "Nothing changed" in r.out
    assert not sh.dropin.exists()


def test_no_service(sh):
    r = sh.run("on", "-y", FAKE_NO_SERVICE="1")
    assert r.code == 1 and "no comfyui systemd service" in r.out


def test_comfy_does_not_come_back(sh):
    r = sh.run("on", "-y", FAKE_COMFYUI_STATE="failed")
    assert r.code == 1 and "did not come up" in r.out


def test_bad_usage(sh):
    assert sh.run("sideways").code == 2


def test_install_sh_does_nothing_when_off(sh, stub_calls):
    r = sh.run(script="install.sh")
    assert r.code == 0 and "Resource limits are off" in r.out
    assert not sh.dropin.exists() and not any(c.startswith("sudo") for c in stub_calls())


def test_install_sh_reapplies_when_on(sh):
    sh.run("on", "-y")
    sh.dropin.write_text(sh.dropin.read_text().replace("Nice=10", "Nice=5"))  # an older version
    r = sh.run(script="install.sh", answer="y")  # update.sh runs it from a terminal: limits.sh asks again
    assert r.code == 0 and "re-applying" in r.out and "Re-apply resource limits?" in r.out
    assert "Nice=10" in sh.dropin.read_text()
    assert "Nothing changed" in sh.run(script="install.sh").out  # no terminal = no


def test_install_sh_is_committed_executable():
    # update.sh only runs comfy/install.sh when git has it as 100755
    import subprocess
    out = subprocess.run(["git", "ls-files", "-s", "comfy/install.sh"], cwd=REPO, capture_output=True, text=True)
    if not out.stdout:
        pytest.skip("not a git checkout")
    assert out.stdout.startswith("100755 ")
