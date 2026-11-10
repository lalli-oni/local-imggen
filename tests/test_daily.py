"""#3: gen daily (theme pick, cast prompt, set wallpaper, failure leaves it alone) and windows/daily-task.sh."""
import base64
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

import characters
import daily
import imagegen
from conftest import REPO
from shell_helpers import run_script

TULU = "chibi green cthulhu, amber eyes"


@pytest.fixture
def daily_conf(monkeypatch):
    """Override [daily] keys for a test: daily_conf(themes=[...], cast=[]) etc."""
    real = imagegen.load_config

    def set_(**kw):
        def load():
            conf = real()
            conf["daily"] = {**conf.get("daily", {}), **kw}
            return conf
        monkeypatch.setattr(imagegen, "load_config", load)
    return set_


@pytest.fixture
def tulu(out_dir):
    characters.new("tulu", "lil tulu", description=TULU)
    return "tulu"


@pytest.fixture
def on_c_drive(monkeypatch):
    """Pretend output_dir is on C: so set_wallpaper accepts it (the fake wslpath maps temp dirs to \\\\wsl$)."""
    monkeypatch.setattr(imagegen, "to_windows_path", lambda p: "C:\\imagegen\\" + Path(p).name)


def _wallpaper_calls(stub_calls) -> list[str]:
    return [base64.b64decode(c.split("-EncodedCommand ")[1]).decode("utf-16-le")
            for c in stub_calls() if c.startswith("powershell.exe") and "-EncodedCommand" in c]


def _log_lines(out_dir) -> list[str]:
    f = out_dir / "daily.log"
    return f.read_text().splitlines() if f.exists() else []


# ---------------------------------------------------------------- theme pick

def test_config_has_a_usable_daily_section():
    d = daily.settings()
    assert d["preset"] in imagegen.list_presets() and d["themes"] and d["cast"] == ["tulu"]
    for day in (date(2026, 10, 3), date(2026, 11, 10), date(2026, 12, 24)):
        theme, where = daily.pick_theme(d, day)
        assert theme in daily._pool(d["months"].get(daily.MONTHS[day.month - 1]) or d["themes"])


def test_each_theme_once_per_round_and_stable(daily_conf):
    themes = [f"theme {i}" for i in range(5)]
    daily_conf(themes=themes, months={}, weekdays={})
    d = daily.settings()
    start = date(2026, 11, 10)
    start -= timedelta(days=start.toordinal() % 5)  # start of a round
    rounds = [[daily.pick_theme(d, start + timedelta(days=r * 5 + i))[0] for i in range(5)] for r in range(3)]
    assert all(sorted(r) == themes for r in rounds)
    assert len({tuple(r) for r in rounds}) > 1  # the order changes between rounds
    assert daily.pick_theme(d, start) == daily.pick_theme(d, start)
    # reordering config.toml does not change the day's theme
    daily_conf(themes=list(reversed(themes)), months={}, weekdays={})
    assert daily.pick_theme(daily.settings(), start) == daily.pick_theme(d, start)


def test_no_theme_twice_in_a_row(daily_conf):
    for n in (2, 3, 4):
        daily_conf(themes=[f"t{i}" for i in range(n)], months={}, weekdays={})
        d = daily.settings()
        picks = [daily.pick_theme(d, date(2026, 1, 1) + timedelta(days=i))[0] for i in range(400)]
        assert all(a != b for a, b in zip(picks, picks[1:])), n


