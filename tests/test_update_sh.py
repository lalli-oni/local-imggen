"""update.sh against a scratch git remote: preview, risk flags, y/N, fast-forward, follow-up steps.

The scratch repo commits the real update.sh, so the script under test is always the current one.
pip is a stub in a fake ~/venvs/imagegen; comfy/install.sh is a stub that only records it ran.
"""
import os
import subprocess
from pathlib import Path

import pytest

from conftest import REPO
from shell_helpers import run_script

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class Repos:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env = {**os.environ, **GIT_ENV}
        self.log = tmp / "calls.log"
        origin, self.dev, self.work = tmp / "origin.git", tmp / "dev", tmp / "work"
        self.git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(tmp, "clone", "-q", str(origin), str(self.dev))
        self.write("update.sh", (REPO / "update.sh").read_text(), mode=0o755)
        self.write("requirements.txt", "mcp>=2,<3\npillow\n")
        self.write("comfy/install.sh", f"#!/bin/sh\necho install.sh ran >> {self.log}\n", mode=0o755)
        self.write("mcp_server.py", "# v1\n")
        self.commit("Initial import")
        self.git(self.dev, "push", "-q", "origin", "main")
        self.git(tmp, "clone", "-q", str(origin), str(self.work))
        pip = tmp / "home" / "venvs" / "imagegen" / "bin" / "pip"
        pip.parent.mkdir(parents=True)
        # records the requirements it was given (update.sh passes a filtered temp copy)
        pip.write_text(f'#!/bin/sh\necho "pip $*" >> {self.log}\nshift 2; cat "$2" >> {self.log}\n')
        pip.chmod(0o755)

    def git(self, cwd, *args) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel, text, mode=None):
        f = self.dev / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        if mode:
            f.chmod(mode)

    def commit(self, msg):
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-q", "-m", msg)

    def pr(self, num, title, files: dict[str, str]):
        """Land a change on origin/main the way GitHub's merge button does."""
        self.git(self.dev, "checkout", "-q", "-b", f"pr{num}")
        for rel, text in files.items():
            self.write(rel, text)
        self.commit(title)
        self.git(self.dev, "checkout", "-q", "main")
        self.git(self.dev, "merge", "-q", "--no-ff", f"pr{num}", "-m",
                 f"Merge pull request #{num} from lalli-oni/pr{num}", "-m", title)
        self.git(self.dev, "push", "-q", "origin", "main")

    def update(self, answer=None):
        return run_script([str(self.work / "update.sh")], {**self.env, "HOME": str(self.tmp / "home")},
                          cwd=self.tmp, answer=answer)

    def head(self):
        return self.git(self.work, "rev-parse", "HEAD")

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture
def repos(tmp_path):
    return Repos(tmp_path)


def test_up_to_date(repos):
    r = repos.update()
    assert r.code == 0 and "Already up to date." in r.out


def test_preview_then_no_terminal_changes_nothing(repos):
    before = repos.head()
    repos.pr(7, "Add a thing", {"imagegen_extra.py": "x = 1\n"})
    r = repos.update()
    assert r.code == 0
    assert "PR #7: Add a thing" in r.out and "imagegen_extra.py" in r.out
    assert "No sudo, systemd, .wslconfig, dependency or download changes found." in r.out
    assert "Nothing applied" in r.out and repos.head() == before


def test_yes_fast_forwards_and_restarts_hint(repos):
    repos.pr(8, "Better MCP", {"mcp_server.py": "# v2\n"})
    r = repos.update(answer="y")
    assert r.code == 0, r.out
    assert repos.head() == repos.git(repos.dev, "rev-parse", "HEAD")
    assert "quit and reopen the Claude app" in r.out
    assert (repos.work / "mcp_server.py").read_text() == "# v2\n"


def test_answer_n(repos):
    before = repos.head()
    repos.pr(9, "x", {"a.txt": "a\n"})
    r = repos.update(answer="n")
    assert "Nothing applied" in r.out and repos.head() == before


def test_requirements_change_installs_without_torch(repos):
    repos.pr(10, "Deps", {"requirements.txt": "mcp>=2,<3\npillow\ntorch==2.9\ntorchvision\nrich\n"})
    r = repos.update(answer="y")
    assert r.code == 0, r.out
    assert "!! Dependencies" in r.out and "+rich" in r.out
    calls = repos.calls()
    assert "pip install -q -r" in calls and "rich" in calls
    assert "torch" not in calls  # filtered out before pip sees it


def test_comfy_change_runs_install_sh(repos):
    repos.pr(11, "Limits tweak", {"comfy/limits.conf": "[Service]\nNice=5\n"})
    r = repos.update(answer="y")
    assert r.code == 0, r.out
    assert "!! systemd / services" in r.out and "run comfy/install.sh" in r.out
    assert "install.sh ran" in repos.calls()


@pytest.mark.parametrize("line, flag", [
    ("sudo rm -rf /x", "Uses sudo / root"),
    ("systemctl --user enable foo.timer", "systemd / services"),
    ("wget https://example.com/model.safetensors", "Downloads"),
    ("powershell.exe -Command x", "WSL / Windows config"),
])
def test_risky_lines_are_flagged(repos, line, flag):
    repos.pr(12, "Risky", {"tool.sh": f"#!/bin/sh\n{line}\n"})
    r = repos.update()
    assert f"!! {flag}" in r.out and line[:30] in r.out
    assert "!! Shell scripts that may be run" in r.out


def test_comments_are_not_flagged(repos):
    repos.pr(13, "Docs", {"notes.py": "# sudo is never needed here\n"})
    assert "Uses sudo" not in repos.update().out


def test_local_edits_refuse(repos):
    (repos.work / "mcp_server.py").write_text("# local\n")
    repos.pr(14, "x", {"a.txt": "a\n"})
    r = repos.update(answer="y")
    assert r.code == 1 and "Local edits found" in r.out


def test_local_commits_refuse(repos):
    (repos.work / "local.txt").write_text("x\n")
    repos.git(repos.work, "add", "local.txt")
    repos.git(repos.work, "commit", "-q", "-m", "local")
    repos.pr(15, "x", {"a.txt": "a\n"})
    r = repos.update(answer="y")
    assert r.code == 1 and "Local commits not on" in r.out
