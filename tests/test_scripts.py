"""Every shell script parses, and shellcheck (warnings and up) is clean."""
import shutil
import subprocess

import pytest

from conftest import REPO

SCRIPTS = sorted(p for p in [*REPO.glob("*.sh"), *REPO.glob("comfy/*.sh"), *REPO.glob("windows/*.sh"),
                             *(REPO / "tests" / "stubs").iterdir()] if p.is_file())


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(REPO)))
def test_bash_syntax(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_shellcheck():
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck not installed (it comes with requirements-dev.txt)")
    r = subprocess.run([sc, "-S", "warning", *map(str, SCRIPTS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
