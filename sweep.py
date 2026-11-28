"""gen sweep (#17): one prompt, many settings, laid out as a labelled grid on one contact sheet.

    gen sweep "prompt" [-p klein,qwen] [--steps 4,8,16 | --steps klein=4,8,16 --steps qwen=15,25,40] [--cfg 3,5]
              [--guidance G,..] [--style a,none] [--seeds N] [--seed S] [--size 1344x768] [--rows AXIS] [-y]

The axes are the model (a preset: its model and workflow, so this is also the model comparison for #64),
style, steps, cfg, guidance and seed. Every axis you give more than one value is varied; the result is the
Cartesian product, one image per cell, the same prompt and the same seeds in every cell. An axis you leave
out keeps each preset's own value (style: the preset's own look; use `--style none` to give every model
the same bare prompt). With `--seeds N` the seeds are base, base+1, ... and cells in a row share one.

Steps can differ per model (#94): `--steps klein=4,8,16 --steps qwen=15,25,40` gives each model its own list, so a
model is compared at its own best step count. A bare list is every model's; a model named in neither keeps its
own default. When the models' lists differ, the rows default to the seed (or none) and each model's steps are
its own columns, so no cell is empty.

Layout: with one varying axis it runs along the columns. With more, the rows are `--rows AXIS` (default: seed
if it varies, else the first axis in the order model, style, steps, cfg, guidance) and the columns are
the other axes combined ("klein · steps 8").

A preset with no input for a setting (klein has no cfg) renders that row once and shows the other values
as skipped, since they would be the same image. Presets run one after another with models unloaded in
between (like gen bakeoff, whose Sampler gives each cell its time and peak VRAM/RAM), and the sheet is
rewritten after every cell and on Ctrl-C. More than MAX_CELLS cells needs a yes (-y).

Output: <output_dir>/<date>/<time>-sweep-<slug>/ with copies of the images and sidecars (params in each),
run.json, bakeoff.json (the numbers per cell) and index.html (the grid).
"""
from __future__ import annotations

import argparse
import itertools
import random
import re
import sys
from pathlib import Path

import bakeoff
import imagegen

MAX_CELLS = 16
AXES = ("preset", "style", "steps", "cfg", "guidance", "seed")  # default row/column order
NAMES = {"preset": "model", "style": "style", "steps": "steps", "cfg": "cfg", "guidance": "guidance",
         "seed": "seed"}
PARAM_AXES = ("steps", "cfg", "guidance")  # a workflow needs a {{key}} for these to do anything


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def _sv(v) -> str:  # a steps value in a message: None is the preset's own
    return "preset default" if v is None else _num(v)


def _uniq(values: list, what: str) -> list:
    if len(set(map(str, values))) != len(values):
        raise ValueError(f"{what}: a value is listed twice")
    return values


def _union(lists) -> list:
    """the values of several lists, once each, in order"""
    out: list = []
    for vs in lists:
        out += [v for v in vs if v not in out]
    return out


def _style_sets(values: list[str] | None, conf: dict) -> list[list[str]] | None:
    """["synthwave", "none", "a+b"] -> [["synthwave"], [], ["a", "b"]]"""
    if not values:
        return None
    sets = [[] if v.strip().lower() == "none" else [s for s in v.split("+") if s.strip()] for v in values]
    for s in sets:
        imagegen.check_styles(s, conf)
    return _uniq(sets, "styles")


def _style_label(s: list[str]) -> str:
    return "+".join(s) or "none"


def _steps_map(steps, presets: list[str]) -> dict[str, list]:
    """steps as given -> {preset: its steps values} (None = the preset's own default).
    A list is every model's. A dict is {"*": list, model: list}: a model's own list wins over "*"."""
    if not steps:
        return {p: [None] for p in presets}
    if isinstance(steps, dict):
        steps = dict(steps)
        if bad := [k for k in steps if k != "*" and k not in presets]:
            raise ValueError(f"steps for {', '.join(bad)}, which isn't one of the models (-p {','.join(presets)})")
    else:
        steps = {"*": list(steps)}
    out = {}
    for p in presets:
        vals = steps.get(p, steps.get("*"))
        out[p] = _uniq(list(vals), f"steps for {p}") if vals else [None]
    if bad := [v for vs in out.values() for v in vs if v is not None and not 1 <= v <= 150]:
        raise ValueError(f"steps must be 1-150 (got {bad[0]})")
    return out


