"""#68: character stages (explore / converge / polish): the stage on a character, stage passes, the board API."""
import json
from pathlib import Path

import pytest

import boards
import boardserve
import characters
import imagegen
import mcp_server
import projects
import stages
from test_boardserve import req, server, wait_job  # noqa: F401 - the running-server fixture

TULU = "chibi green cthulhu, amber eyes"


@pytest.fixture
def tulu(out_dir, comfy, make_image):
    """Character tulu (with a main ref) cast in project "tulu"."""
    characters.new("tulu", "lil tulu", description=TULU, refs={"main": str(make_image("ref.png"))})
    projects.new("tulu", "lil tulu", cast={"tulu": None})
    return "tulu"


def _texts(wf: dict) -> str:
    return json.dumps(wf)


def _pass(slug, n=-1):
    return projects.load(slug)["passes"][n]


# ---------------------------------------------------------------- the stage on a character

def test_character_stage(tulu, make_image):
    ch = characters.load("tulu")
    assert ch["state"]["stage"] == "explore" and characters.summary("tulu")["stage"] == "explore"
    # an asset that shows tulu does not go stale on a stage change (the stage is not the look)
    projects.run_pass("tulu", count=1, seed=1)
    img = boards.resolve(projects.load("tulu"), "1:1")[0]
    projects.promote("tulu", str(boards._abs(img)), "tulu")
    v = characters.set_state("tulu", stage="converge")
    assert v["changed"] == ["stage"] and v["note"] == "stage: converge"
    assert not projects.summary("tulu")["assets"]["tulu"]["stale"]
    with pytest.raises(ValueError, match="nothing changed"):
        characters.set_state("tulu", stage="converge")
    with pytest.raises(ValueError, match="unknown stage 'done'"):
        characters.set_state("tulu", stage="done")
    # characters from before #68 have no stage key: explore, and setting explore writes it
    ch = characters.load("tulu")
    del ch["state"]["stage"]
    characters.save(ch)
    assert stages.character_stage(characters.load("tulu")) == "explore"
    assert characters.set_state("tulu", stage="explore")["changed"] == ["stage"]


def test_project_stage_in_summary(tulu, out_dir):
    projects.new("fjord", prompt="a fjord")
    assert projects.summary("fjord")["stage"] is None
    s = projects.summary("tulu")["stage"]
    assert s["character"] == "tulu" and s["stage"] == "explore" and s["ref"] == "main"
    assert s["stages"]["explore"] == {"mode": "text", "preset": "draft", "count": 8,
                                      "needs_text": False, "needs_source": False}
    assert s["stages"]["polish"]["needs_text"] and s["stages"]["converge"]["mode"] == "edit"
    characters.set_state("tulu", stage="polish")
    assert boards.page_data(projects.load("tulu"), boards.load("tulu"))["stage"]["stage"] == "polish"


def test_settings_come_from_config(tulu, monkeypatch):
    real = imagegen.load_config()
    conf = {**real, "stages": {"converge": {"count": 2, "ideas": ["dancing"]}, "explore": {"mode": "sketch"}}}
    monkeypatch.setattr(imagegen, "load_config", lambda: conf)
    s = stages.settings("converge")
    assert s["count"] == 2 and s["ideas"] == ["dancing"] and s["preset"] == "klein"  # the rest from DEFAULTS
    with pytest.raises(ValueError, match=r"\[stages.explore\] mode = 'sketch'"):
        stages.settings("explore")
    assert "error" in projects.summary("tulu")["stage"]  # the project still shows
    with pytest.raises(ValueError, match="unknown stage"):
        stages.settings("finish")


# ---------------------------------------------------------------- stage passes

def test_explore_is_text_with_the_stage_preset(tulu, comfy):
    projects.set_state("tulu", params={"steps": 40})  # the project's params are for its own preset
    run = stages.run_stage("tulu", seed=5)
    assert run["preset"] == "draft" and len(run["images"]) == 8
    wf = comfy.workflows()[0]
    assert TULU in _texts(wf) and '"steps": 40' not in _texts(wf)
    assert not comfy.uploads  # no source image
    p = _pass("tulu")
    assert p["stage"] == "explore" and "source" not in p and p["chars"] == {"tulu": 1}
    side = json.loads(Path(run["images"][0]["path"]).with_suffix(".json").read_text())
    assert side["project"]["stage"] == "explore"
    stages.run_stage("tulu", "explore", count=1, text="with a tiny crown")
    assert "with a tiny crown" in _texts(comfy.workflows()[-1]) and _pass("tulu")["extra"] == "with a tiny crown"


