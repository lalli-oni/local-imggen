"""Named styles (#10): [styles.NAME] in config.toml, a preset's default `styles`, --style, gen styles,
project snippets "@NAME", and gen daily's `style` naming one."""
import json
from datetime import date
from pathlib import Path

import pytest

import characters
import daily
import imagegen
import mcp_server
import projects

SYNTH = imagegen.style_text(["synthwave"])
STORY = imagegen.style_text(["storybook"])


def _texts(wf):
    return [n["inputs"]["text"] for n in wf.values() if n["class_type"] == "CLIPTextEncode"]


def _prompt(wf):
    """The positive prompt of an SDXL graph (the first CLIPTextEncode is the positive one)."""
    return _texts(wf)[0]


def test_config_styles_are_sane():
    conf = imagegen.load_config()
    assert {"synthwave", "storybook"} <= set(conf["styles"])
    for name, p in conf["presets"].items():
        imagegen.check_styles(p.get("styles", []), conf)  # every preset names known styles
    assert conf["presets"]["wallpaper"]["styles"] == ["synthwave"]
    assert conf["presets"]["wallpaper_ref"]["styles"] == ["synthwave"]
    assert not conf["presets"]["draft"].get("styles") and not conf["presets"]["icon"].get("styles")
    assert SYNTH and not SYNTH.startswith(",") and "synthwave" in SYNTH
    assert daily.settings(conf)["styles"] == ["synthwave", "retro_digital"]  # daily explores both (2026-11-17)


def test_preset_default_style(comfy):
    run = imagegen.generate("fjord", "wallpaper_sdxl", seed=1)
    wf = comfy.workflows()[0]
    preset = imagegen.list_presets()["wallpaper_sdxl"]
    assert _prompt(wf) == "fjord" + preset["suffix"] + ", " + SYNTH
    neg = imagegen.style_text(["synthwave"], key="negative_extra")
    assert _texts(wf)[1].endswith(", " + neg)  # the style's negative after the default negative
    assert run["styles"] == ["synthwave"]
    meta = json.loads(Path(run["images"][0]["path"]).with_suffix(".json").read_text())
    assert meta["styles"] == ["synthwave"] and meta["prompt"] == "fjord"
    assert json.loads((Path(run["run_dir"]) / "run.json").read_text())["styles"] == ["synthwave"]


def test_styles_replace_the_default(comfy):
    imagegen.generate("fjord", "wallpaper_sdxl", seed=1, styles=[])
    imagegen.generate("fjord", "wallpaper_sdxl", seed=1, styles=["storybook"])
    imagegen.generate("fjord", "draft", seed=1, styles=["storybook", "synthwave"])
    plain, story, both = (_prompt(wf) for wf in comfy.workflows())
    suffix = imagegen.list_presets()["wallpaper_sdxl"]["suffix"]
    assert plain == "fjord" + suffix
    assert story == "fjord" + suffix + ", " + STORY
    assert both == f"fjord, {STORY}, {SYNTH}"  # draft has no suffix; in the order given


def test_unknown_style_queues_nothing(comfy):
    with pytest.raises(ValueError, match="unknown style nope.*synthwave"):
        imagegen.generate("x", styles=["nope"])
    assert comfy.workflows() == []


def test_edit_gets_the_style_but_no_negative(comfy, make_image):
    imagegen.edit(str(make_image("src.png")), "make it wave", styles=["storybook"])
    wf = comfy.workflows()[0]
    assert any(t == "make it wave, " + STORY for t in _all_text(wf))


def _all_text(wf):
    return [v for n in wf.values() for v in n["inputs"].values() if isinstance(v, str)]


def test_vary_keeps_the_images_styles(comfy):
    run = imagegen.generate("fjord", "wallpaper_sdxl", seed=1, styles=["storybook"])
    img = run["images"][0]["path"]
    imagegen.vary(img, count=1)
    imagegen.vary(img, count=1, styles=[])
    _, kept, none = (_prompt(wf) for wf in comfy.workflows())
    assert kept.endswith(", " + STORY) and SYNTH not in kept
    assert STORY not in none and SYNTH not in none


def test_vary_on_an_image_from_before_styles_adds_none(comfy, make_image):
    src = make_image("old.png")  # its sidecar has no "styles": it was made without one
    src.with_suffix(".json").write_text(json.dumps({"prompt": "fjord", "preset": "wallpaper_sdxl"}))
    imagegen.vary(str(src), count=1)
    imagegen.vary(str(src), count=1, styles=["synthwave"])
    plain, styled = (_prompt(wf) for wf in comfy.workflows())
    assert SYNTH not in plain and styled.endswith(", " + SYNTH)


# ---------------------------------------------------------------- CLI

def test_cli_style_flags(comfy, capsys):
    assert imagegen.main(["fjord", "-p", "wallpaper_sdxl", "--style", "none", "--seed", "1"]) == 0
    assert imagegen.main(["fjord", "-p", "draft", "--style", "storybook,synthwave", "--seed", "1"]) == 0
    assert imagegen.main(["fjord", "-p", "draft", "--style", "storybook", "--style", "synthwave"]) == 0
    plain, a, b = (_prompt(wf) for wf in comfy.workflows())
    assert SYNTH not in plain and a == b == f"fjord, {STORY}, {SYNTH}"