def plan(prompt: str, presets: list[str], conf: dict, steps=None, cfg=None, guidance=None, styles=None,
         seeds: int = 1, seed: int | None = None, rows: str | None = None) -> dict:
    """The grid without running anything: axes, rows, columns and one cell per combination.
    steps: a list (every model) or {"model": [..], "*": [..]} (per model, #94).
    cells: [{name, r, c, preset, steps, cfg, guidance, styles, seed, label, skip}]; skip = why it isn't rendered."""
    pconf = conf["presets"]
    if not prompt.strip():
        raise ValueError("sweep needs a prompt")
    if bad := [p for p in presets if p not in pconf]:
        raise ValueError(f"unknown preset: {', '.join(bad)}")
    if bad := [p for p in presets if "workflow" not in pconf[p] or imagegen.is_cast_preset(pconf[p])]:
        raise ValueError(f"not a text-to-image preset: {', '.join(bad)}")
    base_seed = seed if seed is not None else random.randrange(imagegen.MAX_SEED)
    seeds = int(seeds)
    if not 1 <= seeds <= 64:
        raise ValueError("--seeds must be 1-64")
    per_steps = _steps_map(steps, list(presets))
    vals = {"preset": _uniq(list(presets), "presets"), "style": styles or [None],
            "steps": _union(per_steps.values()),
            "cfg": _uniq(list(cfg), "cfg") if cfg else [None],
            "guidance": _uniq(list(guidance), "guidance") if guidance else [None],
            "seed": [base_seed + i for i in range(seeds)]}

    def own(a: str, p: str) -> list:  # the values of axis a that preset p is run with
        return per_steps[p] if a == "steps" else vals[a]
    if bad := [v for v in vals["cfg"] + vals["guidance"] if v is not None and not 0 < v <= 100]:
        raise ValueError(f"cfg and guidance must be above 0 (got {bad[0]})")
    varying = [a for a in AXES if len(vals[a]) > 1]
    if not varying:
        raise ValueError("nothing to sweep: give an axis more than one value "
                         "(-p a,b / --steps 4,8 / --cfg 3,5 / --guidance / --style a,none / --seeds 3)")
    if rows is not None and rows not in varying:
        raise ValueError(f"--rows {rows}: that axis doesn't vary (varying: {', '.join(varying)})")
    # models with their own step lists: their steps are columns, not shared rows
    ragged = any(v != vals["steps"] for v in per_steps.values())
    row_axis = None
    if len(varying) > 1:
        if rows:
            row_axis = rows
        elif "seed" in varying:
            row_axis = "seed"
        elif not ragged:
            row_axis = varying[0]
    col_axes = [a for a in varying if a != row_axis]

    def show(axis, v):
        if axis == "preset":
            model = pconf[v].get("model", v)
            return f"{model} ({v})" if sum(pconf[p].get("model", p) == model for p in presets) > 1 else model
        if axis == "style":
            return _style_label(v)
        if v is None:  # steps None = the model's own default (a preset without steps input is rare)
            return "steps preset default" if axis == "steps" else f"{axis} n/a"
        return f"{axis} {_num(v)}"

    def lab(axes, combo):
        return " · ".join(show(a, combo[a]) for a in axes)
    row_keys = [{row_axis: v} for v in vals[row_axis]] if row_axis else [{}]
    col_keys = []
    if "preset" in col_axes:  # each model's columns come from its own steps
        for p in presets:
            axes_vals = [[p] if a == "preset" else own(a, p) for a in col_axes]
            col_keys += [dict(zip(col_axes, vs)) for vs in itertools.product(*axes_vals)]
    else:
        col_keys = [dict(zip(col_axes, vs)) for vs in itertools.product(*(vals[a] for a in col_axes))]
    texts = {}
    for p in presets:
        t = (imagegen.ROOT / "workflows" / pconf[p]["workflow"]).read_text()
        texts[p] = {k: "{{" + k + "}}" in t for k in PARAM_AXES}
    for k in PARAM_AXES:
        if len(vals[k]) > 1 and not any(texts[p][k] for p in presets):
            raise ValueError(f"no chosen preset has a {k} input, so {k} can't vary "
                             f"(presets with one: {', '.join(n for n, q in pconf.items() if q.get('workflow') and '{{' + k + '}}' in (imagegen.ROOT / 'workflows' / q['workflow']).read_text()) or 'none'})")
    cells = []
    for r, rk in enumerate(row_keys):
        for c, ck in enumerate(col_keys):
            combo = {a: vals[a][0] for a in AXES} | rk | ck
            p = combo["preset"]
            if "steps" not in rk and "steps" not in ck:
                combo["steps"] = per_steps[p][0]  # the model's own first steps
            eff, skip = dict(combo), None  # eff: what generate() gets
            if combo["steps"] not in per_steps[p]:
                skip = (f"{p} has no steps {_sv(combo['steps'])} in its list "
                        f"({', '.join(_sv(v) for v in per_steps[p])})")
            for k in PARAM_AXES:
                if combo[k] is not None and not texts[p][k]:
                    eff[k] = None
                    if combo[k] != own(k, p)[0]:
                        skip = skip or (f"{pconf[p]['workflow']} has no {k} input: "
                                        f"same image as {k} {_sv(own(k, p)[0])}")
                    else:
                        combo[k] = None  # shown as "n/a"
            cells.append({"name": f"c{len(cells) + 1:02d}", "r": r, "c": c, "preset": p,
                          "steps": eff["steps"], "cfg": eff["cfg"], "guidance": eff["guidance"],
                          "styles": eff["style"], "seed": eff["seed"], "skip": skip,
                          "label": lab(([row_axis] if row_axis else []) + col_axes, combo)})
    counts = {a: len(vals[a]) for a in varying}
    if ragged and "steps" in counts:  # the title can't count steps values: the cells are per model
        counts["steps"] = "per-model"
    return {"prompt": prompt, "base_seed": base_seed, "varying": varying, "row_axis": row_axis,
            "counts": counts, "col_axes": col_axes, "cells": cells,
            "rows": [lab([row_axis], k) for k in row_keys] if row_axis else [],
            "cols": [lab(col_axes, k) for k in col_keys]}


