"""boards.py (#7): ratings and notes on a project's pass images, the board page, summary and CLI."""
import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

import boards
import projects


@pytest.fixture
def proj(out_dir, comfy):
    """A project with two prompt versions and one 2-image pass on each (passes 1 and 2)."""
    projects.new("fjord", "Fjord wallpapers", "moody fjord at dawn", prompt="a fjord at dawn")
    projects.run_pass("fjord", count=2, seed=1)
    projects.set_state("fjord", "less fog after feedback", "claude", prompt="a fjord at dawn, light mist")
    projects.run_pass("fjord", count=2, seed=10, author="claude")
    return "fjord"


def page_data(slug: str) -> dict:
    html = boards.page_file(slug).read_text(encoding="utf-8")
    m = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "board data block missing"
    return json.loads(m.group(1))


def pass_image(slug: str, p: int, n: int) -> Path:
    run = json.loads((Path(projects.load(slug)["passes"][p - 1]["run_dir"]) / "run.json").read_text())
    return Path(run["images"][n - 1]["path"])


def test_rate_by_pass_id_and_path(proj):
    out = boards.rate(proj, ["1:1", str(pass_image(proj, 2, 2))], "pick", "love the light")
    assert [r["id"] for r in out] == ["1:1", "2:2"] and all(r["rating"] == "pick" for r in out)
    b = boards.load(proj)
    key = str(pass_image(proj, 1, 1).relative_to(projects.projects_root().parent))
    assert b["images"][key]["rating"] == "pick" and b["images"][key]["notes"][0]["text"] == "love the light"
    assert projects.load(proj)["version"] == 2  # an opinion, not a new version
    boards.rate(proj, ["1:1"], "clear")
    assert "rating" not in boards.load(proj)["images"][key]
    assert len(boards.load(proj)["log"]) == 3


def test_rate_validates_everything_first(proj, make_image):
    for images, rating, msg in [(["1:1"], "love", "rating must be"), (["3:1"], "pick", "no pass 3"),
                                (["1:9"], "pick", "no #9"), ([], "pick", "no images")]:
        with pytest.raises(ValueError, match=msg):
            boards.rate(proj, images, rating)
    with pytest.raises(ValueError, match="no #9"):
        boards.rate(proj, ["1:1", "1:9"], "pick")
    with pytest.raises(FileNotFoundError):
        boards.rate(proj, ["/nope.png"], "pick")
    with pytest.raises(ValueError, match="not an image"):
        boards.rate(proj, [str(make_image("x.gif"))], "pick")
    assert boards.load(proj)["images"] == {}  # nothing half-written


def test_notes_on_images_and_board(proj):
    boards.note(proj, "too much fog everywhere")
    e = boards.note(proj, "nice reflection", "2:1", author="claude")
    assert e["id"] == "2:1"
    with pytest.raises(ValueError, match="empty note"):
        boards.note(proj, "  ")
    b = boards.load(proj)
    assert b["notes"][0]["text"] == "too much fog everywhere"
    assert [n["author"] for e in b["images"].values() for n in e["notes"]] == ["claude"]


def test_summary_has_what_claude_needs(proj, make_image):
    """The #7 'done when': picks, rejects and notes per prompt version, from project_get alone."""
    boards.rate(proj, ["1:1"], "reject", "fog hides the mountains")
    boards.rate(proj, ["2:1", "2:2"], "pick")
    boards.rate(proj, [str(make_image("outside.png"))], "maybe")
    boards.note(proj, "keep the mist, lose the fog")
    s = projects.summary(proj)
    b = s["board"]
    assert b["counts"] == {"pick": 2, "maybe": 1, "reject": 1, "unrated": 1}
    assert [(i.get("id"), i["rating"]) for i in b["images"]] == [
        ("2:1", "pick"), ("2:2", "pick"), (None, "maybe"), ("1:1", "reject")]
    assert b["images"][0]["version"] == 2 and b["images"][-1]["version"] == 1
    assert b["images"][-1]["notes"] == ["human: fog hides the mountains"]
    assert b["notes"] == ["human: keep the mist, lose the fog"]
    assert [p["n"] for p in s["recent_passes"]] == [1, 2]
    assert b["page"].endswith("projects/fjord/index.html")


def test_new_since_last_look_includes_board(proj):
    projects.summary(proj, mark_seen="claude")
    boards.rate(proj, ["1:2"], "pick")
    boards.note(proj, "more like 1:2")
    new = projects.summary(proj, mark_seen="claude")["new_since_last_look"]["board"]
    assert [(e["action"], e.get("id")) for e in new] == [("rate", "1:2"), ("note", None)]
    assert projects.summary(proj, mark_seen="claude")["new_since_last_look"]["board"] == []


def test_old_seen_file_without_board_count(proj):
    boards.rate(proj, ["1:1"], "pick")
    seen = projects.project_dir(proj) / "seen.json"
    seen.write_text(json.dumps({"claude": {"version": 2, "passes": 2}}))  # written before boards
    assert len(projects.summary(proj, mark_seen="claude")["new_since_last_look"]["board"]) == 1


