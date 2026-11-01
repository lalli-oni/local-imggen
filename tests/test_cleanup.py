"""Icon cleanup post steps (#4): keep_largest drops stray shapes left by rembg, trim centres the subject.

Shapes are drawn on transparent canvases, so these run without rembg (fake_rembg can return them too).
"""
from __future__ import annotations

import io
import time

from PIL import Image, ImageDraw

import imagegen
import mcp_server
from test_cli import run_main

SOLID = (200, 40, 40, 255)


def cutout_png(path, shapes, size=(1024, 1024), lines=()):
    """A transparent canvas with filled rectangles (x0, y0, x1, y1) and 1 px lines on it."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for box in shapes:
        d.rectangle(box, fill=SOLID)
    for line in lines:
        d.line(line, fill=SOLID, width=1)
    im.save(path, "PNG")
    return path


def alpha_at(path, xy):
    with Image.open(path) as im:
        return im.convert("RGBA").getpixel(xy)[3]


def opaque_box(path):
    with Image.open(path) as im:
        return im.getchannel("A").point(lambda v: 255 if v > imagegen.ALPHA_SOLID else 0).getbbox()


def test_keep_largest_drops_small_islands(tmp_path):
    p = cutout_png(tmp_path / "a-transparent.png",
                   [(300, 300, 700, 700),      # the icon
                    (20, 20, 60, 60),          # stray bits rembg left in the corners
                    (950, 900, 1000, 1010)])
    assert imagegen.keep_largest(p) == p
    assert alpha_at(p, (500, 500)) == 255
    assert alpha_at(p, (300, 300)) == 255 and alpha_at(p, (700, 700)) == 255  # edges not eaten
    assert alpha_at(p, (40, 40)) == 0
    assert alpha_at(p, (975, 950)) == 0
    assert opaque_box(p) == (300, 300, 701, 701)


def test_keep_largest_keeps_big_second_part_and_soft_edges(tmp_path):
    p = tmp_path / "b.png"
    im = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((100, 100, 500, 500), fill=SOLID)
    d.rectangle((600, 600, 800, 800), fill=SOLID)  # a quarter of the main part's area: kept
    d.line((99, 100, 99, 500), fill=(200, 40, 40, 90))  # anti-aliased fringe
    im.save(p)
    before = p.read_bytes()
    imagegen.keep_largest(p)
    assert p.read_bytes() == before  # nothing dropped -> file untouched
    assert alpha_at(p, (700, 700)) == 255 and alpha_at(p, (99, 300)) == 90


def test_keep_largest_follows_thin_connections(tmp_path):
    """A 1 px stroke (antenna, string) joining two parts keeps them one shape, even on the coarse grid."""
    p = cutout_png(tmp_path / "c.png", [(100, 400, 500, 800), (850, 100, 870, 120)],
                   lines=[(500, 400, 860, 110)])
    imagegen.keep_largest(p)
    assert alpha_at(p, (860, 110)) == 255
    assert alpha_at(p, (680, 255)) == 255  # on the line


def test_keep_largest_leaves_images_without_transparency(tmp_path, make_image):
    rgb = make_image("raw.png", size=(256, 256))
    before = rgb.read_bytes()
    assert imagegen.keep_largest(rgb) == rgb
    assert rgb.read_bytes() == before
    empty = cutout_png(tmp_path / "empty.png", [])
    before = empty.read_bytes()
    imagegen.keep_largest(empty)
    assert empty.read_bytes() == before


def test_keep_largest_is_fast_enough(tmp_path):
    """Pure Python on a 256 grid: a busy 1024 cut-out (lots of specks) must not stall the run."""
    specks = [(x, y, x + 6, y + 6) for x in range(0, 1024, 40) for y in range(0, 1024, 40)]
    p = cutout_png(tmp_path / "busy.png", [(300, 300, 700, 700)] + specks)
    t0 = time.time()
    imagegen.keep_largest(p)
    assert time.time() - t0 < 5
    assert alpha_at(p, (3, 3)) == 0 and alpha_at(p, (500, 500)) == 255


def test_trim_centres_on_a_square(tmp_path):
    p = cutout_png(tmp_path / "t.png", [(600, 50, 999, 249)])  # 400x200, off in a corner
    imagegen.trim(p)
    with Image.open(p) as im:
        assert im.size == (1024, 1024) and im.mode == "RGBA"
    x0, y0, x1, y1 = opaque_box(p)
    margin = round(1024 * imagegen.TRIM_MARGIN)
    assert abs(x0 - margin) <= 2 and abs((1024 - x1) - margin) <= 2  # the long side fills the inner square
    assert abs(y0 - (1024 - y1)) <= 2  # centred vertically
    assert 400 < y1 - y0 < 480  # aspect kept: half as tall as wide


def test_trim_non_square_canvas_and_noop_cases(tmp_path, make_image):
    p = cutout_png(tmp_path / "wide.png", [(10, 10, 50, 50)], size=(1344, 768))
    imagegen.trim(p)
    with Image.open(p) as im:
        assert im.size == (768, 768)
    empty = cutout_png(tmp_path / "e.png", [])
    before = empty.read_bytes()
    imagegen.trim(empty)
    assert empty.read_bytes() == before
    rgb = make_image("rgb.png")
    before = rgb.read_bytes()
    imagegen.trim(rgb)
    assert rgb.read_bytes() == before


def test_icon_preset_cleans_cutouts(comfy, fake_rembg, tmp_path):
    buf = io.BytesIO()
    im = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((100, 100, 400, 400), fill=SOLID)
    d.rectangle((900, 900, 950, 950), fill=SOLID)
    im.save(buf, "PNG")
    fake_rembg.output = buf.getvalue()
    assert imagegen.load_config()["presets"]["icon"]["post"] == ["remove_bg", "keep_largest", "trim"]
    run = imagegen.generate("rocket", "icon", count=1, seed=1)
    out = run["images"][0]["path"]
    assert out.endswith("-transparent.png")
    x0, y0, x1, y1 = opaque_box(out)
    assert abs(x0 - (1024 - x1)) <= 2 and abs(y0 - (1024 - y1)) <= 2  # centred: the speck is gone
    assert x1 - x0 > 800


def test_cutout_clean_cli_and_mcp(make_image, fake_rembg, capsys, tmp_path):
    buf = io.BytesIO()
    cutout_png(buf, [(100, 100, 400, 400), (900, 900, 950, 950)])
    fake_rembg.output = buf.getvalue()

    code, out, _ = run_main(["cutout", str(make_image("plain.png"))], capsys)
    assert code == 0
    assert opaque_box(out.strip()) == (100, 100, 951, 951)  # no --clean: speck stays

    code, out, _ = run_main(["cutout", "--clean", str(make_image("logo.png"))], capsys)
    assert code == 0 and out.strip().endswith("logo-transparent.png")
    x0, _, x1, _ = opaque_box(out.strip())
    assert abs(x0 - (1024 - x1)) <= 2

    res = mcp_server.cutout(str(make_image("m.png")), clean=True)
    x0, _, x1, _ = opaque_box(res["path"])
    assert abs(x0 - (1024 - x1)) <= 2