def sweep(prompt: str, presets: list[str] | tuple[str, ...] = ("draft",), steps=None,
          cfg: list[float] | None = None, guidance: list[float] | None = None, styles: list[str] | None = None,
          seeds: int = 1, seed: int | None = None, size: tuple[int, int] | None = None, rows: str | None = None,
          confirm: bool = False, free: bool = True, label: str | None = None, interval: float = 1.0,
          max_cells: int = MAX_CELLS) -> dict:
    """Run the sweep; returns the run dict (bakeoff.json's numbers are in run["bakeoff"], the grid in run["sweep"]).
    steps: a list for every model, or {"model": [..]} per model (see plan)."""
    conf = imagegen.load_config()
    pl = plan(prompt.strip(), list(presets), conf, steps, cfg, guidance, _style_sets(styles, conf), seeds, seed,
              rows)
    todo = [c for c in pl["cells"] if not c["skip"]]
    if len(todo) > max_cells and not confirm:
        raise ValueError(f"{len(todo)} images is more than {max_cells}: that is a long run. "
                         f"Cut an axis, or confirm it (gen sweep -y / sweep(confirm=True))")
    if size is not None:
        w, h = (int(v) for v in size)
        if w < 256 or h < 256 or w % 16 or h % 16:
            raise ValueError(f"size {w}x{h}: each side needs to be 256 or more and a multiple of 16")
    else:
        w = h = None
    comfy = imagegen.Comfy(conf["comfy_url"])
    comfy.wait_ready()
    bakeoff._require_idle(comfy)
    out = bakeoff._out_dir(conf, label or prompt, "sweep")
    pconf = conf["presets"]

    def call(c):
        def run(n, text):
            return imagegen.generate(text, c["preset"], 1, c["seed"], width=w, height=h, steps=c["steps"],
                                     cfg=c["cfg"], guidance=c["guidance"], styles=c["styles"],
                                     label=f"sweep-{c['name']}-{imagegen.slug(text, 20)}")
        return run
    missing = {p: bakeoff._missing_nodes(comfy, pconf[p]["workflow"]) for p in dict.fromkeys(c["preset"] for c in todo)}
    columns = []
    for p in dict.fromkeys(c["preset"] for c in pl["cells"]):  # a preset's cells together: it loads once
        cols = []
        for c in (x for x in pl["cells"] if x["preset"] == p):
            action = c["skip"] or (f"custom node not installed (gen doctor): ComfyUI has no {', '.join(missing[p])}"
                                   if missing.get(p) else call(c))
            cols.append((c["name"], c["label"], action, [(1, pl["prompt"])]))
        columns.append((p, cols))

    def build(results, images, done=True):
        return {"preset": "sweep: " + " × ".join(f"{n} {NAMES[a]}" for a, n in pl["counts"].items())
                + ("" if done else bakeoff.STOPPED),
                "prompt": pl["prompt"], "base_seed": pl["base_seed"], "count": len(images), "run_dir": str(out),
                "images": images, "transparent": False, "source_image": None, "mask": None, "denoise": None,
                "edit": False,
                "sweep": {k: pl[k] for k in ("row_axis", "col_axes", "rows", "cols", "varying")}
                | {"cells": [{k: c[k] for k in ("name", "r", "c", "label")} for c in pl["cells"]]},
                "bakeoff": {"mode": "sweep", "instructions": [pl["prompt"]], "count": 1, "complete": done,
                            "size": f"{w}x{h}" if w else None, "presets": results}}
    results, images = bakeoff._bake(comfy, out, columns, free, interval,
                                    save=lambda r, i, done: bakeoff._finish(out, build(r, i, done)))
    return bakeoff._finish(out, build(results, images))


