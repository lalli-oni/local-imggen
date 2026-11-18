"""#3: gen daily (theme pick, cast prompt, set wallpaper, failure leaves it alone) and windows/daily-task.sh."""
import base64
import io
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
    """Override [daily] keys for a test: daily_conf(themes=[...], cast=[]) etc. No dates or props unless given."""
    real = imagegen.load_config

    def set_(**kw):
        def load():
            conf = real()
            conf["daily"] = {**conf.get("daily", {}), "dates": {}, "props_per_day": 0, **kw}
            return conf
        monkeypatch.setattr(imagegen, "load_config", load)
    return set_


@pytest.fixture
def tulu(out_dir, make_image):
    characters.new("tulu", "lil tulu", description=TULU, refs={"v1": str(make_image("tulu.png", (600, 800)))})
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
    for day in (date(2026, 10, 3), date(2026, 11, 12), date(2026, 12, 20)):  # no [daily.dates] entry
        theme, where = daily.pick_theme(d, day)
        assert theme in daily._pool(d["months"].get(daily.MONTHS[day.month - 1]) or d["themes"])


def _simulate(d, start, days):
    """Pick a theme each day, feeding back the history like real daily runs would."""
    used, picks = {}, []
    for i in range(days):
        day = start + timedelta(days=i)
        window = {t: u for t, u in used.items() if (day - u).days < d["no_repeat_days"]}
        theme = daily.pick_theme(d, day, window)[0]
        used[theme] = day
        picks.append(theme)
    return picks


@pytest.mark.parametrize("n", [10, 8, 7])
def test_no_theme_within_7_days(daily_conf, n):
    daily_conf(themes=[f"t{i}" for i in range(n)], months={}, weekdays={})
    picks = _simulate(daily.settings(), date(2026, 1, 1), 300)
    for i, t in enumerate(picks):
        assert t not in picks[max(0, i - 6):i], (i, t)
    assert set(picks) == {f"t{i}" for i in range(n)}


def test_small_pool_rotates_least_recent(daily_conf):
    daily_conf(themes=["a", "b", "c"], months={}, weekdays={})
    picks = _simulate(daily.settings(), date(2026, 1, 1), 30)
    assert all(picks[i] == picks[i % 3] for i in range(30)) and sorted(picks[:3]) == ["a", "b", "c"]


def test_pick_is_stable_and_ignores_config_order(daily_conf):
    themes = [f"theme {i}" for i in range(5)]
    daily_conf(themes=themes, months={}, weekdays={})
    d, day = daily.settings(), date(2026, 11, 10)
    assert daily.pick_theme(d, day) == daily.pick_theme(d, day)
    daily_conf(themes=list(reversed(themes)), months={}, weekdays={})
    assert daily.pick_theme(daily.settings(), day) == daily.pick_theme(d, day)


def test_history_comes_from_past_daily_runs(tulu, daily_conf, make_run):
    daily_conf(themes=["a", "b"], months={}, weekdays={}, no_repeat_days=7)
    make_run(n=1, preset="wallpaper", prompt="x", date="2026-11-09", daily={"theme": "a"})
    make_run(n=1, preset="wallpaper", prompt="y", date="2026-11-03", daily={"theme": "b"})
    make_run(n=1, preset="draft", prompt="z", date="2026-11-08")  # not a daily
    conf = imagegen.load_config()
    assert daily.recent_themes(conf, date(2026, 11, 10), 6) == {"a": date(2026, 11, 9)}  # 11-03 is 7 days back
    assert daily.plan(date(2026, 11, 10))["theme"] == "b"
    assert daily.plan(date(2026, 11, 9))["theme"] == "a"  # only days before count: b (11-03) is 6 back


def test_month_beats_weekday_beats_themes(daily_conf):
    daily_conf(themes=["plain"], weekdays={"fri": "friday thing"}, months={"dec": ["xmas"]})
    d = daily.settings()
    assert daily.pick_theme(d, date(2026, 11, 13)) == ("friday thing", "weekdays.fri")  # a Friday
    assert daily.pick_theme(d, date(2026, 11, 12)) == ("plain", "themes")
    assert daily.pick_theme(d, date(2026, 12, 11)) == ("xmas", "months.dec")  # a Friday in December


def test_dates_beat_months(daily_conf):
    daily_conf(themes=["plain"], weekdays={}, months={"may": "spring"},
               dates={"05-04": "space hero", "2027-05-04": ["only in 2027"]})
    d = daily.settings()
    assert daily.pick_theme(d, date(2026, 5, 4)) == ("space hero", "dates.05-04")
    assert daily.pick_theme(d, date(2027, 5, 4)) == ("only in 2027", "dates.2027-05-04")
    assert daily.pick_theme(d, date(2026, 5, 5)) == ("spring", "months.may")