def test_page_groups_passes_and_links_files(proj):
    boards.rate(proj, ["2:1"], "pick", "this one")
    d = page_data(proj)
    assert d["version"] == 2 and d["state"]["prompt"] == "a fjord at dawn, light mist"
    assert [(p["n"], p["version"]) for p in d["passes"]] == [(1, 1), (2, 2)]
    im = d["passes"][1]["images"][0]
    assert im["id"] == "2:1" and im["rating"] == "pick" and im["notes"][0]["text"] == "this one"
    page_dir = boards.page_file(proj).parent
    assert (page_dir / unquote(im["src"])).resolve() == pass_image(proj, 2, 1).resolve()
    assert (page_dir / unquote(d["passes"][0]["sheet"])).is_file()
    assert [v["note"] for v in d["versions"]] == ["created", "less fog after feedback"]
    assert "{{" not in boards.page_file(proj).read_text()


def test_page_follows_every_change(out_dir, make_image):
    projects.new("fox", prompt="a fox")
    assert page_data("fox")["passes"] == []  # written on create
    projects.set_state("fox", "red", prompt="a red fox")
    assert page_data("fox")["version"] == 2
    projects.promote("fox", str(make_image()), "fox")
    assert page_data("fox")["assets"][0]["name"] == "fox"


def test_page_survives_missing_run_and_escapes(proj):
    projects.set_state(proj, "x", prompt="</script><script>alert(1)</script>")
    shutil_rm = Path(projects.load(proj)["passes"][0]["run_dir"])
    for f in shutil_rm.iterdir():
        f.unlink()
    shutil_rm.rmdir()
    boards.write_page(proj)
    html = boards.page_file(proj).read_text()
    assert "</script><script>alert" not in html
    d = page_data(proj)
    assert d["passes"][0]["missing"] and d["passes"][0]["images"] == []
    assert projects.summary(proj)["board"]["counts"]["unrated"] == 2  # only pass 2 is left
    with pytest.raises(ValueError, match="has 0 image"):
        boards.rate(proj, ["1:1"], "pick")


def test_broken_page_never_breaks_a_change(out_dir, monkeypatch, capsys):
    projects.new("fox", prompt="a fox")
    monkeypatch.setattr(boards, "write_page", lambda slug: 1 / 0)
    projects.set_state("fox", "x", prompt="a grey fox")
    assert projects.load("fox")["version"] == 2
    assert "could not rebuild the board page" in capsys.readouterr().err


def test_cli(proj, capsys, stub_calls):
    assert projects.main(["rate", proj, "pick", "1:1", "2:2", "--note", "good"]) == 0
    assert capsys.readouterr().out.splitlines() == ["1:1: pick", "2:2: pick"]
    assert projects.main(["note", proj, "warmer next time"]) == 0
    assert projects.main(["note", proj, "sharp", "--image", "2:2", "--author", "claude"]) == 0
    assert projects.main(["status", proj]) == 0
    out = capsys.readouterr().out
    assert "board: 2 pick, 0 maybe, 0 reject, 2 unrated" in out
    assert "P2" in out and "2:2  human: good | claude: sharp" in out
    assert projects.main(["board", proj, "--open"]) == 0
    assert any(c.startswith("explorer.exe") for c in stub_calls())
    with pytest.raises(SystemExit):
        projects.main(["rate", proj, "love", "1:1"])


def test_clear_on_unrated_image_changes_nothing(proj):
    assert boards.rate(proj, ["1:1"], "clear") == [{"image": str(pass_image(proj, 1, 1)), "id": "1:1", "rating": None}]
    b = boards.load(proj)
    assert b["images"] == {} and b["log"] == []
    boards.rate(proj, ["1:1"], "pick")
    boards.rate(proj, ["1:1"], "clear")
    assert boards.load(proj)["images"] == {}  # an entry with no rating and no notes is dropped


def test_path_to_a_pass_image_gets_its_id(proj):
    assert boards.rate(proj, [str(pass_image(proj, 2, 1))], "maybe")[0]["id"] == "2:1"
    assert boards.note(proj, "hm", str(pass_image(proj, 1, 2)))["id"] == "1:2"


def test_page_and_summary_counts_agree(proj, make_image):
    boards.rate(proj, ["1:1"], "pick")
    boards.rate(proj, [str(make_image("outside.png"))], "reject")
    boards.note(proj, "noted only", str(make_image("other.png")))
    assert page_data(proj)["counts"] == projects.summary(proj)["board"]["counts"] == \
        {"pick": 1, "maybe": 0, "reject": 1, "unrated": 3}
    assert len(page_data(proj)["others"]) == 2


def test_character_change_rebuilds_the_board(out_dir):
    import characters
    characters.new("tulu", description="chibi cthulhu")
    projects.new("wall", prompt="a wallpaper", cast={"tulu": None})
    characters.set_state("tulu", "greener", description="green chibi cthulhu")
    assert page_data("wall")["state"]["prompt"].startswith("green chibi cthulhu")
