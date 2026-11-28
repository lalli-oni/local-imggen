"""refs.py (#112): plan text -> inbox checklist, import with provenance into projects and characters."""
import json
import re
from pathlib import Path

import pytest

import characters
import imagegen
import projects
import refs
from refs import import_inbox, plan, status


def text(*tasks, date="2026-11-29"):
    return json.dumps({"date": date, "tasks": list(tasks)})


def task(**kw):
    return {"service": "chatgpt", "title": "Neon skyline", "prompt": "a neon skyline at dusk",
            "target": {"project": "rs", "asset": "world/skyline"}, **kw}


@pytest.fixture
def rs(out_dir):
    projects.new("rs", "Radical Sabbatical", "vaporwave cats", prompt="vaporwave")
    return "rs"


def save_result(day, tid, make_image, ext="png", where=None):
    src = make_image(f"{tid}.{ext}")
    dest = (where or refs.inbox_dir(day)) / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    return dest


def test_plan_writes_files_and_ids(rs, make_image):
    att = make_image("ref.png")
    p = plan(text(task(attachments=[str(att)]), task(service="ideogram", title="Sign", prompt="a sign")))
    assert [t["id"] for t in p["tasks"]] == ["01-chatgpt-neon-skyline", "02-ideogram-sign"]
    d = refs.inbox_dir("2026-11-29")
    assert {f.name for f in d.iterdir()} == {"plan.json", "checklist.md", "checklist.html"}
    md = (d / "checklist.md").read_text()
    assert "a neon skyline at dusk" in md and "01-chatgpt-neon-skyline.png" in md and imagegen.to_windows_path(att) in md
    assert "https://chatgpt.com/" in md
    html = (d / "checklist.html").read_text()
    assert "{{DATA}}" not in html and "a neon skyline at dusk" in html
    assert json.loads((d / "plan.json").read_text())["tasks"][0]["status"] == "todo"


def test_plan_again_adds_and_rejects_duplicates(rs):
    plan(text(task()))
    p = plan(text(task(title="Second")))
    assert [t["id"] for t in p["tasks"]] == ["01-chatgpt-neon-skyline", "02-chatgpt-second"]
    with pytest.raises(ValueError, match="used twice"):
        plan(text(task(id="02-chatgpt-second")))


@pytest.mark.parametrize("bad,msg", [
    (task(service="midjourney"), "unknown service"),
    (task(prompt="  "), "no prompt"),
    (task(target={}), "exactly one"),
    (task(target={"project": "rs", "character": "x"}), "exactly one"),
    (task(target={"project": "rs"}), "needs an asset"),
    (task(target={"character": "tulu"}), "needs a ref"),
    (task(id="Bad Id"), "bad id"),
    (task(attachments=["/nope/missing.png"]), "does not exist"),
])
def test_plan_validation(rs, bad, msg):
    with pytest.raises((ValueError, FileNotFoundError), match=msg):
        plan(text(bad))
    assert not refs.inbox_dir("2026-11-29").exists()  # a bad plan leaves nothing behind


def test_plan_cap_warning(rs):
    n = refs.services()["chatgpt"]["daily"]
    p = plan(text(*[task(title=f"t{i}") for i in range(n + 1)]))
    assert any("chatgpt" in w and "free tier" in w for w in p["warnings"])


def test_import_project_asset_with_provenance(rs, make_image, capsys):
    plan(text(task(target={"project": "rs", "asset": "world/skyline", "as_ref": "skyline"})))
    save_result("2026-11-29", "01-chatgpt-neon-skyline", make_image)
    dry = import_inbox()
    assert dry["results"][0]["status"] == "would import"
    assert projects.load("rs")["assets"] == {}  # dry run changes nothing
    r = import_inbox(apply=True)
    assert r["results"][0]["status"] == "imported"
    proj = projects.load("rs")
    v = proj["assets"]["world/skyline"]["versions"][0]
    assert v["external"] == {"service": "chatgpt", "prompt": "a neon skyline at dusk",
                             "task": "01-chatgpt-neon-skyline", "date": "2026-11-29"}
    assert v["prompt"] == "a neon skyline at dusk" and "skyline" in proj["state"]["refs"]
    assert projects.summary("rs")["assets"]["world/skyline"]["external"]["service"] == "chatgpt"
    assert projects.main(["status", "rs"]) == 0
    assert "external (chatgpt)" in capsys.readouterr().out
    # done tasks are not imported again, and the checklist shows them ticked
    assert import_inbox(apply=True)["results"] == []
    assert "[x]" in (refs.inbox_dir("2026-11-29") / "checklist.md").read_text()
    assert status()[0]["todo"] == []