def _list(conv):
    def parse(v: str) -> list:
        try:
            return [conv(x) for x in v.split(",") if x.strip()]
        except ValueError:
            raise argparse.ArgumentTypeError(f"comma-separated numbers, not {v!r}")
    return parse


def _steps_arg(tokens: list[str]) -> dict:
    """["4,8", "klein=4,8,16", "qwen=15,25"] -> {"*": [4, 8], "klein": [4, 8, 16], "qwen": [15, 25]}"""
    out: dict[str, list[int]] = {}
    for t in tokens:
        name, sep, nums = t.partition("=")
        if not sep:
            name, nums = "*", t
        name = name.strip()
        if name in out:
            raise ValueError(f"steps: {name} is given twice" if name != "*"
                             else "steps: give one bare list (for every model), or one list per model")
        vals = _list(int)(nums)
        if not vals:
            raise ValueError(f"steps for {name}: no numbers")
        out[name] = vals
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="gen sweep", description="One prompt over a grid of settings (model, steps, cfg, guidance, style, "
        "seeds), same seeds in every cell, on one labelled contact sheet. Each axis with several values "
        "(comma-separated) is varied; the cells are the product.")
    ap.add_argument("prompt")
    ap.add_argument("-p", "--presets", default="draft", help="comma-separated presets = models to compare (default draft)")
    ap.add_argument("--steps", action="append", metavar="A,B | MODEL=A,B",
                    help="sampling steps: one list for every model (4,8,16), or one per model: repeat the flag, "
                    "--steps klein=4,8,16 --steps qwen=15,25,40 (a bare list covers the models not named)")
    ap.add_argument("--cfg", type=_list(float), metavar="A,B", help="CFG (SDXL, Qwen)")
    ap.add_argument("--guidance", type=_list(float), metavar="A,B", help="guidance (FLUX)")
    ap.add_argument("--style", help="comma-separated looks, e.g. synthwave,none (a+b = both; default: each preset's own)")
    ap.add_argument("--seeds", type=int, default=1, help="seeds per cell: base, base+1, ... (default 1)")
    ap.add_argument("--seed", type=int, help="base seed (default random)")
    ap.add_argument("--size", type=bakeoff._parse_size, help="WxH for every cell (default each preset's own)")
    ap.add_argument("--rows", choices=AXES, help="the axis down the side (default seed, else the first varying)")
    ap.add_argument("-y", "--yes", action="store_true", help=f"allow more than {MAX_CELLS} images")
    ap.add_argument("--no-free", action="store_true", help="don't unload models between presets")
    ap.add_argument("--label", help="folder name instead of the prompt's")
    ap.add_argument("--open", action="store_true", help="open the contact sheet in the Windows browser")
    a = ap.parse_args(argv)
    presets = [p.strip() for p in re.split(r"[,\s]+", a.presets) if p.strip()]
    styles = [s.strip() for s in a.style.split(",") if s.strip()] if a.style else None
    try:
        steps = _steps_arg(a.steps) if a.steps else None
    except (ValueError, argparse.ArgumentTypeError) as e:
        ap.error(str(e))
    confirm = a.yes
    if not confirm:
        try:  # show the size of the grid before refusing, so the answer can be a yes
            conf = imagegen.load_config()
            n = sum(not c["skip"] for c in plan(a.prompt.strip(), presets, conf, steps, a.cfg, a.guidance,
                                                _style_sets(styles, conf), a.seeds, a.seed, a.rows)["cells"])
            if n > MAX_CELLS and sys.stdin.isatty():
                confirm = input(f"{n} images is a long run. Go on? [y/N] ").strip().lower() in ("y", "yes")
        except ValueError:
            pass  # sweep() raises the same error with its message
    run = sweep(a.prompt, presets, steps, a.cfg, a.guidance, styles, a.seeds, a.seed, a.size, a.rows, confirm,
                not a.no_free, a.label)
    print(bakeoff.table(run))
    print(f"\ncontact sheet: {run['contact_sheet_windows'] or run['contact_sheet']}")
    print(f"numbers: {Path(run['run_dir']) / 'bakeoff.json'}")
    if a.open:
        imagegen.open_in_windows(run["contact_sheet"])
    return 1 if any(r["error"] for r in run["bakeoff"]["presets"]) else 0
