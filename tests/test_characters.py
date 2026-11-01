"""#46: shared characters, casting, per-character staleness, project docs and import."""
import json
from pathlib import Path

import pytest

import characters
import mcp_server
import projects

TULU = "chibi green cthulhu, amber eyes"


@pytest.fixture
def tulu(out_dir):
    characters.new("tulu", "lil tulu", description=TULU, palette=["#3A7"])
    return "tulu"


@pytest.fixture
def walls(tulu):
    projects.new("walls", prompt="misty fjord at dawn, painterly", cast={"tulu": None})
    return "walls"


def _asset(slug, name):
    return projects.summary(slug)["assets"][name]


# ---------------------------------------------------------------- characters

def test_character_versions_and_validation(tulu, make_image):
    c = characters.load(tulu)
    assert c["version"] == 1 and c["state"]["palette"] == ["#3a7"]
    v = characters.set_state(tulu, "glossier", description=TULU + ", glossy")
    assert v["version"] == 2 and v["changed"] == ["description"]
    assert characters.state_at(characters.load(tulu), 1)["description"] == TULU
    characters.set_state(tulu, refs={"main": str(make_image("a.png"))})
    characters.set_state(tulu, refs={"main": str(make_image("b.png", color=(1, 2, 3)))})
    c = characters.load(tulu)
    assert c["state"]["refs"]["main"]["file"] == "refs/main-v4.png"
    assert (characters.char_dir(tulu) / "refs/main-v3.png").is_file()  # older version keeps its file
    for bad, msg in [({"palette": ["green"]}, "hex"), ({"drop_refs": ["x"]}, "no ref"),
                     ({"description": TULU + ", glossy"}, "nothing changed")]:
        with pytest.raises(ValueError, match=msg):
            characters.set_state(tulu, **bad)
    with pytest.raises(ValueError, match="already exists"):
        characters.new(tulu)
    with pytest.raises(ValueError, match="no v9"):
        characters.state_at(c, 9)
    with pytest.raises(FileNotFoundError, match="known: tulu"):
        characters.load("nope")
    with pytest.raises(ValueError, match="bad character name"):
        characters.new("Tulu")


def test_cast_validation(walls, tulu):
    for bad, err in [({"cast": {"nope": None}}, FileNotFoundError), ({"cast": {"tulu": 7}}, ValueError),
                     ({"uncast": ["mango"]}, ValueError)]:
        with pytest.raises(err):
            projects.set_state(walls, **bad)
    v = projects.set_state(walls, cast={"tulu": 1})
    assert v["changed"] == ["cast"]
    assert projects.load(walls)["state"]["cast"] == {"tulu": {"pin": 1}}


def test_old_project_json_still_loads(out_dir):
    projects.new("old", prompt="x")
    f = projects.project_dir("old") / "project.json"
    p = json.loads(f.read_text())
    del p["state"]["cast"], p["state"]["docs"]
    f.write_text(json.dumps(p))
    s = projects.summary("old")
    assert s["state"]["cast"] == {} and s["state"]["docs"] == {}


# ---------------------------------------------------------------- passes

def test_pass_puts_cast_first_and_tags_versions(walls, comfy):
    characters.new("mango", description="orange tabby cat in sunglasses")
    projects.set_state(walls, cast={"mango": None})
    run = projects.run_pass(walls, count=1, extra="sunrise")
    assert run["prompt"] == f"{TULU}, orange tabby cat in sunglasses, misty fjord at dawn, painterly, sunrise"
    assert run["project"]["chars"] == {"tulu": 1, "mango": 1}
    sidecar = json.loads(Path(run["images"][0]["path"]).with_suffix(".json").read_text())
    assert sidecar["project"]["chars"] == {"tulu": 1, "mango": 1}
    assert projects.load(walls)["passes"][-1]["chars"] == {"tulu": 1, "mango": 1}
    assert projects.run_pass(walls, count=1, chars=["mango"])["prompt"].startswith("orange tabby")
    none = projects.run_pass(walls, count=1, chars=[])
    assert none["prompt"] == "misty fjord at dawn, painterly" and "chars" not in none["project"]
    with pytest.raises(ValueError, match="not cast in this project: rex"):
        projects.run_pass(walls, chars=["rex"])


def test_pin_draws_the_pinned_description(walls, comfy):
    characters.set_state("tulu", description="purple tulu")
    projects.set_state(walls, cast={"tulu": 1})
    run = projects.run_pass(walls, count=1)
    assert run["prompt"].startswith(TULU) and run["project"]["chars"] == {"tulu": 1}
    c = projects.summary(walls)["state"]["cast"]["tulu"]
    assert c["version"] == 1 and c["latest"] == 2 and c["pin"] == 1


