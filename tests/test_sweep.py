"""gen sweep (#17): a grid of settings/models for one prompt: plan, run (fake ComfyUI), sheet layout, guardrail."""
import json
from pathlib import Path

import pytest

import bakeoff
import imagegen
import mcp_server
import sweep


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch):
    monkeypatch.setattr(bakeoff, "SETTLE_STEP", 0.01)


def _ks(wf):  # the sampler's settings (SDXL KSampler) or klein's scheduler steps
    ks = next((n["inputs"] for n in wf.values() if n["class_type"] == "KSampler"), None)
    if ks:
        return {"seed": ks["seed"], "steps": ks["steps"], "cfg": ks["cfg"]}
    return {"seed": next(n["inputs"]["noise_seed"] for n in wf.values() if n["class_type"] == "RandomNoise"),
            "steps": next(n["inputs"]["steps"] for n in wf.values() if n["class_type"] == "Flux2Scheduler")}


def _conf():
    return imagegen.load_config()


def test_plan_two_axes_rows_are_the_first_axis_columns_the_rest():
    pl = sweep.plan("fox", ["draft"], _conf(), steps=[10, 20], cfg=[3.0, 6.5], seed=5)
    assert pl["varying"] == ["steps", "cfg"] and pl["row_axis"] == "steps" and pl["col_axes"] == ["cfg"]
    assert pl["rows"] == ["steps 10", "steps 20"] and pl["cols"] == ["cfg 3", "cfg 6.5"]
    assert [(c["r"], c["c"], c["steps"], c["cfg"]) for c in pl["cells"]] == \
        [(0, 0, 10, 3.0), (0, 1, 10, 6.5), (1, 0, 20, 3.0), (1, 1, 20, 6.5)]
    assert {c["seed"] for c in pl["cells"]} == {5}  # fixed seed unless --seeds
    assert pl["cells"][3]["label"] == "steps 20 · cfg 6.5"


def test_plan_seeds_become_rows_and_models_the_columns():
    pl = sweep.plan("fox", ["draft", "klein"], _conf(), seeds=2, seed=7)
    assert pl["row_axis"] == "seed" and pl["rows"] == ["seed 7", "seed 8"]
    assert pl["cols"][1] == "FLUX.2 klein 4B" and len(pl["cols"]) == 2
    assert [(c["preset"], c["seed"]) for c in pl["cells"]] == \
        [("draft", 7), ("klein", 7), ("draft", 8), ("klein", 8)]


def test_plan_one_axis_is_one_row_of_columns_and_three_axes_combine_in_the_columns():
    one = sweep.plan("fox", ["draft"], _conf(), steps=[4, 8, 16])
    assert one["row_axis"] is None and one["rows"] == [] and one["cols"] == ["steps 4", "steps 8", "steps 16"]
    three = sweep.plan("fox", ["draft"], _conf(), steps=[4, 8], cfg=[3.0, 6.0], seeds=2, seed=1, rows="steps")
    assert three["row_axis"] == "steps" and three["cols"] == \
        ["cfg 3 · seed 1", "cfg 3 · seed 2", "cfg 6 · seed 1", "cfg 6 · seed 2"]


def test_plan_errors():
    c = _conf()
    with pytest.raises(ValueError, match="nothing to sweep"):
        sweep.plan("fox", ["draft"], c)
    with pytest.raises(ValueError, match="unknown preset"):
        sweep.plan("fox", ["nope"], c, steps=[1, 2])
    with pytest.raises(ValueError, match="not a text-to-image preset"):
        sweep.plan("fox", ["kontext"], c, seeds=2)
    with pytest.raises(ValueError, match="twice"):
        sweep.plan("fox", ["draft"], c, steps=[8, 8])
    with pytest.raises(ValueError, match="steps must be"):
        sweep.plan("fox", ["draft"], c, steps=[0, 8])
    with pytest.raises(ValueError, match="doesn't vary"):
        sweep.plan("fox", ["draft"], c, steps=[1, 2], rows="cfg")
    with pytest.raises(ValueError, match="empty|needs a prompt"):
        sweep.plan("  ", ["draft"], c, seeds=2)


def test_a_setting_the_model_lacks_is_rendered_once_and_the_rest_skipped():
    pl = sweep.plan("fox", ["draft", "klein"], _conf(), cfg=[3.0, 6.0], seed=1)
    by = {(c["preset"], c["cfg"] if not c["skip"] else "skip"): c for c in pl["cells"]}
    klein = [c for c in pl["cells"] if c["preset"] == "klein"]
    assert [bool(c["skip"]) for c in klein] == [False, True]
    assert klein[0]["cfg"] is None and "cfg n/a" in klein[0]["label"]  # the preset's own cfg, nothing passed
    assert "no cfg input" in klein[1]["skip"]
    assert by[("draft", 3.0)]["cfg"] == 3.0


