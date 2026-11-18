"""tunnel-cloudflared.sh (#71): pinned cloudflared download, checksum checked, y/N first, removable."""
import os
import re

import pytest

from conftest import REPO
from shell_helpers import run_script

SCRIPT = str(REPO / "tunnel-cloudflared.sh")


@pytest.fixture
def env(tmp_path):
    """A fake curl (records its args, writes FAKE_BODY to -o) and a temp install path."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/usr/bin/env bash\necho "$@" >> "$CURL_LOG"\n'
                    'while [ $# -gt 0 ]; do [ "$1" = -o ] && { printf %s "$FAKE_BODY" > "$2"; }; shift; done\n')
    curl.chmod(0o755)
    e = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CURL_LOG=str(tmp_path / "curl.log"),
             FAKE_BODY="not the real binary", CLOUDFLARED_BIN=str(tmp_path / "bin" / "cloudflared"))
    return e


def pinned():
    text = open(SCRIPT).read()
    return re.search(r"^VERSION=(\S+)", text, re.M).group(1), re.search(r"^SHA256=(\S+)", text, re.M).group(1)


def test_pins_a_version_and_a_full_sha256():
    version, sha = pinned()
    assert re.fullmatch(r"\d{4}\.\d+\.\d+", version) and re.fullmatch(r"[0-9a-f]{64}", sha)


def test_no_terminal_means_no_download(env, tmp_path):
    r = run_script([SCRIPT, "install"], env)
    assert r.code == 1 and "Nothing downloaded" in r.out
    assert not (tmp_path / "curl.log").exists()


def test_checksum_mismatch_installs_nothing(env, tmp_path):
    r = run_script([SCRIPT, "install"], env, answer="y")
    assert r.code == 1 and "checksum mismatch" in r.out
    assert not (tmp_path / "bin" / "cloudflared").exists() and not (tmp_path / "bin" / "cloudflared.part").exists()
    args = (tmp_path / "curl.log").read_text()
    version, _ = pinned()
    assert f"/{version}/cloudflared-linux-amd64" in args and "--proto =https" in args


def test_status_and_remove(env, tmp_path):
    assert "not installed" in run_script([SCRIPT, "status"], env).out
    b = tmp_path / "bin"
    b.mkdir()
    (b / "cloudflared").write_text("#!/bin/sh\necho cloudflared version 0\n")
    (b / "cloudflared").chmod(0o755)
    assert "not the pinned" in run_script([SCRIPT, "status"], env).out
    r = run_script([SCRIPT, "remove"], env)
    assert r.code == 0 and not (b / "cloudflared").exists()
