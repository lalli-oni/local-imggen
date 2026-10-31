"""Contact sheet (index.html): static checks on what write_contact_sheet inlines.

The page itself is checked in a real browser in test_contact_sheet_browser.py (skipped without Playwright).
"""
import json
import re
from pathlib import Path

import pytest

import imagegen


def sheet_data(run_dir: Path) -> dict:
    html = (Path(run_dir) / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script id="run-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "run data block missing"
    return json.loads(m.group(1))


def test_inlines_run_data(make_run):
    run = make_run(n=3)
    imagegen.write_contact_sheet(Path(run["run_dir"]), run)
    d = sheet_data(run["run_dir"])
    assert [im["file"] for im in d["images"]] == ["01-seed100.png", "02-seed101.png", "03-seed102.png"]
    assert d["images"][0]["width"] == 1024 and d["negative"] == "blurry"
    assert d["transparent"] is False and d["source_file"] is None and d["project"] is None


def test_no_placeholders_left(make_run):
    run = make_run(n=1)
    out = imagegen.write_contact_sheet(Path(run["run_dir"]), run)
    assert "{{" not in out.read_text()


def test_script_breakout_is_escaped(make_run):
    evil = "</script><script>alert(1)</script>"
    run = make_run(n=1, prompt="x")
    run["prompt"] = evil
    out = imagegen.write_contact_sheet(Path(run["run_dir"]), run)
    html = out.read_text()
    assert evil not in html
    assert sheet_data(run["run_dir"])["prompt"] == evil
    assert "<title>&lt;/script&gt;" in html


def test_transparent_guessed_for_old_runs(make_run):
    run = make_run(n=2, transparent=True)
    del run["transparent"]  # run.json from before the field existed
    imagegen.write_contact_sheet(Path(run["run_dir"]), run)
    assert sheet_data(run["run_dir"])["transparent"] is True


def test_source_image_copied_for_old_img2img_runs(make_run, make_image):
    src = make_image("orig.png")
    run = make_run(n=1, source_image=str(src), mask=None, denoise=0.4)
    imagegen.write_contact_sheet(Path(run["run_dir"]), run)
    d = sheet_data(run["run_dir"])
    assert d["source_image"] == "orig.png" and d["source_file"] == imagegen.SHEET_SOURCE
    assert (Path(run["run_dir"]) / imagegen.SHEET_SOURCE).is_file()


def test_missing_source_still_writes_sheet(make_run):
    run = make_run(n=1, source_image="/gone/orig.png", denoise=0.4)
    imagegen.write_contact_sheet(Path(run["run_dir"]), run)
    d = sheet_data(run["run_dir"])
    assert d["source_image"] == "orig.png" and d["source_file"] is None


@pytest.mark.parametrize("how", ["dir", "run.json", "index.html", "image", "relative", "dropped-slash"])
def test_find_run_files(make_run, out_dir, how):
    run = make_run(n=1)
    d = Path(run["run_dir"])
    arg = {"dir": str(d), "run.json": str(d / "run.json"), "index.html": str(d / "index.html"),
           "image": run["images"][0]["path"], "relative": str(d.relative_to(out_dir)),
           "dropped-slash": str(d).lstrip("/")}[how]
    assert imagegen.find_run_files(arg) == [d / "run.json"]


def test_find_run_files_missing(out_dir):
    with pytest.raises(FileNotFoundError):
        imagegen.find_run_files("2020-01-01")