def test_import_character_ref(out_dir, make_image):
    characters.new("tulu", description="a chibi cthulhu")
    plan(text(task(target={"character": "tulu", "ref": "pose-wave", "role": "pose"})))
    save_result("2026-11-29", "01-chatgpt-neon-skyline", make_image)
    import_inbox(apply=True)
    ch = characters.load("tulu")
    assert ch["version"] == 2 and ch["state"]["refs"]["pose-wave"]["role"] == "pose"
    assert "external (chatgpt)" in ch["versions"][-1]["note"]


def test_import_waiting_missing_target_and_watch_folder(rs, make_image, tmp_path):
    plan(text(task(), task(title="Other", target={"project": "ghost", "asset": "a"}),
              task(title="Third")))
    day = "2026-11-29"
    save_result(day, "02-chatgpt-other", make_image)
    dl = tmp_path / "Downloads"
    save_result(day, "03-chatgpt-third", make_image, ext="jpg", where=dl)
    first = dl / "03-chatgpt-third (1).png"  # a browser's second save of the same name, newer
    first.write_bytes((dl / "03-chatgpt-third.jpg").read_bytes())
    r = {x["id"]: x for x in import_inbox(apply=True, watch=[str(dl)])["results"]}
    assert r["01-chatgpt-neon-skyline"]["status"] == "waiting"
    assert r["02-chatgpt-other"]["status"] == "error" and "no project 'ghost'" in r["02-chatgpt-other"]["error"]
    assert r["03-chatgpt-third"]["status"] == "imported" and r["03-chatgpt-third"]["ignored"]
    # the failed and waiting tasks stay todo
    assert status()[0]["todo"] == ["01-chatgpt-neon-skyline", "02-chatgpt-other"]


def test_cli(rs, make_image, tmp_path, capsys, monkeypatch):
    f = tmp_path / "plan.json"
    f.write_text(text(task()))
    assert imagegen.main(["ref", "plan", "--from", str(f)]) == 0
    assert "1 tasks for 2026-11-29" in capsys.readouterr().out
    assert imagegen.main(["ref", "import"]) == 0
    out = capsys.readouterr().out
    assert "waiting" in out
    save_result("2026-11-29", "01-chatgpt-neon-skyline", make_image)
    assert imagegen.main(["ref", "import"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert imagegen.main(["ref", "import", "--yes"]) == 0
    assert imagegen.main(["ref", "status"]) == 0
    assert imagegen.main(["ref", "services"]) == 0
    f.write_text("{not json")
    assert imagegen.main(["ref", "plan", "--from", str(f)]) == 1


def test_extension_knows_every_configured_service():
    js = (Path(imagegen.ROOT) / "extension" / "services.js").read_text()
    in_js = set(re.findall(r"^  (\w+): \{$", js, re.M))
    assert in_js == set(refs.services())
    manifest = json.loads((Path(imagegen.ROOT) / "extension" / "manifest.json").read_text())
    assert "content_scripts" not in manifest  # nothing runs on pages by itself
    # host access only to the configured services' own origins, no wildcards over other sites
    hosts = sorted(h for h in manifest["host_permissions"])
    urls = sorted(re.sub(r"^(https://[^/]+)/.*$", r"\1/*", s["url"]) for s in refs.services().values())
    assert hosts == urls and not any("*." in h or "all_urls" in h for h in hosts)