def test_gen_styles_and_recent(comfy, capsys):
    assert imagegen.main(["styles"]) == 0
    out = capsys.readouterr().out
    assert "synthwave" in out and "wallpaper_ref" in out and SYNTH in out
    assert imagegen.main(["presets"]) == 0
    assert "[style: synthwave]" in capsys.readouterr().out
    imagegen.generate("fjord", "draft", seed=1, styles=["storybook"])
    assert imagegen.main(["recent", "1"]) == 0
    assert capsys.readouterr().out.startswith("draft+storybook ")


def test_recent_runs_without_styles(make_run):
    make_run()  # a run made before styles: no "styles" in run.json
    assert imagegen.recent_runs(1)[0]["styles"] == []


# ---------------------------------------------------------------- projects

@pytest.fixture
def proj(out_dir):
    projects.new("fjords", prompt="a fjord", preset="wallpaper_sdxl")
    return "fjords"


def test_project_snippet_copies_a_style(proj):
    projects.set_state(proj, snippets={"style": "@storybook"})
    assert projects.load(proj)["state"]["snippets"]["style"] == STORY
    with pytest.raises(ValueError, match="unknown style nope"):
        projects.set_state(proj, snippets={"style": "@nope"})
    projects.set_state(proj, snippets={"at": "@", "handle": "@ the beach"})  # not a style name: kept as text
    assert projects.load(proj)["state"]["snippets"] == {"style": STORY, "at": "@", "handle": "@ the beach"}


def test_project_pass_has_no_preset_style(proj, comfy):
    projects.set_state(proj, snippets={"style": "@storybook"})
    projects.run_pass(proj, count=1)
    prompt = _prompt(comfy.workflows()[0])
    assert prompt.startswith(f"a fjord, {STORY}") and SYNTH not in prompt  # the project's look, once


# ---------------------------------------------------------------- daily

TULU = "lil tulu, a tiny green chibi cthulhu, plain white background"


@pytest.fixture
def tulu(out_dir, make_image):
    characters.new("tulu", "lil tulu", description=TULU, refs={"v1": str(make_image("tulu.png", (600, 800)))})


@pytest.fixture
def daily_conf(monkeypatch):
    real = imagegen.load_config

    def set_(**kw):
        def load():
            conf = real()
            conf["daily"] = {**conf.get("daily", {}), "dates": {}, "months": {}, "weekdays": {},
                             "themes": ["on a beach"], "ref_prompt": "", **kw}
            return conf
        monkeypatch.setattr(imagegen, "load_config", load)
    return set_


@pytest.mark.parametrize("style, text, names", [
    ("synthwave", SYNTH, ["synthwave"]),
    ("storybook, synthwave", f"{STORY}, {SYNTH}", ["storybook", "synthwave"]),
    ("", SYNTH, ["synthwave"]),  # wallpaper_ref's own styles
    ("soft light", "soft light", []),  # literal text, the old way
])
def test_daily_style(tulu, daily_conf, style, text, names):
    daily_conf(style=style)
    p = daily.plan(date(2026, 11, 10))
    assert p["prompt"] == f"on a beach, {text}" and p["styles"] == names


@pytest.mark.parametrize("style", ["synthwav", "synthwave, extra glow"])
def test_daily_style_typo_or_mix(tulu, daily_conf, style):
    daily_conf(style=style)
    with pytest.raises(ValueError, match="use style names"):
        daily.plan(date(2026, 11, 10))


def test_daily_unknown_preset_style(tulu, daily_conf, monkeypatch):
    real = imagegen.load_config

    def load():
        conf = real()
        conf["presets"]["wallpaper_ref"]["styles"] = ["gone"]
        conf["daily"] = {**conf["daily"], "style": ""}
        return conf
    monkeypatch.setattr(imagegen, "load_config", load)
    with pytest.raises(ValueError, match="unknown style gone"):
        daily.plan(date(2026, 11, 10))


def test_daily_run_adds_the_style_once(tulu, comfy, daily_conf, out_dir):
    daily_conf(style="synthwave", preset="wallpaper_sdxl")  # text only: generate() adds the style
    assert SYNTH not in daily.plan(date(2026, 11, 10))["prompt"]
    run = daily.run(date(2026, 11, 10), wait=1, set_wallpaper=False)
    prompt, negative = _texts(comfy.workflows()[0])[:2]
    assert prompt.count(SYNTH) == 1
    assert negative.endswith(imagegen.style_text(["synthwave"], key="negative_extra"))  # the style's negative too
    meta = json.loads((Path(run["run_dir"]) / "run.json").read_text())
    assert meta["daily"]["styles"] == ["synthwave"]


# ---------------------------------------------------------------- MCP

def test_mcp_styles(comfy):
    assert mcp_server.list_styles()["synthwave"]["text"] == SYNTH
    presets = mcp_server.list_presets()
    assert presets["wallpaper"]["styles"] == ["synthwave"] and "styles" not in presets["draft"]
    out = mcp_server.generate("a fox", seed=1, styles=["storybook"], preview=False)
    assert out[0]["status"] == "done"
    assert _prompt(comfy.workflows()[0]) == f"a fox, {STORY}"
    mcp_server.vary(out[0]["images"][0]["path"], count=1, styles=[], preview=False)
    assert STORY not in _prompt(comfy.workflows()[1])