def test_converge_edits_the_ref_with_one_idea_per_image(tulu, comfy):
    characters.set_state("tulu", stage="converge")
    run = stages.run_stage("tulu", seed=1)
    assert run["preset"] == "klein" and len(run["images"]) == 4 and len(comfy.uploads) >= 1
    ideas = stages.DEFAULTS["converge"]["ideas"]
    wfs = comfy.workflows()
    for wf, idea in zip(wfs, ideas[:4]):
        assert idea in _texts(wf)
    assert ideas[1] not in _texts(wfs[0])
    # each image's own instruction is in its sidecar and run.json (the board shows it)
    assert ideas[2] in json.loads(Path(run["images"][2]["path"]).with_suffix(".json").read_text())["prompt"]
    assert ideas[3] in run["images"][3]["prompt"]
    p = _pass("tulu")
    assert p["stage"] == "converge" and p["source"] == "tulu:main" and p["chars"] == {"tulu": 2}
    # the next pass moves on along the list (and wraps around)
    stages.run_stage("tulu", count=5)
    got = comfy.workflows()[4:]
    assert ideas[4] in _texts(got[0]) and ideas[0] in _texts(got[4])


def test_converge_from_a_pass_image_with_one_idea(tulu, comfy):
    stages.run_stage("tulu", "explore", count=2, seed=3)
    src = boards._abs(boards.resolve(projects.load("tulu"), "1:2")[0])
    stages.run_stage("tulu", "converge", count=2, source="1:2", text="holding a teacup")
    p = _pass("tulu")
    assert p["source"] == "1:2" and p["text"] == "holding a teacup"
    run = json.loads((Path(p["run_dir"]) / "run.json").read_text())
    assert run["source_image"] == str(src) and run["edit"]
    assert all("holding a teacup" in _texts(wf) for wf in comfy.workflows()[-2:])
    # a typed idea does not use up the idea list
    stages.run_stage("tulu", "converge", count=1)
    assert stages.DEFAULTS["converge"]["ideas"][0] in _texts(comfy.workflows()[-1])
    # a path works too; a bad one says what to give
    stages.run_stage("tulu", "converge", count=1, source=str(src))
    with pytest.raises(FileNotFoundError, match="use P:N, an image path, or CHARACTER:REF"):
        stages.run_stage("tulu", "converge", count=1, source="nope")
    with pytest.raises(ValueError, match="no pass 9"):
        stages.run_stage("tulu", "converge", count=1, source="9:1")


def test_polish_needs_the_fix(tulu, comfy):
    with pytest.raises(ValueError, match="polish needs an instruction"):
        stages.run_stage("tulu", "polish")
    run = stages.run_stage("tulu", "polish", text="remove the bump on the right elbow")
    assert len(run["images"]) == 2
    assert "remove the bump on the right elbow. Keep everything else exactly the same" in _texts(comfy.workflows()[0])
    assert _pass("tulu")["text"] == "remove the bump on the right elbow"


def test_stage_needs_a_cast_and_a_ref(out_dir, comfy, make_image):
    projects.new("fjord", prompt="a fjord")
    with pytest.raises(ValueError, match="casts no character"):
        stages.run_stage("fjord")
    stages.run_stage("fjord", "explore", count=1)  # a named stage still works (text from the prompt)
    with pytest.raises(ValueError, match="pick one on the board"):
        stages.run_stage("fjord", "converge", count=1)
    characters.new("blob", description="a blob")
    projects.new("blob", cast={"blob": None})
    with pytest.raises(ValueError, match="character 'blob' has no ref yet"):
        stages.run_stage("blob", "converge", count=1)