def test_weekday_pool_counts_weeks(daily_conf):
    themes = [f"fri {i}" for i in range(7)]
    daily_conf(themes=["plain"], months={}, weekdays={"fri": themes})
    d = daily.settings()
    first = date(2026, 1, 2)  # a Friday
    first += timedelta(weeks=-((first.toordinal() // 7) % 7))  # the first Friday of a round
    fridays = [daily.pick_theme(d, first + timedelta(weeks=i))[0] for i in range(7)]
    assert sorted(fridays) == themes  # 7 Fridays in a row: each theme once


def test_month_beats_weekday_beats_themes(daily_conf):
    daily_conf(themes=["plain"], weekdays={"fri": "friday thing"}, months={"dec": ["xmas"]})
    d = daily.settings()
    assert daily.pick_theme(d, date(2026, 11, 13)) == ("friday thing", "weekdays.fri")  # a Friday
    assert daily.pick_theme(d, date(2026, 11, 12)) == ("plain", "themes")
    assert daily.pick_theme(d, date(2026, 12, 11)) == ("xmas", "months.dec")  # a Friday in December


def test_bad_daily_settings(daily_conf):
    daily_conf(weekdays={"friday": "x"})
    with pytest.raises(ValueError, match="unknown friday"):
        daily.settings()
    daily_conf(weekdays={}, preset="nope")
    with pytest.raises(ValueError, match="preset 'nope'"):
        daily.settings()
    daily_conf(preset="wallpaper", themes=[], months={}, weekdays={})
    with pytest.raises(ValueError, match="no themes"):
        daily.pick_theme(daily.settings(), date(2026, 11, 10))


def test_prompt_puts_the_cast_first(tulu, daily_conf):
    daily_conf(themes=["on a beach"], months={}, weekdays={}, style="soft light")
    p = daily.plan(date(2026, 11, 10))
    assert p["prompt"] == f"{TULU}, on a beach, soft light"
    assert p["cast"] == {"tulu": 1}


def test_cast_may_be_one_name(tulu, daily_conf):
    daily_conf(cast="tulu")
    assert daily.plan(date(2026, 11, 10))["cast"] == {"tulu": 1}


def test_show(tulu, capsys):
    assert imagegen.main(["daily", "--show", "--date", "2026-12-24"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("2026-12-24: ") and "(from months.dec)" in out and "tulu v1" in out
    assert f"prompt:  {TULU}, " in out


# ---------------------------------------------------------------- runs

def test_daily_generates_and_sets_wallpaper(tulu, comfy, on_c_drive, stub_calls, out_dir, capsys):
    assert imagegen.main(["daily", "--wait", "5"]) == 0
    [wf] = comfy.workflows()
    prompts = [n["inputs"]["text"] for n in wf.values() if n["class_type"] == "CLIPTextEncode"]
    assert any(t.startswith(TULU + ", ") for t in prompts)
    [run] = imagegen.recent_runs(1)
    assert run["preset"] == "wallpaper" and "-wallpaper-daily-" in run["run_dir"]
    meta = json.loads((Path(run["run_dir"]) / "run.json").read_text())
    assert meta["daily"]["cast"] == {"tulu": 1} and meta["daily"]["date"] == date.today().isoformat()
    [script] = _wallpaper_calls(stub_calls)
    assert f"'C:\\imagegen\\{Path(run['images'][0]).name}'" in script
    assert "wallpaper set: C:\\imagegen\\" in capsys.readouterr().out
    [line] = _log_lines(out_dir)
    assert " ok: " in line and "C:\\imagegen\\" in line
    assert meta["daily"]["theme"] in (Path(run["run_dir"]) / "index.html").read_text()  # in the prompt it shows
    assert meta["daily"]["theme"] in (Path(run["run_dir"]) / "index.html").read_text()  # in the prompt it shows


def test_comfyui_down_leaves_wallpaper_alone(tulu, on_c_drive, stub_calls, out_dir, capsys):
    assert imagegen.main(["daily", "--wait", "0"]) == 1  # IMAGEGEN_COMFY_URL is a closed port
    assert _wallpaper_calls(stub_calls) == []
    [line] = _log_lines(out_dir)
    assert "FAILED, wallpaper unchanged: RuntimeError: ComfyUI not reachable" in line
    assert "ComfyUI not reachable" in capsys.readouterr().err
    assert imagegen.recent_runs(1) == []


def test_missing_character_fails_before_comfyui(comfy, on_c_drive, stub_calls, out_dir):
    assert imagegen.main(["daily", "--wait", "5"]) == 1
    assert comfy.workflows() == [] and _wallpaper_calls(stub_calls) == []
    assert "FileNotFoundError: no character 'tulu'" in _log_lines(out_dir)[0]


def test_failed_job_leaves_wallpaper_alone(tulu, comfy, on_c_drive, stub_calls, out_dir):
    comfy.fail_jobs = True
    assert imagegen.main(["daily", "--wait", "5"]) == 1
    assert _wallpaper_calls(stub_calls) == []
    assert "FAILED" in _log_lines(out_dir)[0]


def test_broken_config_is_still_logged(tulu, monkeypatch, stub_calls, out_dir):
    def broken():
        raise ValueError("config.toml: bad line 3")
    monkeypatch.setattr(imagegen, "load_config", broken)
    assert daily.main(["--wait", "0"]) == 1
    assert "ValueError: config.toml: bad line 3" in _log_lines(out_dir)[0]  # IMAGEGEN_OUTPUT_DIR still found
    assert _wallpaper_calls(stub_calls) == []


def test_no_set(tulu, comfy, stub_calls, out_dir):
    # no on_c_drive: a \\wsl$ output_dir would make set_wallpaper fail, but --no-set never calls it
    assert imagegen.main(["daily", "--no-set", "--wait", "5"]) == 0
    assert _wallpaper_calls(stub_calls) == []
    assert _log_lines(out_dir)[0].endswith("(not set)")


# ---------------------------------------------------------------- windows/daily-task.sh

SCRIPT = str(REPO / "windows" / "daily-task.sh")


@pytest.fixture
def task_env(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    import sys
    (venv / "bin" / "python").symlink_to(sys.executable)
    return {**os.environ, "IMAGEGEN_VENV": str(venv), "WSL_DISTRO_NAME": "Ubuntu-24.04"}


def _ps(stub_calls) -> list[str]:
    return [c for c in stub_calls() if c.startswith("powershell.exe")]


def test_task_on(task_env, stub_calls):
    r = run_script([SCRIPT, "on", "--at", "07:15"], task_env)
    assert r.code == 0, r.out
    [call] = _ps(stub_calls)
    assert "Register-ScheduledTask -TaskName 'imagegen-daily'" in call and "-Force" in call
    assert "New-ScheduledTaskTrigger -Daily -At '07:15'" in call and "-StartWhenAvailable" in call
    assert (f"-Execute 'conhost.exe' -Argument '--headless wsl.exe -d Ubuntu-24.04 --cd {REPO} "
            f"-e {task_env['IMAGEGEN_VENV']}/bin/python {REPO}/imagegen.py daily'") in call
    assert "daily at 07:15" in r.out


@pytest.mark.parametrize("args", [["on", "--at", "25:00"], ["on", "--at"], ["on", "--bogus"], ["what"], []])
def test_task_bad_args(task_env, stub_calls, args):
    r = run_script([SCRIPT, *args], task_env)
    assert r.code == 2, r.out
    assert _ps(stub_calls) == []


def test_task_on_needs_setup(task_env, tmp_path, stub_calls):
    r = run_script([SCRIPT, "on"], {**task_env, "IMAGEGEN_VENV": str(tmp_path / "none")})
    assert r.code == 1 and "setup.sh" in r.out
    assert _ps(stub_calls) == []


def test_task_off(task_env, stub_calls):
    r = run_script([SCRIPT, "off"], {**task_env, "FAKE_POWERSHELL_OUT": "removed"})
    assert r.code == 0 and "removed" in r.out
    assert "Unregister-ScheduledTask -TaskName 'imagegen-daily' -Confirm:$false" in _ps(stub_calls)[0]
    r = run_script([SCRIPT, "off"], {**task_env, "FAKE_POWERSHELL_OUT": "absent"})
    assert r.code == 0 and "was not there" in r.out


@pytest.mark.parametrize("cmd", ["on", "off", "status", "run"])
def test_task_powershell_failure_is_reported(task_env, cmd):
    r = run_script([SCRIPT, cmd], {**task_env, "FAKE_POWERSHELL_FAIL": "1", "FAKE_POWERSHELL_OUT": "Access denied"})
    assert r.code == 1, r.out
    assert "powershell.exe failed: Access denied" in r.out
    assert "removed" not in r.out and "was not there" not in r.out and "runs daily" not in r.out


def test_task_status(task_env, out_dir):
    r = run_script([SCRIPT, "status"], {**task_env, "FAKE_POWERSHELL_OUT": "absent"})
    assert r.code == 0 and "not set up" in r.out and "No daily runs logged yet" in r.out
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "daily.log").write_text("2026-11-10 06:00:01 ok: 'x' -> C:\\a.png\n")
    r = run_script([SCRIPT, "status"], {**task_env, "FAKE_POWERSHELL_OUT": "state: Ready"})
    assert r.code == 0 and "state: Ready" in r.out and "ok: 'x' -> C:\\a.png" in r.out


def test_task_run(task_env, stub_calls):
    r = run_script([SCRIPT, "run"], task_env)
    assert r.code == 0
    assert "Start-ScheduledTask -TaskName 'imagegen-daily'" in _ps(stub_calls)[0]
