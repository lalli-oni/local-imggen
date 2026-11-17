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
                             "themes": ["on a beach"], "ref_prompt": "", "props_per_day": 0, **kw}
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


# ---------------------------------------------------------------- crt post step and daily props

def _gradient(path, size=(320, 180), mode="RGB"):
    from PIL import Image
    im = Image.new(mode, size)
    im.putdata([(x * 255 // size[0], y * 255 // size[1], 128) + ((200,) if mode == "RGBA" else ())
                for y in range(size[1]) for x in range(size[0])])
    im.save(path)
    return path


def test_crt_writes_a_copy_and_never_stacks(tmp_path):
    from PIL import Image, ImageChops
    src = _gradient(tmp_path / "a.png")
    before = src.read_bytes()
    out = imagegen.crt(src)
    assert out == tmp_path / "a-crt.png" and src.read_bytes() == before  # the clean image stays
    first = Image.open(out).convert("RGB")
    assert first.size == (320, 180) and ImageChops.difference(first, Image.open(src)).getbbox()
    again = imagegen.crt(out)  # from the -crt.png: starts over from the clean one
    assert again == out and not ImageChops.difference(Image.open(again).convert("RGB"), first).getbbox()


def test_crt_knobs(tmp_path):
    from PIL import Image, ImageChops
    src = _gradient(tmp_path / "a.png")
    off = {k: 0 for k in imagegen.CRT_DEFAULTS}
    out = imagegen.crt(src, **off)  # every knob at 0: the image as it was
    assert not ImageChops.difference(Image.open(out).convert("RGB"), Image.open(src).convert("RGB")).getbbox()
    lines = Image.open(imagegen.crt(src, **{**off, "lines": 18, "scanline": 0.5})).convert("L")
    col = [lines.getpixel((100, y)) for y in range(20)]
    assert max(col) - min(col) > 30  # dark gaps every 10 px (18 lines over 180 px)
    with pytest.raises(ValueError, match="unknown sharpness"):
        imagegen.crt_settings(sharpness=1)


def test_crt_keeps_transparency(tmp_path):
    from PIL import Image
    out = imagegen.crt(_gradient(tmp_path / "a.png", mode="RGBA"))
    im = Image.open(out)
    assert im.mode == "RGBA" and im.getchannel("A").getextrema() == (200, 200)


def test_crt_config_overrides(monkeypatch):
    real = imagegen.load_config
    monkeypatch.setattr(imagegen, "load_config", lambda: {**real(), "crt": {"lines": 240}})
    s = imagegen.crt_settings(scanline=0.5)
    assert s["lines"] == 240 and s["scanline"] == 0.5 and s["blur"] == imagegen.CRT_DEFAULTS["blur"]


def test_style_post_step_runs_after_generate(comfy):
    assert imagegen.list_styles()["retro_digital"]["post"] == ["crt"]
    run = imagegen.generate("fjord", "draft", seed=1, styles=["retro_digital"])
    im = Path(run["images"][0]["path"])
    assert im.name.endswith("-crt.png") and im.is_file()
    assert im.with_name(im.name.replace("-crt", "")).is_file()  # the clean one too
    meta = json.loads(im.with_name(im.name.replace("-crt.png", ".json")).read_text())
    assert meta["file"] == im.name and "crt_s" in meta["timing"]
    assert run["transparent"] is False
    imagegen.generate("fjord", "draft", seed=1, styles=[])
    assert comfy.workflows()  # and without the style: no crt
    assert not imagegen.recent_runs(1)[0]["images"][0].endswith("-crt.png")


def test_unknown_post_step_in_a_style(monkeypatch):
    real = imagegen.load_config

    def load():
        conf = real()
        conf["styles"]["broken"] = {"suffix": "x", "post": ["sparkle"]}
        return conf
    monkeypatch.setattr(imagegen, "load_config", load)
    with pytest.raises(ValueError, match="unknown post step sparkle"):
        imagegen.style_post(["broken"])


def test_gen_crt_cli(tmp_path, capsys):
    src = _gradient(tmp_path / "a.png")
    assert imagegen.main(["crt", str(src), "--lines", "90"]) == 0
    assert (tmp_path / "a-crt.png").is_file()
    assert imagegen.main(["crt", str(tmp_path / "missing.png")]) == 1


def test_daily_ref_run_gets_the_styles_post_steps(tulu, daily_conf):
    daily_conf(style="synthwave, retro_digital")
    p = daily.plan(date(2026, 11, 10))
    assert p["ref"] and p["gen_styles"] == [] and p["post"] == ["crt"]
    daily_conf(style="synthwave, retro_digital", preset="wallpaper_sdxl")  # text only: generate() adds them
    p = daily.plan(date(2026, 11, 10))
    assert p["gen_styles"] == ["synthwave", "retro_digital"] and p["post"] == []


PROPS = {"tech": ["a boombox", "a Walkman"], "cars": ["a DeLorean"], "wear": ["a visor", "LED sneakers"],
         "empty": []}


def test_daily_props(tulu, daily_conf):
    daily_conf(props=PROPS, props_per_day=2, style="soft light", ref_prompt="")
    p = daily.plan(date(2026, 11, 10))
    assert len(p["props"]) == 2
    cats = {c for item in p["props"] for c, items in PROPS.items() if item in items}
    assert len(cats) == 2  # from different categories
    assert p["prompt"] == f"on a beach, with {p['props'][0]} and {p['props'][1]} in the scene, soft light"
    assert daily.plan(date(2026, 11, 10))["props"] == p["props"]  # the date decides
    picks = {tuple(daily.plan(date(2026, 11, d))["props"]) for d in range(1, 29)}
    assert len(picks) > 3
    daily_conf(props=PROPS, props_per_day=9, style="soft light", ref_prompt="")
    assert len(daily.plan(date(2026, 11, 10))["props"]) == 3  # at most one per non-empty category
    daily_conf(props=PROPS, props_per_day=0, style="soft light", ref_prompt="")
    assert daily.plan(date(2026, 11, 10))["prompt"] == "on a beach, soft light"
    daily_conf(props={"tech": "a boombox"})
    with pytest.raises(ValueError, match="daily.props"):
        daily.plan(date(2026, 11, 10))


def test_real_config_props_and_show(tulu, capsys):
    p = daily.plan(date(2026, 11, 10))
    assert len(p["props"]) == 3 and all(x in p["prompt"] for x in p["props"])
    assert imagegen.main(["daily", "--show", "--date", "2026-11-10"]) == 0
    assert "props:   " in capsys.readouterr().out


def test_daily_run_records_props(tulu, comfy, daily_conf):
    daily_conf(props=PROPS, props_per_day=1, style="soft light", preset="wallpaper_sdxl")
    run = daily.run(date(2026, 11, 10), wait=1, set_wallpaper=False)
    meta = json.loads((Path(run["run_dir"]) / "run.json").read_text())
    assert len(meta["daily"]["props"]) == 1 and meta["daily"]["theme"] == "on a beach"  # theme without props