def test_pass_from_character_ref(walls, comfy, make_image):
    characters.set_state("tulu", refs={"main": str(make_image())})
    run = projects.run_pass(walls, count=1, ref="tulu:main", strength=0.3)
    assert run["denoise"] == 0.3 and run["project"]["ref"] == "tulu:main"
    assert comfy.uploads  # the character ref was sent as the source image
    with pytest.raises(ValueError, match="has no ref 'x'"):
        projects.run_pass(walls, ref="tulu:x")
    with pytest.raises(ValueError, match="not cast"):
        projects.run_pass(walls, ref="mango:main")


# ---------------------------------------------------------------- staleness

def test_character_change_stales_its_assets_in_every_project(walls, tulu, comfy, make_image):
    projects.new("sheet", prompt="character sheet", cast={"tulu": None})
    characters.new("mango", description="orange cat")
    projects.set_state(walls, cast={"mango": None})
    run = projects.run_pass(walls, count=1, chars=["tulu"])
    projects.promote(walls, run["images"][0]["path"], "fjord")  # shows tulu (from its pass)
    projects.promote(walls, str(make_image()), "plain", shows=[])  # shows nobody
    projects.promote(walls, str(make_image("m.png")), "cat", shows=["mango"])
    projects.promote("sheet", str(make_image("s.png")), "pose", shows=["tulu"])
    assert _asset(walls, "fjord")["shows"] == {"tulu": 1}
    assert not any(a["stale"] for a in projects.summary(walls)["assets"].values())

    characters.set_state(tulu, "glossy", description=TULU + ", glossy")
    assert _asset(walls, "fjord")["stale_reasons"] == ["tulu v2 changed description"]
    assert _asset("sheet", "pose")["stale_reasons"] == ["tulu v2 changed description"]
    assert not _asset(walls, "plain")["stale"] and not _asset(walls, "cat")["stale"]
    assert characters.summary(tulu)["used_by"] == {"walls": {"pin": None, "assets": ["fjord"]},
                                                   "sheet": {"pin": None, "assets": ["pose"]}}

    projects.keep(walls, "fjord", "fine")
    assert not _asset(walls, "fjord")["stale"]
    characters.set_state(tulu, palette=["#000"])
    assert _asset(walls, "fjord")["stale_reasons"] == ["tulu v3 changed palette"]


def test_pinned_character_does_not_stale_until_the_pin_moves(walls, make_image):
    projects.set_state(walls, cast={"tulu": 1})
    projects.promote(walls, str(make_image()), "fjord", shows=["tulu"])
    characters.set_state("tulu", description="purple tulu")
    assert not _asset(walls, "fjord")["stale"]
    projects.set_state(walls, cast={"tulu": None})
    assert _asset(walls, "fjord")["stale_reasons"] == ["tulu v2 changed description"]


def test_making_the_asset_a_character_ref_is_not_stale(walls, make_image):
    r = projects.promote(walls, str(make_image()), "tulu-front", shows=["tulu"])
    characters.set_state("tulu", refs={"front": r["file"]})
    assert not _asset(walls, "tulu-front")["stale"]
    characters.set_state("tulu", refs={"side": str(make_image("other.png", color=(9, 9, 9)))})
    assert _asset(walls, "tulu-front")["stale_reasons"] == ["tulu v3 changed refs"]


def test_promote_shows_validation(walls, make_image):
    with pytest.raises(FileNotFoundError, match="no character 'rex'"):
        projects.promote(walls, str(make_image()), "x", shows=["rex"])


# ---------------------------------------------------------------- docs

def test_docs(walls, tmp_path):
    md = tmp_path / "lore.md"
    md.write_text("# Lore\nTulu lives in a fjord.")
    v = projects.set_state(walls, docs={"lore": str(md)})
    assert v["changed"] == ["docs"]
    assert projects.read_doc(walls, "lore").startswith("# Lore")
    md.write_text("# Lore v2")
    projects.set_state(walls, docs={"lore": str(md)})
    assert projects.read_doc(walls, "lore") == "# Lore v2"
    assert (projects.project_dir(walls) / "docs/lore-v2.md").is_file()  # the old version stays
    s = projects.summary(walls)["state"]["docs"]["lore"]
    assert s["file"] == "docs/lore-v3.md" and Path(s["path"]).is_file()
    bad = tmp_path / "x.pdf"
    bad.write_text("x")
    with pytest.raises(ValueError, match="docs must be"):
        projects.set_state(walls, docs={"x": str(bad)})
    with pytest.raises(ValueError, match="no doc 'nope'"):
        projects.read_doc(walls, "nope")
    projects.set_state(walls, drop_docs=["lore"])
    assert projects.load(walls)["state"]["docs"] == {}