def test_config_dates_are_valid():
    d = daily.settings()
    assert d["dates"] and daily.pick_theme(d, date(2026, 5, 4))[1] == "dates.05-04"


@pytest.mark.parametrize("key", ["5-4", "13-01", "02-30", "2026-02-29", "may-04", "2026-5-4"])
def test_bad_date_keys(daily_conf, key):
    daily_conf(dates={key: "x"})
    with pytest.raises(ValueError, match=r"\[daily.dates\]"):
        daily.settings()


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
    daily_conf(themes=["on a beach"], months={}, weekdays={}, style="soft light", preset="wallpaper")  # text only
    p = daily.plan(date(2026, 11, 10))
    assert p["prompt"] == f"{TULU}, on a beach, soft light"
    assert p["cast"] == {"tulu": 1}


def test_cast_may_be_one_name(tulu, daily_conf):
    daily_conf(cast="tulu")
    assert daily.plan(date(2026, 11, 10))["cast"] == {"tulu": 1}


def test_show(tulu, capsys):
    assert imagegen.main(["daily", "--show", "--date", "2026-12-24"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("2026-12-24: ") and "(from dates.12-24)" in out and "tulu v1" in out
    assert "scene:   A wide, finished wallpaper image" in out and "ref:     " in out


# ---------------------------------------------------------------- runs

def test_daily_generates_and_sets_wallpaper(tulu, comfy, on_c_drive, stub_calls, out_dir, capsys, daily_conf):
    daily_conf(preset="wallpaper_sdxl")  # SDXL, text only
    assert imagegen.main(["daily", "--wait", "5"]) == 0
    [wf] = comfy.workflows()
    prompts = [n["inputs"]["text"] for n in wf.values() if n["class_type"] == "CLIPTextEncode"]
    assert any(t.startswith(TULU + ", ") for t in prompts)
    [run] = imagegen.recent_runs(1)
    assert run["preset"] == "wallpaper_sdxl" and "-daily-" in run["run_dir"]
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


def _node(wf, cls):
    return next(n["inputs"] for n in wf.values() if n["class_type"] == cls)


def test_default_preset_puts_the_cast_into_a_scene(tulu, comfy, on_c_drive, stub_calls, out_dir):
    """#52: the scene from text with a stand-in, then a Qwen edit replaces it with tulu from her ref."""
    assert daily.settings()["preset"] == "wallpaper_cast"
    assert imagegen.main(["daily", "--wait", "5"]) == 0
    [wf] = comfy.workflows()
    scene_enc, cast_enc = (n["inputs"] for n in wf.values() if n["class_type"] == "TextEncodeQwenImage21")
    scene = scene_enc["prompt"]
    assert scene.startswith("A wide, finished wallpaper image that fills the whole frame: a small cute cartoon "
                            "creature, ")
    assert "no white or empty background" in scene
    assert scene.count(imagegen.style_text(["synthwave"])) == 1  # named styles: from generate(), once
    assert imagegen.style_text(["retro_digital"]) in scene
    assert TULU not in scene and "tulu" not in scene.lower()  # the scene only has the stand-in
    cast = cast_enc["prompt"]
    assert cast.startswith("Replace the main character in image 1 with the character from image 2. ")
    assert "Keep everything else in image 1" in cast and "from head to feet" in cast
    scene_img = wf[cast_enc["images.image_1"][0]]  # image 1 = the scene pass's decoded image
    assert scene_img["class_type"] == "VAEDecodeTiled"
    assert wf[wf[scene_img["inputs"]["samples"][0]]["inputs"]["latent_image"][0]]["class_type"] == "EmptyLatentImage"
    loader = cast_enc["images.image_2"][0]
    up = wf[loader]["inputs"]["image"]
    assert up in comfy.uploads
    from PIL import Image
    assert Image.open(io.BytesIO(comfy.uploads[up])).size == imagegen.kontext_size(600, 800)  # the ref, as is
    assert (_node(wf, "ImageScale")["width"], _node(wf, "ImageScale")["height"]) == (3840, 2160)
    [run] = imagegen.recent_runs(1)
    rd = Path(run["run_dir"])
    meta = json.loads((rd / "run.json").read_text())
    assert meta["daily"]["refs"][0].endswith("refs/v1-v1.png") and meta["daily"]["cast"] == {"tulu": 1}
    assert meta["refs"][0]["file"] == "ref-1.png" and (rd / "ref-1.png").is_file()
    [im] = meta["images"]
    assert Path(im["scene"]).name.endswith("-scene.png") and Path(im["scene"]).is_file()
    assert _wallpaper_calls(stub_calls)[0].count("-scene") == 0  # the wallpaper is the cast image, not the scene
    assert len(_wallpaper_calls(stub_calls)) == 1


def test_cast_with_two_characters(tulu, comfy, daily_conf, make_image, out_dir):
    characters.new("mitts", "Mitts the cat", description="orange cat", refs={"a": str(make_image("cat.png"))})
    daily_conf(cast=["tulu", "mitts"], themes=["on a beach"], months={}, weekdays={}, stand_ins={})
    p = daily.plan(date(2026, 11, 12))
    assert [Path(r).name for r in p["refs"]] == ["v1-v1.png", "a-v1.png"]
    assert "a small cute cartoon creature on the left, another one on the right, on a beach" in p["prompt"]
    assert ("Replace the character on the left in image 1 with the character from image 2. "
            "Replace the character on the right in image 1 with the character from image 3.") \
        in p["cast_prompt"]
    assert p["cast"] == {"tulu": 1, "mitts": 1}
    run = imagegen.generate(p["prompt"], p["preset"], refs=p["refs"], cast_prompt=p["cast_prompt"])
    [wf] = comfy.workflows()
    cast_enc = [n["inputs"] for n in wf.values() if n["class_type"] == "TextEncodeQwenImage21"][1]
    loaders = [cast_enc["images.image_2"][0], cast_enc["images.image_3"][0]]
    assert len(set(loaders)) == 2 and "images.image_4" not in cast_enc
    assert all(wf[ld]["inputs"]["image"] in comfy.uploads for ld in loaders)
    assert len(comfy.uploads) == 2 and len(run["images"]) == 1


def test_cast_prompt_templates(tulu, daily_conf):
    daily_conf(themes=["on a beach"], months={}, weekdays={}, stand_in="", style="soft light")
    p = daily.plan(date(2026, 11, 12))
    assert "nobody in it yet, on a beach, soft light" in p["prompt"]
    assert p["cast_prompt"].startswith("Add lil tulu, the character from image 2, into the scene from image 1")
    daily_conf(themes=["on a beach"], months={}, weekdays={}, scene_prompt="{scene} with {stand_ins}",
               cast_prompt="{replace} ({scene})", style="", stand_ins={})
    p = daily.plan(date(2026, 11, 12))
    assert p["prompt"] == "on a beach with a small cute cartoon creature"
    assert p["cast_prompt"].endswith("image 2. (on a beach)")
    daily_conf(cast_prompt="{name}")
    with pytest.raises(ValueError, match=r"cast_prompt: only \{replace\}, \{scene\}"):
        daily.plan(date(2026, 11, 12))


def test_cast_refs_from_config(tulu, daily_conf, make_image, out_dir):
    other = make_image("other.png")
    daily_conf(ref=["tulu:v1", str(other)])
    p = daily.plan(date(2026, 11, 12))
    assert p["refs"][1] == str(other) and "with the character from image 3" in p["cast_prompt"]
    daily_conf(ref=["tulu:v1", str(other)], preset="wallpaper_ref")
    with pytest.raises(ValueError, match="takes one ref image, not 2"):
        daily.plan(date(2026, 11, 12))
    daily_conf(ref="", cast=[])
    with pytest.raises(ValueError, match="needs the cast's ref images"):
        daily.plan(date(2026, 11, 12))


def test_show_prints_scene_and_cast(tulu, capsys):
    assert imagegen.main(["daily", "--show", "--date", "2026-11-12"]) == 0
    out = capsys.readouterr().out
    assert "scene:   A wide, finished" in out and "then:    Replace the main" in out and "ref:     " in out


def test_ref_preset_draws_from_the_cast_ref(tulu, comfy, on_c_drive, stub_calls, out_dir, daily_conf):
    daily_conf(preset="wallpaper_ref")
    assert imagegen.main(["daily", "--wait", "5"]) == 0
    [wf] = comfy.workflows()
    assert _node(wf, "LoadImage")["image"] in comfy.uploads  # the ref went up as the reference
    assert _node(wf, "UNETLoader")["unet_name"] == "qwen_image_2.1_bf16.safetensors"  # Qwen since #51
    assert _node(wf, "TextEncodeQwenImage21")["images.image_1"] == [next(k for k, n in wf.items()
                                                                         if n["class_type"] == "LoadImage"), 0]
    # Qwen draws at the ref's size: the ref went up on a 16:9 canvas, and the sampler uses the encoder's latent
    from PIL import Image
    up = Image.open(io.BytesIO(comfy.uploads[_node(wf, "LoadImage")["image"]]))
    assert up.size == (1536, 864)  # 16:9, not the ref's 3:4
    enc = next(k for k, n in wf.items() if n["class_type"] == "TextEncodeQwenImage21")
    assert _node(wf, "KSampler")["latent_image"] == [enc, 2] and _node(wf, "TextEncodeQwenImage21")["resolution"] == 1152
    assert (_node(wf, "ImageScale")["width"], _node(wf, "ImageScale")["height"]) == (3840, 2160)
    text = _node(wf, "TextEncodeQwenImage21")["prompt"]
    assert text.startswith("A wide, finished wallpaper image that fills the whole frame: ")
    assert "as lil tulu: busy with the action" in text and "no white or empty background" in text
    synth = imagegen.style_text(["synthwave"])
    assert text.count(synth) == 1  # [daily] style = synthwave, and generate() adds the preset's no second time
    assert TULU not in text  # the text description (plain background, standing...) would make it copy the ref
    [run] = imagegen.recent_runs(1)
    meta = json.loads((Path(run["run_dir"]) / "run.json").read_text())
    assert meta["daily"]["ref"].endswith("refs/v1-v1.png") and meta["edit"] is True
    assert len(_wallpaper_calls(stub_calls)) == 1


def test_ref_prompt_template(tulu, daily_conf):
    daily_conf(preset="wallpaper_ref", themes=["on a beach"], months={}, weekdays={}, style="soft light", ref_prompt="{name} is here")
    assert daily.plan(date(2026, 11, 12))["prompt"] == "on a beach, soft light. lil tulu is here"
    daily_conf(preset="wallpaper_ref", themes=["on a beach"], months={}, weekdays={}, style="none-such text", ref_prompt="")
    assert daily.plan(date(2026, 11, 12))["prompt"] == "on a beach, none-such text"
    daily_conf(preset="wallpaper_ref", ref_prompt="{oops}")
    with pytest.raises(ValueError, match="only {scene} and {name}"):
        daily.plan(date(2026, 11, 12))


def test_ref_choices(tulu, daily_conf, make_image):  # wallpaper_ref: one ref
    daily_conf(preset="wallpaper_ref", ref="tulu:v1")
    assert daily.plan(date(2026, 11, 12))["ref"].endswith("refs/v1-v1.png")
    other = make_image("other.png")
    daily_conf(preset="wallpaper_ref", ref=str(other))
    assert daily.plan(date(2026, 11, 12))["ref"] == str(other)
    daily_conf(preset="wallpaper_ref", ref="tulu:nope")
    with pytest.raises(ValueError, match="no ref 'nope'"):
        daily.plan(date(2026, 11, 12))
    (imagegen.Path(imagegen.load_config()["output_dir"]) / "r").mkdir(parents=True)
    (imagegen.Path(imagegen.load_config()["output_dir"]) / "r" / "a:b.png").write_bytes(other.read_bytes())
    daily_conf(preset="wallpaper_ref", ref="r/a:b.png")  # a relative path with a colon is a file under output_dir, not CHAR:REF
    assert daily.plan(date(2026, 11, 12))["ref"].endswith("/r/a:b.png")
    daily_conf(ref="tulu:v1", preset="wallpaper")
    with pytest.raises(ValueError, match="can't use a ref"):
        daily.plan(date(2026, 11, 12))
    daily_conf(ref="tulu:v1", preset="klein")  # does both: a ref makes it draw from the ref
    assert daily.plan(date(2026, 11, 12))["ref"].endswith("refs/v1-v1.png")
    daily_conf(preset="wallpaper_ref", ref="/no/such.png")
    with pytest.raises(FileNotFoundError):
        daily.plan(date(2026, 11, 12))
    daily_conf(preset="wallpaper_ref", ref="", cast=[])
    with pytest.raises(ValueError, match="needs a reference image"):
        daily.plan(date(2026, 11, 12))


def test_character_without_ref_fails_and_is_logged(out_dir, comfy, on_c_drive, stub_calls):
    characters.new("tulu", "lil tulu", description=TULU)
    assert imagegen.main(["daily", "--wait", "5"]) == 1
    assert comfy.workflows() == [] and _wallpaper_calls(stub_calls) == []
    assert "has no ref image" in _log_lines(out_dir)[0]


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


def test_blank_image_never_becomes_the_wallpaper(tulu, comfy, on_c_drive, stub_calls, out_dir, daily_conf):
    daily_conf(preset="wallpaper_sdxl")
    comfy.blank_jobs = 99  # black even after the retry
    assert imagegen.main(["daily", "--wait", "5"]) == 1
    assert _wallpaper_calls(stub_calls) == []
    assert "every image came out blank" in _log_lines(out_dir)[0]


def test_cast_refs_mixed_with_char_refs_and_no_description(tulu, daily_conf, make_image):
    # a path takes the cast name no CHARACTER:REF entry has; a cast preset needs refs, not descriptions
    characters.new("mitts", "Mitts the cat", refs={"a": str(make_image("cat.png"))})
    daily_conf(cast=["tulu", "mitts"], ref=["mitts:a", str(make_image("t2.png"))], themes=["on a beach"],
               months={}, weekdays={}, stand_ins={}, cast_prompt="{replace} Put {images} in.")
    p = daily.plan(date(2026, 11, 12))
    assert set(p["cast"]) == {"tulu", "mitts"} and p["cast_prompt"].endswith("Put images 2 and 3 in.")


def test_cast_ref_paths_take_the_cast_names(tulu, daily_conf, make_image):
    daily_conf(ref=[str(make_image("t2.png"))], stand_in="A small fox", themes=["on a beach"], months={}, weekdays={},
               stand_ins={})
    p = daily.plan(date(2026, 11, 12))
    assert p["cast_prompt"].startswith("Replace the main character in image 1 with the character from image 2.")
    assert "A small fox, on a beach" in p["prompt"] and not p["prompt"].endswith(".")


def test_own_stand_ins(tulu, daily_conf, make_image):
    characters.new("mitts", "Mitts the cat", description="orange cat", refs={"a": str(make_image("cat.png"))})
    daily_conf(cast=["tulu", "mitts"], themes=["on a beach"], months={}, weekdays={}, stand_ins={"tulu": "a green blob"})
    p = daily.plan(date(2026, 11, 12))
    assert "frame: a green blob on the left, a small cute cartoon creature on the right, on a beach" in p["prompt"]
    assert p["cast_prompt"].startswith("Replace the character on the left in image 1 with the character from "
                                       "image 2. Replace the character on the right in image 1 with the character "
                                       "from image 3.")
    daily_conf(stand_ins="nope")
    with pytest.raises(ValueError, match="daily.stand_ins"):
        daily.plan(date(2026, 11, 12))


# ---------------------------------------------------------------- #60

def test_ref_mode_needs_no_description(out_dir, make_image, daily_conf):
    """A character made from only a ref image works with wallpaper_ref (the description is thrown away there)."""
    characters.new("refonly", "ref only", description="", refs={"v1": str(make_image("r.png", (600, 800)))})
    daily_conf(preset="wallpaper_ref", cast=["refonly"])
    p = daily.plan(date(2026, 11, 10))
    assert p["ref"] and p["cast"] == {"refonly": 1}


def test_text_mode_still_needs_a_description(out_dir, make_image, daily_conf):
    characters.new("refonly", "ref only", description="", refs={"v1": str(make_image("r.png", (600, 800)))})
    daily_conf(preset="wallpaper_sdxl", cast=["refonly"])
    with pytest.raises(ValueError, match="no description"):
        daily.plan(date(2026, 11, 10))


def test_date_preview_does_not_use_up_the_theme_today(tulu, daily_conf, make_run):
    """A run for another day (gen daily --date D) sits in today's folder; it counts on D, not today."""
    daily_conf(themes=["a", "b"], months={}, weekdays={}, no_repeat_days=7)
    today = date.today()
    far = today + timedelta(days=30)
    make_run(n=1, preset="wallpaper", prompt="x", date=today.isoformat(),
             daily={"theme": "a", "date": far.isoformat()})
    conf = imagegen.load_config()
    assert daily.recent_themes(conf, today + timedelta(days=1), 6) == {}  # not used in the week before tomorrow
    assert daily.recent_themes(conf, far + timedelta(days=1), 6) == {"a": far}  # but it counts on its own day


def test_run_made_far_ahead_still_counts(tulu, daily_conf, make_run):
    """Made 12 days before the day it is for: its folder is older than the window, but it still counts."""
    daily_conf(themes=["a", "b"], months={}, weekdays={}, no_repeat_days=7)
    make_run(n=1, preset="wallpaper", prompt="x", date="2026-11-18", daily={"theme": "a", "date": "2026-11-30"})
    conf = imagegen.load_config()
    assert daily.recent_themes(conf, date(2026, 12, 1), 6) == {"a": date(2026, 11, 30)}
