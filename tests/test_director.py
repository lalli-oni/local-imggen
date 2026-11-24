"""#94: the Claude prompt pass (director.py): the theme rewrite in gen daily, gen ideas, and every way the CLI can fail."""
import json
from pathlib import Path
from datetime import date

import pytest

import characters
import daily
import director
import doctor
import imagegen
from test_daily import daily_conf, on_c_drive, tulu  # noqa: F401  (fixtures)

THEME = "robbing a neon casino vault, a neon sign reads 'SQUID PRO QUO'"


def _prompt(tmp_path) -> str:
    return (tmp_path / "stub-calls.log.claude-prompt").read_text()


# ---------------------------------------------------------------- the CLI call

def test_direct_returns_scene_gags_layout(tmp_path):
    r = director.direct(THEME, ["the main character"], model="haiku")
    assert "SQUID PRO QUO" in r["scene"] and r["gags"] == ["the flamingo snores"] and r["layout"]
    assert "Theme: " + THEME in _prompt(tmp_path) and "the main character" in _prompt(tmp_path)


def test_cli_gets_no_tools_no_session_and_the_model(tmp_path):
    director.direct(THEME, ["the main character"], model="haiku")
    args = (tmp_path / "stub-calls.log.claude-args").read_text().split()
    assert args[args.index("--tools") + 1] == "--no-session-persistence"  # --tools "" (an empty argument)
    assert "-p" in args and "--model" in args and args[args.index("--model") + 1] == "haiku"
    assert "--output-format" in args and "json" in args


@pytest.mark.parametrize("mode,why", [("fail", "claude failed"), ("text", "not a JSON object"),
                                      ("nosign", "lost the sign text 'SQUID PRO QUO'")])
def test_bad_answers_raise_unavailable(monkeypatch, mode, why):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(director.Unavailable, match=why):
        director.direct(THEME, ["the main character"])


def test_missing_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert not director.available()
    with pytest.raises(director.Unavailable, match="claude --version"):
        director.direct(THEME, ["x"])