def test_generate_prompts_per_image(comfy):
    run = imagegen.generate("label", count=3, seed=1, prompts=["a cat", "a dog"])
    assert [im["prompt"] for im in run["images"]] == ["a cat", "a dog", "a cat"]
    assert run["prompt"] == "label"
    assert "a dog" in _texts(comfy.workflows()[1]) and "a cat" not in _texts(comfy.workflows()[1])
    assert "prompt" not in imagegen.generate("plain", count=1)["images"][0]
    with pytest.raises(ValueError, match="at least one non-empty prompt"):
        imagegen.generate("x", prompts=["  "])


# ---------------------------------------------------------------- CLI and MCP

def test_cli(tulu, comfy, capsys):
    assert characters.main(["set", "tulu", "--stage", "converge"]) == 0
    assert "changed stage" in capsys.readouterr().out
    characters.main(["status", "tulu"])
    assert "stage: converge" in capsys.readouterr().out
    assert projects.main(["gen", "tulu", "--stage", "-n", "1"]) == 0
    assert _pass("tulu")["stage"] == "converge" and _pass("tulu")["count"] == 1
    assert projects.main(["gen", "tulu", "--stage", "explore", "--seed", "2"]) == 0
    assert _pass("tulu")["count"] == 8
    assert projects.main(["gen", "tulu", "--stage", "polish", "--from", "1:1", "--text", "fix the eye"]) == 0
    assert _pass("tulu")["source"] == "1:1"
    projects.main(["status", "tulu"])
    out = capsys.readouterr().out
    assert "stage:    converge (character tulu" in out and "polish from 1:1: fix the eye" in out
    for bad in (["--from", "1:1"], ["--stage", "explore", "--extra", "x"]):
        with pytest.raises(SystemExit):
            projects.main(["gen", "tulu", *bad])
    assert projects.main(["gen", "tulu", "-n", "1"]) == 0 and "stage" not in _pass("tulu")


def test_mcp(tulu, comfy):
    mcp_server.character_update("tulu", "ready to converge", stage="converge")
    assert mcp_server.project_get("tulu")[0]["stage"]["stage"] == "converge"
    out = mcp_server.project_generate("tulu", stage="current", source="tulu:main", preview=False)[0]
    assert out["status"] == "done" and len(out["images"]) == 4
    assert _pass("tulu")["stage"] == "converge" and _pass("tulu")["author"] == "claude"
    mcp_server.project_generate("tulu", count=1, stage="polish", text="fix the eye", preview=False)
    assert _pass("tulu")["count"] == 1
    with pytest.raises(ValueError, match="source and text need a stage"):
        mcp_server.project_generate("tulu", text="x", preview=False)
    with pytest.raises(ValueError, match="a stage pass takes"):
        mcp_server.project_generate("tulu", stage="explore", extra="x", preview=False)


# ---------------------------------------------------------------- the board API

def test_board_gen_stage(tulu, server):  # noqa: F811
    port, jobs = server
    st, _, body = req(port, "POST", "/api/projects/tulu/gen", {"stage": "converge", "count": 2})
    assert st == 202 and body["result"]["stage"] == "converge" and body["result"]["count"] == 2
    assert wait_job(jobs)["state"] == "done"
    assert _pass("tulu")["stage"] == "converge" and _pass("tulu")["source"] == "tulu:main"
    st, _, body = req(port, "POST", "/api/projects/tulu/gen", {"stage": "polish", "source": "1:2", "text": "fix the tail"})
    assert st == 202 and body["result"]["count"] == 2  # the stage's count
    wait_job(jobs)
    assert _pass("tulu")["source"] == "1:2" and _pass("tulu")["text"] == "fix the tail"
    for bad, msg in (({"stage": "polish"}, "polish needs an instruction"), ({"stage": "nope"}, "stage: one of"),
                     ({"stage": "converge", "source": 3}, "source and text: strings"),
                     ({"stage": "explore", "count": 99}, "count: a whole number")):
        st, _, body = req(port, "POST", "/api/projects/tulu/gen", bad)
        assert st == 400 and msg in body["error"], body
    # a failed stage pass (a bad source) is reported on the job, like any other
    req(port, "POST", "/api/projects/tulu/gen", {"stage": "converge", "source": "9:9"})
    j = wait_job(jobs)
    assert j["state"] == "failed" and "no pass 9" in j["error"]