def test_sweep_runs_the_grid_with_the_same_seeds_and_a_labelled_sheet(comfy):
    run = sweep.sweep("a fox", ["draft", "klein"], steps=[8, 16], seeds=2, seed=40, styles=["none"], interval=0.01)
    wfs = comfy.workflows()
    assert len(wfs) == 8
    # a model's cells together (it loads once); every model sees the same (steps, seed) combinations
    assert comfy.frees == 2
    assert sorted((_ks(w)["steps"], _ks(w)["seed"]) for w in wfs[:4]) == [(8, 40), (8, 41), (16, 40), (16, 41)]
    assert sorted((_ks(w)["steps"], _ks(w)["seed"]) for w in wfs[:4]) == \
        sorted((_ks(w)["steps"], _ks(w)["seed"]) for w in wfs[4:])
    out = Path(run["run_dir"])
    assert "-sweep-a-fox" in out.name
    s = run["sweep"]
    assert s["row_axis"] == "seed" and s["rows"] == ["seed 40", "seed 41"]
    first = s["cols"][0].split(" · ")[0]  # the draft preset's model name
    assert s["cols"] == [f"{m} · steps {st}" for m in (first, "FLUX.2 klein 4B") for st in (8, 16)]
    assert len(run["images"]) == 8 and all(im["column"] for im in run["images"])
    for im in run["images"]:  # copies with the params in each sidecar
        side = json.loads(Path(im["path"]).with_suffix(".json").read_text())
        assert side["params"]["steps"] in (8, 16) and side["seed"] == im["seed"]
    assert (out / "index.html").is_file() and (out / "bakeoff.json").is_file()
    assert run["bakeoff"]["mode"] == "sweep" and len(run["bakeoff"]["presets"]) == 8
    assert run["preset"] == "sweep: 2 model × 2 steps × 2 seed"
    assert '"sweep"' in (out / "index.html").read_text()


def test_the_prompt_is_the_same_in_every_cell_and_style_none_removes_the_look(comfy):
    sweep.sweep("a fox", ["draft", "klein"], seeds=1, styles=["none", "synthwave"], interval=0.01)
    texts = [json.dumps(w) for w in comfy.workflows()]
    assert all("a fox" in t for t in texts)
    assert sum("neon" in t.lower() or "synth" in t.lower() for t in texts) >= 2  # synthwave cells carry the look


def test_confirm_guardrail(comfy):
    with pytest.raises(ValueError, match="more than 4"):
        sweep.sweep("fox", ["draft"], steps=[4, 8, 12], seeds=2, max_cells=4)
    assert comfy.workflows() == []  # refused before anything ran
    run = sweep.sweep("fox", ["draft"], steps=[4, 8, 12], seeds=2, max_cells=4, confirm=True, interval=0.01)
    assert len(run["images"]) == 6


def test_a_failing_cell_is_noted_and_the_rest_run(comfy, monkeypatch):
    real = imagegen.generate

    def gen(*a, **k):
        if k.get("steps") == 4:
            raise RuntimeError("out of memory")
        return real(*a, **k)
    monkeypatch.setattr(imagegen, "generate", gen)
    run = sweep.sweep("fox", ["draft"], steps=[4, 8], interval=0.01)
    res = run["bakeoff"]["presets"]
    assert "out of memory" in res[0]["error"] and res[1]["error"] is None and len(run["images"]) == 1


def test_ctrl_c_keeps_a_sheet(comfy, monkeypatch):
    real = imagegen.generate

    def gen(*a, **k):
        if k.get("steps") == 8:
            raise KeyboardInterrupt
        return real(*a, **k)
    monkeypatch.setattr(imagegen, "generate", gen)
    with pytest.raises(KeyboardInterrupt):
        sweep.sweep("fox", ["draft"], steps=[4, 8, 12], interval=0.01, label="stop")
    out = Path(imagegen.load_config()["output_dir"]).expanduser()
    run = json.loads(next(out.glob("*/*-sweep-stop/run.json")).read_text())
    assert run["bakeoff"]["complete"] is False and len(run["images"]) == 1 and "sweep" in run


def test_cli_and_skipped_cells(comfy, capsys):
    assert imagegen.main(["sweep", "fox", "-p", "draft,klein", "--cfg", "3,6", "--seed", "3"]) == 0
    out = capsys.readouterr().out
    assert "contact sheet" in out and "no cfg input" in out
    assert len(comfy.workflows()) == 3  # draft x2, klein once


def test_cli_asks_before_a_big_grid_and_refuses_without_a_terminal(comfy, capsys):
    with pytest.raises(ValueError, match="more than 16"):
        imagegen.main(["sweep", "fox", "--steps", "4,8,12,16,20", "--seeds", "4"])
    assert comfy.workflows() == []


def test_mcp_sweep(comfy):
    out = mcp_server.sweep("a fox", presets=["draft"], steps=[4, 8], seed=2)
    summary = out[0]
    assert summary["status"] == "done" and len(summary["images"]) == 2
    assert summary["contact_sheet_windows"] is None or "index.html" in summary["contact_sheet_windows"]
    with pytest.raises(ValueError, match="together"):
        mcp_server.sweep("a fox", width=512)