# ---------------------------------------------------------------- import

@pytest.fixture
def folder(tmp_path, make_image):
    from PIL import Image
    root = tmp_path / "Radical Sabbatical"
    (root / "Backgrounds").mkdir(parents=True)
    Image.new("RGB", (64, 64), (200, 0, 200)).save(root / "Backgrounds" / "Neon City 01.png")
    Image.new("RGB", (64, 64), (0, 200, 200)).save(root / "hero.jpg")
    (root / "Characters.md").write_text("# Cats\nMango: orange tabby")
    (root / "notes.pdf").write_text("x")
    (root / ".DS_Store").write_text("x")
    return root


def test_import_dry_run_writes_nothing(out_dir, folder):
    plan = projects.import_folder("rad", str(folder))
    assert plan["assets"] == {"backgrounds/neon-city-01": [str(folder / "Backgrounds/Neon City 01.png")],
                              "hero": [str(folder / "hero.jpg")]}
    assert plan["docs"] == {"characters": str(folder / "Characters.md")}
    assert plan["skipped"] == [str(folder / "notes.pdf")] and plan["new_project"]
    assert projects.list_projects() == []


def test_import_apply(tulu, folder):
    plan = projects.import_folder("rad", str(folder), apply=True, shows=["tulu"], title="Radical Sabbatical")
    p = projects.load("rad")
    assert p["title"] == "Radical Sabbatical" and p["version"] == plan["project_version"] == 2
    assert p["versions"][-1]["changed"] == ["docs", "assets"]
    assert projects.read_doc("rad", "characters").startswith("# Cats")
    a = projects.summary("rad")["assets"]["backgrounds/neon-city-01"]
    assert a["imported"] and a["shows"] == {"tulu": 1} and not a["stale"] and Path(a["file"]).is_file()
    # importing again adds versions instead of replacing
    plan = projects.import_folder("rad", str(folder), apply=True)
    assert plan["existing_assets"] == ["backgrounds/neon-city-01", "hero"]
    assert projects.summary("rad")["assets"]["hero"]["current"] == "v2"


