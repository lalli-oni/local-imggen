"""projects.py: versions, staleness, promote/keep, passes, seen.json, CLI."""
import json
from pathlib import Path

import pytest

import projects


@pytest.fixture
def proj(out_dir):
    projects.new("tulu", "lil tulu", "chibi cthulhu", prompt="chibi cthulhu", palette=["#AABBCC"])
    return "tulu"


def test_new_is_v1(proj, out_dir):
    p = projects.load(proj)
    assert p["version"] == 1 and p["state"]["palette"] == ["#aabbcc"]
    assert (out_dir / "projects" / "tulu" / "project.json").is_file()
    with pytest.raises(ValueError, match="already exists"):
        projects.new(proj)


@pytest.mark.parametrize("slug", ["Tulu", "a/b", "", "-x", "a b"])
def test_bad_slugs(slug):
    with pytest.raises(ValueError, match="bad project name"):
        projects.new(slug)


def test_set_state_versions_and_validation(proj):
    v = projects.set_state(proj, "bigger", params={"width": 832, "steps": 30})
    assert v["version"] == 2 and v["changed"] == ["params"]
    assert projects.load(proj)["state"]["params"] == {"width": 832, "steps": 30}
    projects.set_state(proj, "drop", params={"steps": None})
    assert projects.load(proj)["state"]["params"] == {"width": 832}
    with pytest.raises(ValueError, match="nothing changed"):
        projects.set_state(proj, params={"width": 832})
    for bad, msg in [({"params": {"width": 833}}, "multiple of 8"), ({"params": {"cfg": 99}}, "between"),
                     ({"params": {"steps": 1.5}}, "whole number"), ({"params": {"seed": 1}}, "unknown param"),
                     ({"palette": ["red"]}, "hex"), ({"preset": "nope"}, "unknown preset"),
                     ({"drop_refs": ["x"]}, "no ref")]:
        with pytest.raises(ValueError, match=msg):
            projects.set_state(proj, **bad)
    assert projects.load(proj)["version"] == 3  # failed changes commit nothing


def test_snippets_order_and_full_prompt(proj):
    projects.set_state(proj, snippets={"style": "flat vector", "bg": "white background,"})
    st = projects.load(proj)["state"]
    assert projects.full_prompt(st, "smiling") == "chibi cthulhu, flat vector, white background, smiling"


def test_refs_are_copied_per_version(proj, make_image):
    projects.set_state(proj, refs={"main": str(make_image("a.png"))})
    projects.set_state(proj, refs={"main": str(make_image("b.png", color=(1, 2, 3)))})
    p = projects.load(proj)
    d = projects.project_dir(proj)
    assert p["state"]["refs"]["main"]["file"] == "refs/main-v3.png"
    assert p["versions"][1]["state"]["refs"]["main"]["file"] == "refs/main-v2.png"  # snapshot kept
    assert (d / "refs/main-v2.png").is_file() and (d / "refs/main-v3.png").is_file()
    with pytest.raises(ValueError, match="must be"):
        projects.set_state(proj, refs={"x": str(make_image("x.gif"))})


def test_staleness(proj, make_image):
    img = make_image()
    r = projects.promote(proj, str(img), "tulu", "first pick")
    assert r == {"asset": "tulu", "asset_version": 1, "file": r["file"], "project_version": 2}
    assert not projects.summary(proj)["assets"]["tulu"]["stale"]
    projects.set_state(proj, params={"steps": 40})  # params do not change the look
    assert not projects.summary(proj)["assets"]["tulu"]["stale"]
    projects.set_state(proj, prompt="chibi cthulhu, purple")
    a = projects.summary(proj)["assets"]["tulu"]
    assert a["stale"] and a["stale_reasons"] == ["v4 changed prompt"]
    projects.keep(proj, "tulu", "still fine")
    assert not projects.summary(proj)["assets"]["tulu"]["stale"]
    with pytest.raises(ValueError, match="no asset"):
        projects.keep(proj, "nope")


def test_promote_as_ref_is_not_stale(proj, make_image):
    projects.promote(proj, str(make_image()), "tulu", as_ref="main")
    s = projects.summary(proj)
    assert not s["assets"]["tulu"]["stale"] and "main" in s["state"]["refs"]


def test_run_pass_tags_and_records(proj, comfy):
    projects.set_state(proj, params={"steps": 12})
    run = projects.run_pass(proj, count=2, extra="waving", seed=9)
    assert run["project"] == {"slug": "tulu", "version": 2}
    assert run["prompt"] == "chibi cthulhu, waving"
    ks = next(n["inputs"] for n in comfy.workflows()[0].values() if n["class_type"] == "KSampler")
    assert ks["steps"] == 12
    sidecar = json.loads(Path(run["images"][0]["path"]).with_suffix(".json").read_text())
    assert sidecar["project"] == {"slug": "tulu", "version": 2}
    assert json.loads((Path(run["run_dir"]) / "run.json").read_text())["project"]["slug"] == "tulu"
    assert "\"project\": {\"slug\": \"tulu\"" in (Path(run["run_dir"]) / "index.html").read_text()
    passes = projects.load(proj)["passes"]
    assert len(passes) == 1 and passes[0]["count"] == 2 and passes[0]["extra"] == "waving"
    # promoting a pass image records the version it was made from, not the current one
    projects.set_state(proj, prompt="other")
    r = projects.promote(proj, run["images"][0]["path"], "tulu")
    a = projects.load(proj)["assets"]["tulu"]["versions"][0]
    assert a["project_version"] == 2 and a["from_pass"] and a["seed"] == 9
    assert projects.summary(proj)["assets"]["tulu"]["stale_reasons"] == ["v3 changed prompt"]


def test_run_pass_from_ref(proj, comfy, make_image):
    projects.set_state(proj, refs={"main": str(make_image())}, params={"strength": 0.3})
    run = projects.run_pass(proj, count=1, ref="main")
    assert run["denoise"] == 0.3 and run["project"]["ref"] == "main"
    with pytest.raises(ValueError, match="no ref"):
        projects.run_pass(proj, ref="nope")
    with pytest.raises(ValueError, match="needs a ref"):
        projects.run_pass(proj, strength=0.5)


def test_summary_new_since_last_look(proj):
    first = projects.summary(proj, mark_seen="claude")
    assert "new_since_last_look" not in first
    projects.set_state(proj, "purple", prompt="purple tulu")
    s = projects.summary(proj, mark_seen="claude")
    assert [v["version"] for v in s["new_since_last_look"]["versions"]] == [2]
    assert projects.summary(proj, mark_seen="claude")["new_since_last_look"]["versions"] == []
    assert projects.load(proj)["version"] == 2  # reading never commits


def test_unknown_project_lists_known(proj):
    with pytest.raises(FileNotFoundError, match="known: tulu"):
        projects.load("nope")


def test_cli_roundtrip(out_dir, capsys, make_image):
    assert projects.main(["new", "fox", "--prompt", "a fox", "--snippet", "style=ink"]) == 0
    assert projects.main(["set", "fox", "--param", "steps=20", "--note", "faster"]) == 0
    assert "fox v2: changed params" in capsys.readouterr().out
    assert projects.main(["promote", "fox", str(make_image()), "--asset", "icon/fox"]) == 0
    assert projects.main(["status", "fox"]) == 0
    assert projects.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "icon/fox" in out and "fox" in out
    assert projects.main(["bogus"]) == 2
    with pytest.raises(ValueError, match="NAME=VALUE"):
        projects.main(["set", "fox", "--param", "steps"])