def test_board_stage_switch_and_ref(tulu, server, comfy):  # noqa: F811
    port, jobs = server
    st, _, body = req(port, "POST", "/api/projects/tulu/stage", {"stage": "converge"})
    assert st == 200 and body["data"]["stage"]["stage"] == "converge"
    assert characters.load("tulu")["state"]["stage"] == "converge"
    v = characters.load("tulu")["version"]
    st, _, body = req(port, "POST", "/api/projects/tulu/stage", {"stage": "converge"})  # no change, no version
    assert st == 200 and characters.load("tulu")["version"] == v
    assert req(port, "POST", "/api/projects/tulu/stage", {"stage": "x"})[0] == 400
    stages.run_stage("tulu", count=2, seed=1)
    st, _, body = req(port, "POST", "/api/projects/tulu/ref", {"image": "1:2"})
    assert st == 200 and body["result"] == {"character": "tulu", "ref": "main", "version": v + 1, "image": "1:2"}
    ch = characters.load("tulu")
    assert ch["state"]["refs"]["main"]["file"] == f"refs/main-v{v + 1}.png"
    assert ch["versions"][-1]["note"] == "main ref from tulu 1:2"
    # the image that became the ref is not stale against its own ref change
    projects.promote("tulu", str(boards._abs(boards.resolve(projects.load("tulu"), "1:2")[0])), "tulu")
    assert not projects.summary("tulu")["assets"]["tulu"]["stale"]
    assert req(port, "POST", "/api/projects/tulu/ref", {})[0] == 400
    projects.new("fjord", prompt="a fjord")
    st, _, body = req(port, "POST", "/api/projects/fjord/stage", {"stage": "polish"})
    assert st == 400 and "casts no character" in body["error"]
    st, _, body = req(port, "POST", "/api/projects/fjord/gen", {"stage": "explore"})
    assert st == 400 and "casts no character" in body["error"]
    assert req(port, "POST", "/api/projects/fjord/ref", {"image": "1:1"})[0] == 400


def test_job_shows_the_stage():
    jobs = boardserve.Jobs()
    j = jobs.start("tulu", 2, run=lambda s, n: {"run_dir": None}, stage="polish")
    assert j["stage"] == "polish"
    jobs.thread.join(5)
    assert "stage" not in jobs.start("tulu", 2, run=lambda s, n: {})


# ---------------------------------------------------------------- review fixes

def test_source_checks_and_the_latest_ref(tulu, comfy, make_image, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("not an image")
    with pytest.raises(ValueError, match="id_rsa: not an image"):
        stages.run_stage("tulu", "converge", count=1, source=str(secret))
    # a project that pins tulu still edits tulu's newest ref (what the board's ref button set)
    projects.set_state("tulu", cast={"tulu": 1})
    characters.set_state("tulu", refs={"main": str(make_image("new.png", color=(1, 2, 3)))})
    stages.run_stage("tulu", "converge", count=1)
    run = json.loads((Path(_pass("tulu")["run_dir"]) / "run.json").read_text())
    assert run["source_image"].endswith("main-v2.png")


def test_ideas_offset_and_run_prompt(tulu, comfy):
    ideas = stages.DEFAULTS["converge"]["ideas"]
    run = stages.run_stage("tulu", "converge", count=2)
    assert "[one idea per image]" in run["prompt"]  # run.json and the contact sheet: the template
    stages.run_stage("tulu", "converge", count=3, text="dancing")  # a typed idea takes nothing from the list
    stages.run_stage("tulu", "converge", count=1)
    assert ideas[2] in _texts(comfy.workflows()[-1])
    assert [p.get("ideas") for p in projects.load("tulu")["passes"]] == [2, None, 1]


def test_counts(tulu, comfy):
    assert len(stages.run_stage("tulu", "explore", count=0)["images"]) == 1
    out = mcp_server.project_generate("tulu", stage="explore", preview=False)[0]
    assert len(out["images"]) == min(8, mcp_server.MAX_COUNT)
    out = mcp_server.project_generate("tulu", count=0, preview=False)[0]
    assert len(out["images"]) == 1