def test_timeout(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    with pytest.raises(director.Unavailable, match="did not answer within"):
        director.direct(THEME, ["x"], timeout=0.3)


def test_signs_keep_apostrophes_inside_a_sign():
    assert director.signs("a billboard reads 'CAN'T STOP, WON'T STOP' over a road") == ["CAN'T STOP, WON'T STOP"]
    assert director.signs("no text here, just a duck") == []


def test_scene_too_long_is_rejected():
    with pytest.raises(director.Unavailable, match="max"):
        director.check_scene("x", {"scene": "word " * 400})


# ---------------------------------------------------------------- gen daily

def _directed(daily_conf, **kw):
    daily_conf(themes=[THEME], months={}, weekdays={}, cast=["tulu"], preset="wallpaper_cast",
               director={"enabled": True, **kw})


def test_off_by_default_never_calls_claude(tulu, daily_conf, stub_calls):
    daily_conf(themes=[THEME], months={}, weekdays={}, cast=["tulu"], preset="wallpaper_cast")
    p = daily.plan(date(2026, 11, 10))
    assert p["director"] is None and "SQUID PRO QUO" in p["prompt"] and not [c for c in stub_calls() if c.startswith("claude")]


def test_plan_uses_the_rewritten_scene(tulu, daily_conf, stub_calls):
    _directed(daily_conf)
    p = daily.plan(date(2026, 11, 10))
    assert "squid in a tuxedo" in p["prompt"] and "a small cute cartoon creature, robbing a neon casino vault with a squid" in p["prompt"]
    assert p["director"]["layout"] == "vault left, guard in front" and p["director"]["gags"]


def test_plan_asks_once_per_day_and_theme(tulu, daily_conf, stub_calls):
    _directed(daily_conf)
    daily.plan(date(2026, 11, 10)); daily.plan(date(2026, 11, 10))  # --show then the real run
    assert len([c for c in stub_calls() if c.startswith("claude -p")]) == 1
    daily.plan(date(2026, 11, 11))  # another day: asked again
    assert len([c for c in stub_calls() if c.startswith("claude -p")]) == 2


def test_failure_falls_back_to_the_plain_theme_and_logs(tulu, daily_conf, monkeypatch, out_dir):
    _directed(daily_conf)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    p = daily.plan(date(2026, 11, 10))
    assert "SQUID PRO QUO" in p["prompt"] and "claude failed" in p["director"]["error"]
    assert "director skipped" in (out_dir / "daily.log").read_text()


def test_no_director_flag_skips_it(tulu, daily_conf, stub_calls):
    _directed(daily_conf)
    p = daily.plan(date(2026, 11, 10), use_director=False)
    assert p["director"] is None and not [c for c in stub_calls() if c.startswith("claude")]


def test_run_saves_the_director_result_in_run_json(tulu, daily_conf, comfy, on_c_drive):
    _directed(daily_conf)
    r = daily.run(date(2026, 11, 10), wait=2, set_wallpaper=False)
    meta = json.loads((Path(r["run_dir"]) / "run.json").read_text())
    assert meta["daily"]["director"]["layout"] == "vault left, guard in front"


def test_roles_for_a_cast_of_two(daily_conf):
    daily_conf(cast=["a", "b"], preset="wallpaper")
    assert daily.roles(daily.settings()) == ["the character on the left", "the character on the right"]


# ---------------------------------------------------------------- gen ideas

def test_ideas_are_saved_and_not_repeated(daily_conf, capsys):
    conf = imagegen.load_config()
    assert director.main(["-n", "5"]) == 0
    assert director.load_ideas(conf) == ["roller-skating through a neon mall, a sign reads 'WHEEL DEAL'",
                                         "brewing coffee on Mars, a sign reads 'MARS BARISTA'"]
    assert director.main(["-n", "5"]) == 1  # the stub answers the same two again: nothing new
    assert "no new themes" in capsys.readouterr().err
    assert len(director.load_ideas(conf)) == 2


def test_ideas_failure_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    assert director.main([]) == 1 and "gen ideas:" in capsys.readouterr().err


def test_ideas_join_the_pool_only_when_used(daily_conf):
    director.make_ideas(2, imagegen.load_config())
    daily_conf(themes=["plain"], ideas={"use": False})
    assert daily.settings()["themes"] == ["plain"]
    daily_conf(themes=["plain"], ideas={"use": True})
    assert len(daily.settings()["themes"]) == 3


def test_ideas_list(capsys):
    director.main(["--list"])
    assert "no ideas yet" in capsys.readouterr().out


# ---------------------------------------------------------------- doctor

def test_doctor_checks_claude_only_when_used():
    assert doctor.check_claude_cli({"daily": {}}) is None
    assert doctor.check_claude_cli({"daily": {"director": {"enabled": True}}})["status"] == "ok"


def test_doctor_warns_without_the_cli(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert doctor.check_claude_cli({"daily": {"ideas": {"use": True}}}) is None  # only gen ideas needs it
    c = doctor.check_claude_cli({"daily": {"director": {"enabled": True}}})
    assert c["status"] == "warn" and "claude --version" in c["fix"]


# ---------------------------------------------------------------- hardening (code review)

@pytest.mark.parametrize("bad", [{"scene": "x y", "gags": 3}, {"scene": "x y", "gags": "ha", "layout": 5}])
def test_odd_gags_and_layout_types_do_not_crash(bad):
    r = director.check_scene("plain", bad)
    assert r["gags"] == [] and r["layout"] == ""


@pytest.mark.parametrize("junk", ["[]", "null", '"text"', "7"])
def test_damaged_cache_is_asked_again(tmp_path, junk):
    conf, day, who = imagegen.load_config(), date(2026, 11, 10), ["the main character"]
    f = director.cache_file(conf, day, THEME, who)
    f.parent.mkdir(parents=True); f.write_text(junk)
    assert "SQUID PRO QUO" in director.direct_cached(conf, day, THEME, who)["scene"]
    assert "SQUID PRO QUO" in json.loads(f.read_text())["scene"]  # overwritten


def test_themes_must_be_a_list(monkeypatch):
    monkeypatch.setattr(director, "ask", lambda *a, **k: {"themes": "some text"})
    with pytest.raises(director.Unavailable, match="no list of themes"):
        director.make_ideas(3, imagegen.load_config())
    assert director.load_ideas(imagegen.load_config()) == []


def test_apostrophe_in_a_word_does_not_hide_the_sign():
    assert director.signs("Tulu's heist, a sign reads 'SQUID PRO QUO'") == ["SQUID PRO QUO"]
    assert director.signs("a sign reads 'Hot Tub, Hotter Lava'.") == ["Hot Tub, Hotter Lava"]


def test_one_string_theme_in_config_is_fine_for_ideas(daily_conf):
    daily_conf(themes="a single theme")
    conf = imagegen.load_config()
    assert director.make_ideas(5, conf)
