"""Workflow templates and config.toml: placeholders, presets, JSON validity."""
import json

import pytest

import doctor
import imagegen

WORKFLOWS = sorted((imagegen.ROOT / "workflows").glob("*.json"))


def test_fill_keeps_types_and_recurses():
    wf = {"a": {"inputs": {"seed": "{{seed}}", "cfg": "{{cfg}}", "text": "{{prompt}}", "list": ["{{seed}}", 1]}}}
    out = imagegen.fill(wf, {"seed": 7, "cfg": 6.5, "prompt": "hi"})
    assert out == {"a": {"inputs": {"seed": 7, "cfg": 6.5, "text": "hi", "list": [7, 1]}}}
    assert wf["a"]["inputs"]["seed"] == "{{seed}}"  # template untouched


def test_fill_leaves_partial_placeholders_alone():
    # only a whole string "{{key}}" is a placeholder, so prompts can contain braces
    assert imagegen.fill({"t": "a {{seed}} b"}, {}) == {"t": "a {{seed}} b"}


def test_fill_names_the_missing_key():
    with pytest.raises(KeyError, match="width"):
        imagegen.fill({"w": "{{width}}"}, {})


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_api_format(wf):
    graph = json.loads(wf.read_text())
    assert graph, "empty workflow"
    for node_id, node in graph.items():
        assert {"class_type", "inputs"} <= set(node), f"node {node_id} is not API format"
        for v in node["inputs"].values():  # links are [node_id, output_index] to an existing node
            if isinstance(v, list):
                assert v[0] in graph, f"node {node_id} links to missing node {v[0]}"
    assert any(n["class_type"] == "SaveImage" for n in graph.values())


def test_every_preset_fills_every_mode():
    """doctor's placeholder check, run against the committed config.toml: all presets pass."""
    results = doctor.check_workflows(imagegen.load_config())
    bad = [r for r in results if r["status"] != "ok"]
    assert not bad, bad


def test_every_workflow_file_is_used_by_some_preset():
    conf = imagegen.load_config()
    used = set()
    for p in conf["presets"].values():
        for _key, wf, _ in doctor._modes(p):
            used.add(wf)
    unused = {w.name for w in WORKFLOWS} - used
    assert not unused, f"workflows no preset uses: {unused}"


def test_preset_post_steps_exist():
    for name, p in imagegen.list_presets().items():
        for step in p.get("post", []):
            assert step in imagegen.POST, f"{name}: unknown post step {step}"
