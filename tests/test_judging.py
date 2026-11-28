"""docs/judging.md: the example JSON must parse and match the field list; the agent file must be readable."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KINDS = {"anatomy", "text", "duplicate", "melt", "seam", "noise", "blank", "off_model", "other"}


def _example():
    text = (ROOT / "docs" / "judging.md").read_text()
    m = re.search(r"```json\n(.*?)\n```", text, re.S)
    assert m, "no json example in docs/judging.md"
    return json.loads(m.group(1)), text


def test_example_matches_fields():
    ex, _ = _example()
    assert set(ex) == {"image", "artifacts", "instruction_following", "character_match",
                       "style_match", "overall", "confidence"}
    for a in ex["artifacts"]:
        assert set(a) == {"kind", "where", "severity", "note"}
        assert a["kind"] in KINDS and a["severity"] in (1, 2, 3)
    assert set(ex["instruction_following"]) == {"score", "missed", "extra"}
    assert set(ex["style_match"]) == {"score", "style"}
    assert ex["confidence"] in ("high", "medium", "low")


def test_doc_lists_every_kind():
    _, text = _example()
    for k in KINDS:
        assert f"`{k}`" in text


def test_agent_file():
    raw = (ROOT / ".claude" / "agents" / "image-judge.md").read_text()
    head = raw.split("---")[1]
    fields = dict(l.split(":", 1) for l in head.strip().splitlines())
    assert fields["name"].strip() == "image-judge"
    assert fields["model"].strip() == "sonnet"
    tools = {t.strip() for t in fields["tools"].split(",")}
    assert tools <= {"Read", "Glob", "Grep"}, "the judge must stay read-only"
    assert "docs/judging.md" in raw