def test_import_errors(out_dir, tmp_path):
    with pytest.raises(FileNotFoundError, match="no folder"):
        projects.import_folder("rad", str(tmp_path / "missing"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="nothing to import"):
        projects.import_folder("rad", str(tmp_path / "empty"))


# ---------------------------------------------------------------- migration

def test_from_project_moves_prompt_and_keeps_assets_fresh(out_dir, comfy, make_image):
    projects.new("tulu", "lil tulu", prompt=TULU, palette=["#123456"], refs={"main": str(make_image())})
    projects.set_state("tulu", snippets={"style": "flat"})
    before = projects.run_pass("tulu", count=1)["prompt"]
    projects.promote("tulu", str(make_image("p.png")), "tulu")
    characters.from_project("tulu")
    s = projects.summary("tulu")
    assert s["state"]["prompt"] == "" and s["state"]["refs"] == {} and "tulu" in s["state"]["cast"]
    assert s["state"]["full_prompt"] == before
    assert s["assets"]["tulu"]["shows"] == {"tulu": 1} and not s["assets"]["tulu"]["stale"]
    c = characters.load("tulu")
    assert c["state"]["description"] == TULU and c["state"]["palette"] == ["#123456"]
    assert (characters.char_dir("tulu") / c["state"]["refs"]["main"]["file"]).is_file()
    assert projects.run_pass("tulu", count=1)["prompt"] == before
    characters.set_state("tulu", description="purple tulu")
    assert projects.summary("tulu")["assets"]["tulu"]["stale"]
    with pytest.raises(ValueError, match="already casts"):
        characters.from_project("tulu")


# ---------------------------------------------------------------- CLI + MCP

def test_cli(out_dir, comfy, folder, capsys):
    assert characters.main(["new", "tulu", "--desc", TULU]) == 0
    assert characters.main(["set", "tulu", "--palette", "#111", "--note", "darker"]) == 0
    assert "tulu v2: changed palette" in capsys.readouterr().out
    assert characters.main(["list"]) == 0 and characters.main(["status", "tulu"]) == 0
    assert projects.main(["new", "walls", "--prompt", "fjord", "--cast", "tulu@1"]) == 0
    assert "cast tulu (v1, pinned (latest v2))" in capsys.readouterr().out
    assert projects.main(["gen", "walls", "-n", "1", "--char", "tulu"]) == 0
    assert projects.main(["import", "walls", str(folder), "--shows", "tulu"]) == 0
    out = capsys.readouterr().out
    assert "would import into project walls" in out and "dry run" in out and "--yes" in out
    assert projects.main(["import", "walls", str(folder), "--shows", "tulu", "--yes"]) == 0
    assert projects.main(["doc", "walls", "characters"]) == 0
    assert "Mango" in capsys.readouterr().out
    assert projects.main(["set", "walls", "--uncast", "tulu", "--doc", str(folder / "Characters.md")]) == 0
    assert projects.main(["status", "walls"]) == 0
    out = capsys.readouterr().out
    assert "shows tulu" in out and "(imported)" in out and "doc characters" in out
    with pytest.raises(ValueError, match="version number"):
        projects.main(["set", "walls", "--cast", "tulu@latest"])
    with pytest.raises(SystemExit):
        projects.main(["gen", "walls", "--char", "tulu", "--no-chars"])
    assert characters.main(["bogus"]) == 2


def test_cli_from_project(out_dir, capsys):
    projects.new("tulu", prompt=TULU)
    assert characters.main(["new", "tulu", "--from-project", "tulu"]) == 0
    assert "tulu: now casts tulu" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        characters.main(["new", "x", "--from-project", "tulu", "--desc", "y"])


def test_mcp_tools(out_dir, comfy, folder, make_image):
    assert mcp_server.character_new("tulu", TULU, refs={"main": str(make_image())})["version"] == 1
    assert mcp_server.character_list() == [{"name": "tulu", "title": "tulu", "version": 1}]
    assert len(mcp_server.character_get("tulu", preview=True)) == 2
    plan = mcp_server.project_import("rad", str(folder))
    assert not plan["applied"] and projects.list_projects() == []
    mcp_server.project_import("rad", str(folder), apply=True)
    mcp_server.project_update("rad", "cast tulu", cast={"tulu": None})
    out = mcp_server.project_generate("rad", count=1, preview=False)[0]
    assert out["status"] == "done"
    mcp_server.project_promote("rad", out["images"][0]["path"], "tulu-neon", "pick")
    assert mcp_server.project_doc("rad", "characters").startswith("# Cats")
    mcp_server.character_update("tulu", "glossy", description=TULU + ", glossy")
    got = mcp_server.project_get("rad")[0]
    assert got["assets"]["tulu-neon"]["stale"] and not got["assets"]["hero"]["stale"]
    assert got["state"]["cast"]["tulu"]["version"] == 2


# ---------------------------------------------------------------- review fixes

def test_missing_cast_character_fails_the_pass(walls, comfy):
    import shutil
    shutil.rmtree(characters.char_dir("tulu"))
    with pytest.raises(FileNotFoundError, match="cast character 'tulu' is missing"):
        projects.run_pass(walls, count=1)
    assert projects.summary(walls)["state"]["cast"]["tulu"]["missing"]


def test_pin_rolled_back_below_the_shown_version_is_stale(walls, make_image):
    characters.set_state("tulu", description="purple tulu")
    projects.promote(walls, str(make_image()), "fjord", shows=["tulu"])  # shows v2
    projects.set_state(walls, cast={"tulu": 1})
    assert _asset(walls, "fjord")["stale_reasons"] == ["tulu is pinned at v1, the asset shows v2"]
    projects.keep(walls, "fjord")
    assert not _asset(walls, "fjord")["stale"]


def test_pass_from_before_the_move_shows_the_character(out_dir, comfy):
    projects.new("tulu", prompt=TULU)
    old = projects.run_pass("tulu", count=1)["images"][0]["path"]
    characters.from_project("tulu", title="Lil Tulu")
    assert characters.load("tulu")["title"] == "Lil Tulu"
    projects.promote("tulu", old, "tulu")
    assert _asset("tulu", "tulu")["shows"] == {"tulu": 1}


def test_own_image_as_ref_by_relative_path(walls, make_image, monkeypatch):
    r = projects.promote(walls, str(make_image()), "front", shows=["tulu"])
    monkeypatch.chdir(projects.project_dir(walls))
    characters.set_state("tulu", refs={"front": str(Path(r["file"]).relative_to(projects.project_dir(walls)))})
    assert not _asset(walls, "front")["stale"]


def test_doc_path_with_equals_sign(walls, tmp_path):
    md = tmp_path / "a=b.md"
    md.write_text("x")
    assert projects._doc_arg([str(md)]) == {"a-b": str(md)}
    assert projects._doc_arg([f"lore={md}"]) == {"lore": str(md)}
