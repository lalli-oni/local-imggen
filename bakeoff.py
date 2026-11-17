"""gen bakeoff: the same prompts through several models, side by side on one contact sheet.

    gen bakeoff IMAGE "instruction" ["instruction" ...] [-p kontext,klein,qwen] [-n 2] [--seed S]   (#40)
    gen bakeoff --t2i "prompt" ["prompt" ...] [--ref IMAGE] [-p draft,dreamshaper,klein,qwen]
                [--size 1344x768] [-n 2] [--seed S]                                                 (#51)

Edit mode (#40): every instruction is an edit of IMAGE (gen edit) through each edit preset.
Text-to-image mode (--t2i, #51): every prompt goes through each preset's text-to-image workflow at the
same size (--size). With --ref, presets that can edit also run each prompt with the ref image as
the character reference (gen edit of the ref, with REF_SUFFIX added); those columns are named
"<preset>+ref" and their size follows the ref's aspect ratio. Presets with no ref input (SDXL) say so.

For each preset in turn: ComfyUI unloads every model first (/free), so each model's first image
includes loading it from disk (a preset's +ref column runs right after, with the model loaded), then
every prompt runs with the same seeds as the other presets. While a column runs, a sampler polls
once a second for peak VRAM (whole GPU, from ComfyUI's /system_stats), the comfyui process's RAM and
swap (/proc/<MainPID>/status) and system swap (/proc/meminfo). A column that fails (models missing,
ComfyUI OOM-killed) is recorded and the bake-off moves on to the next one.

Output: <output_dir>/<date>/<time>-bakeoff-<slug>/ with copies of every image (and its sidecar),
run.json, bakeoff.json (the numbers) and index.html: the normal contact sheet with each image
tagged by column and prompt number, and a table of the numbers. The per-column runs stay where
gen / gen edit put them too.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import statistics
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import imagegen

DEFAULT_PRESETS = ("kontext", "klein", "qwen")
SETTLE_MAX = 20.0   # seconds to wait for /free to take effect before a preset starts
SETTLE_STEP = 0.5
GIB = 1024 ** 3
PROC = Path("/proc")  # tests point this at a fake /proc


def _comfy_pid() -> int | None:
    try:
        out = subprocess.run(["systemctl", "show", "-P", "MainPID", "comfyui"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out) or None
    except Exception:
        return None


def _proc_kib(path: Path, keys: tuple[str, ...]) -> dict[str, int]:
    """Values in kB from a /proc file with "Key:   123 kB" lines."""
    out = {}
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition(":")
            if k in keys and v.split():
                out[k] = int(v.split()[0])
    except (OSError, ValueError):
        pass
    return out


class Sampler:
    """Peak memory while a preset runs: a thread polling every `interval` seconds until stop()."""

    def __init__(self, comfy: imagegen.Comfy, interval: float = 1.0):
        self.comfy, self.interval, self.proc = comfy, interval, PROC
        self.pid = _comfy_pid()
        self.peak: dict[str, float] = {}
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _max(self, key: str, gib: float | None) -> None:
        if gib is not None:
            self.peak[key] = max(self.peak.get(key, 0.0), gib)

    def sample(self) -> None:
        try:
            st = self.comfy.system_stats()
            dev = next((d for d in st.get("devices", []) if "vram_total" in d), None)
            if dev:
                self._max("vram_gib", (dev["vram_total"] - dev["vram_free"]) / GIB)
                self.peak["vram_total_gib"] = dev["vram_total"] / GIB
            sysm = st.get("system", {})
            if "ram_total" in sysm and "ram_free" in sysm:
                self._max("ram_gib", (sysm["ram_total"] - sysm["ram_free"]) / GIB)
        except Exception:
            pass  # ComfyUI busy loading or restarting: keep the samples we have
        mem = _proc_kib(self.proc / "meminfo", ("SwapTotal", "SwapFree"))
        if len(mem) == 2:
            self._max("swap_gib", (mem["SwapTotal"] - mem["SwapFree"]) / 1024 ** 2)
        if self.pid:
            st = _proc_kib(self.proc / str(self.pid) / "status", ("VmRSS", "VmSwap"))
            if "VmRSS" in st:
                self._max("comfy_rss_gib", st["VmRSS"] / 1024 ** 2)
            if "VmSwap" in st:
                self._max("comfy_swap_gib", st["VmSwap"] / 1024 ** 2)
        self.samples += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval)

    def start(self) -> "Sampler":
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=10)
        self.sample()
        return {k: round(v, 2) for k, v in self.peak.items()}


def _vram_used(comfy: imagegen.Comfy) -> float | None:
    try:
        dev = next(d for d in comfy.system_stats().get("devices", []) if "vram_total" in d)
        return (dev["vram_total"] - dev["vram_free"]) / GIB
    except Exception:
        return None


def _unload(comfy: imagegen.Comfy) -> None:
    """/free only sets flags that ComfyUI's worker acts on a moment later: wait until VRAM use stops
    falling, so the next preset's numbers don't include the previous model."""
    comfy.free()
    t0, last = time.time(), _vram_used(comfy)
    while time.time() - t0 < SETTLE_MAX:
        time.sleep(SETTLE_STEP)
        now = _vram_used(comfy)
        if now is None or last is None or (now >= last - 0.05 and time.time() - t0 >= 2 * SETTLE_STEP):
            return
        last = now


def _require_idle(comfy: imagegen.Comfy) -> None:
    """A bake-off times every model: jobs already in ComfyUI's queue (a stopped run, gen daily) would be
    counted in the first model's time, or hold the whole bake-off up (#51). Stop with the commands to clear it."""
    try:
        running, waiting = comfy.busy()
    except Exception:
        return  # can't tell: go ahead
    if running or waiting:
        raise RuntimeError(
            f"ComfyUI is busy ({running} running, {waiting} waiting), so the timings would be off. Wait for it, "
            f"or clear the queue and stop the running job:\n"
            f"  curl --silent --request POST --header 'Content-Type: application/json' --data '{{\"clear\": true}}' "
            f"{comfy.url}/queue\n  curl --silent --request POST {comfy.url}/interrupt")


def _check_presets(presets: list[str], conf: dict, t2i: bool = False) -> None:
    key, what = ("workflow", "text-to-image") if t2i else ("edit_workflow", "edit")
    bad = [p for p in presets if key not in conf.get(p, {})]
    if bad:
        ok = [n for n, q in conf.items() if key in q]
        raise ValueError(f"not a{'n' if what[0] == 'e' else ''} {what} preset: {', '.join(bad)} "
                         f"({what} presets: {', '.join(ok)})")
    if len(set(presets)) != len(presets):
        raise ValueError("a preset is listed twice")


def _clean(texts: list[str], what: str) -> list[str]:
    texts = [t.strip() for t in texts]
    if not texts or not all(texts):
        raise ValueError(f"bakeoff needs at least one {what} (and no empty ones)")
    return texts


def _size_steps_of(side: dict | None) -> tuple[str | None, int | None]:
    """WxH and sampling steps from an image's sidecar."""
    prm = (side or {}).get("params", {})
    size = next((f"{prm[w]}x{prm[h]}" for w, h in (("target_width", "target_height"), ("out_width", "out_height"),
                                                   ("width", "height"))
                 if w in prm and h in prm), None)  # target_*: after an upscale; out_*: wallpaper_ref's own size
    return size, prm.get("steps")


def _size_steps(run: dict) -> tuple[str | None, int | None]:
    """WxH and sampling steps of a run's images, from the first image's sidecar."""
    return _size_steps_of(imagegen.find_sidecar(Path(run["images"][0]["path"])) if run.get("images") else None)


def _bake(comfy: imagegen.Comfy, out: Path, columns: list[tuple[str, list[tuple]]], free: bool,
          interval: float, save=None) -> tuple[list[dict], list[dict]]:
    """Run the columns. columns: [(preset, [(column name, label, call or skip note, items), ...]), ...], one
    /free per preset. items: [(prompt #, text), ...]; call(n, text) -> a generate() run dict; a str instead of
    a call = skipped, with that note. label names the model and steps on the sheet ("FLUX.2 klein 4B · 16 steps").
    save(results, images, done) is called after every column, so a long bake-off has a sheet to look at while
    it runs, and one that is stopped (Ctrl-C) or crashes keeps what it got (not-run columns are listed as such).
    Returns (per-column results, sheet images in prompt-major order)."""
    results = []
    copied: dict[tuple[int, str], list[dict]] = {}  # (prompt #, column) -> image entries
    numbers = sorted({n for _, cols in columns for c in cols for n, _ in c[3]})

    def collect(pending_note: str | None = None) -> tuple[list[dict], list[dict]]:
        ran = {r["preset"] for r in results}
        rest = [{"preset": c[0], "label": c[1], "model": p, "runs": [], "seconds": [], "error": None,
                 "note": pending_note, "images": 0, "peak": {}, "first_seconds": None, "median_seconds": None}
                for p, cols in columns for c in cols if c[0] not in ran] if pending_note else []
        res = results + rest
        names = [r["preset"] for r in res]
        return res, [e for n in numbers for c in names for e in copied.get((n, c), [])]

    def checkpoint(note: str | None, done: bool = False) -> None:
        if save is None:
            return
        try:
            save(*collect(note), done)
        except Exception as e:  # a sheet problem must not lose the bake-off
            imagegen.log(f"   could not write the sheet so far ({type(e).__name__}: {e})")

    res = None
    try:
        for preset, cols in columns:
            imagegen.log(f"== {preset}")
            if free and any(callable(c[2]) for c in cols):
                try:
                    _unload(comfy)
                except Exception as e:
                    imagegen.log(f"   could not unload models first ({e}); load times may be off")
            for name, label, call, items in cols:
                res = {"preset": name, "label": label, "model": preset, "runs": [], "seconds": [], "error": None}
                results.append(res)
                if not callable(call):
                    res.update(note=call, images=0, peak={}, first_seconds=None, median_seconds=None)
                    imagegen.log(f"   {name}: skipped ({call})")
                    continue
                sampler = Sampler(comfy, interval).start()
                t0 = time.time()
                try:
                    for n, text in items:
                        run = call(n, text)
                        res["runs"].append(run["run_dir"])
                        if not res.get("size"):
                            res["size"], res["steps"] = _size_steps(run)
                        entries = copied.setdefault((n, name), [])
                        for im in run["images"]:
                            if im.get("seconds") is not None:  # one per job (a job may save several images)
                                res["seconds"].append(im["seconds"])
                            src = Path(im["path"])
                            dest = out / f"{n:02d}-{name}-{src.name}"  # "klein+ref": + is fine in file names
                            shutil.copy2(src, dest)
                            if (side := src.with_suffix(".json")).is_file():
                                shutil.copy2(side, dest.with_suffix(".json"))
                            entries.append({"path": str(dest), "seed": im["seed"], "seconds": im.get("seconds"),
                                            "timing": im.get("timing"), "tag": f"{label} · {n}"})  # per stage
                        if not (out / imagegen.SHEET_SOURCE).is_file() and \
                                (s := Path(run["run_dir"]) / imagegen.SHEET_SOURCE).is_file():
                            shutil.copy2(s, out / imagegen.SHEET_SOURCE)
                except Exception as e:  # missing models, rejected graph, OOM: note it and try the next one
                    res["error"] = f"{type(e).__name__}: {e}"
                    imagegen.log(f"   {name} failed: {res['error']}")
                finally:  # also on Ctrl-C: the numbers so far go on the sheet
                    res["total_seconds"] = round(time.time() - t0, 1)
                    res["peak"] = sampler.stop()
                    secs = res["seconds"]
                    res["first_seconds"] = secs[0] if secs else None  # includes loading the model (unless warm)
                    res["median_seconds"] = statistics.median(secs[1:]) if len(secs) > 1 else None
                    res["images"] = sum(len(v) for (_, c), v in copied.items() if c == name)
                checkpoint("not run yet")
    except BaseException as e:  # Ctrl-C (or a crash outside a column): keep a sheet of what ran
        if res is not None and res.get("error") is None and "note" not in res and \
                len(res["runs"]) < len(next(c[3] for _, cols in columns for c in cols if c[0] == res["preset"])):
            res["error"] = "interrupted (Ctrl-C)" if isinstance(e, KeyboardInterrupt) else f"{type(e).__name__}: {e}"
        checkpoint("not run (bake-off stopped)")
        raise
    return collect()


STOPPED = " (so far: still running, or stopped)"  # a sheet written before the last column finished


def _out_dir(conf: dict, label: str) -> Path:
    now = datetime.now()
    out = Path(conf["output_dir"]).expanduser() / now.strftime("%Y-%m-%d") / \
        f"{now:%H%M%S}-bakeoff-{imagegen.slug(label, 30)}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _finish(out: Path, run: dict) -> dict:
    (out / "bakeoff.json").write_text(json.dumps(run["bakeoff"], indent=2))
    (out / "run.json").write_text(json.dumps(run, indent=2))
    sheet = imagegen.write_contact_sheet(out, run)
    run["contact_sheet"] = str(sheet)
    run["contact_sheet_windows"] = imagegen.to_windows_path(sheet)
    run["run_dir_windows"] = imagegen.to_windows_path(out)
    return run


def bakeoff(path: str, instructions: list[str], presets: list[str] | tuple[str, ...] = DEFAULT_PRESETS,
            count: int = 2, seed: int | None = None, free: bool = True, label: str | None = None,
            interval: float = 1.0) -> dict:
    """Run every edit instruction through every edit preset with the same seeds; returns the bake-off run dict."""
    presets = list(presets)
    instructions = _clean(instructions, "instruction")
    conf = imagegen.load_config()
    _check_presets(presets, conf["presets"])
    src = imagegen.to_linux_path(path)
    if not src.is_file():
        raise FileNotFoundError(src)
    count = max(1, int(count))
    base_seed = seed if seed is not None else random.randrange(imagegen.MAX_SEED)
    comfy = imagegen.Comfy(conf["comfy_url"])
    comfy.wait_ready()
    _require_idle(comfy)
    out = _out_dir(conf, label or src.stem)

    def call(preset):
        return lambda n, text: imagegen.edit(str(src), text, count, preset, seed=base_seed,
                                             label=f"bakeoff-{preset}-{n}-{imagegen.slug(text, 20)}", styles=[])
    items = list(enumerate(instructions, 1))

    def build(results, images, done=True):
        return {"preset": "bakeoff: " + " vs ".join(presets) + ("" if done else STOPPED),
                "prompt": " | ".join(f"{n}. {i}" for n, i in enumerate(instructions, 1)),
                "base_seed": base_seed, "count": len(images), "run_dir": str(out), "images": images,
                "transparent": False, "source_image": str(src), "mask": None, "denoise": None, "edit": True,
                "bakeoff": {"mode": "edit", "instructions": instructions, "count": count, "complete": done,
                            "presets": results}}
    results, images = _bake(comfy, out, [(p, [(p, _label(conf["presets"][p], p), call(p), items)])
                                         for p in presets], free, interval,
                            save=lambda r, i, done: _finish(out, build(r, i, done)))
    return _finish(out, build(results, images))


T2I_PRESETS = ("draft", "dreamshaper", "klein", "qwen")
T2I_SIZE = (1344, 768)  # the wallpaper preset's SDXL size (~16:9); sizes must be multiples of 16 (klein's latent)
# Added to every ref prompt: the ref is the character sheet, the prompt is the scene. From the daily wallpaper
# work (#3): without it klein copied the ref as-is, white background included.
REF_SUFFIX = (" The character from the reference image appears exactly once, with the same design and colours,"
              " in a new pose that fits the scene. The scene fills the whole frame: no white or empty background.")
NO_REF = "no ref input for this model (no edit_workflow or ref_preset)"


def _ref_preset(pconf: dict, name: str) -> str | None:
    """The preset that draws `name`'s model with a ref image: itself if it can edit (klein, qwen), else its
    `ref_preset` (draft -> sdxl_ref: SDXL + IP-Adapter), else None."""
    p = pconf[name]
    if "edit_workflow" in p:
        return name
    r = p.get("ref_preset")
    if r is None:
        return None
    if "edit_workflow" not in pconf.get(r, {}):
        raise ValueError(f"preset '{name}': ref_preset '{r}' is not an edit preset in config.toml")
    return r


def _label(p: dict, name: str, steps: int | None = None, ref: bool = False) -> str:
    """What the sheet calls a column: the model (preset `model`, else the preset name) and its steps."""
    steps = steps if steps is not None else p.get("steps")
    return " · ".join([p.get("model", name)] + ([f"{steps} steps"] if steps else []) + (["ref"] if ref else []))


def _missing_nodes(comfy: imagegen.Comfy, workflow: str) -> list[str]:
    """Node types a workflow uses that ComfyUI doesn't have (a custom node not installed), [] if unknown."""
    try:
        have = set(json.loads(comfy._get("/object_info")))
        wf = json.loads((imagegen.ROOT / "workflows" / workflow).read_text())
    except Exception:
        return []  # can't tell: let ComfyUI say so
    return sorted({n["class_type"] for n in wf.values()} - have)


def t2i_bakeoff(prompts: list[str] | None = None, presets: list[str] | tuple[str, ...] = T2I_PRESETS,
                ref: str | None = None, ref_prompts: list[str] | None = None, count: int = 2,
                seed: int | None = None, size: tuple[int, int] = T2I_SIZE, free: bool = True,
                label: str | None = None, interval: float = 1.0,
                steps: dict[str, int | list[int]] | None = None) -> dict:
    """Two tests on one sheet, with the same seeds for every model:
    - prompts (fresh scenes, no ref): every preset's text-to-image workflow at `size`;
    - ref_prompts with ref (a character to keep on-model): presets that can take a ref (edit_workflow) draw the
      ref's character into each scene. Their size follows the ref (a square ref: 1024x1024 for all).
    steps: {preset: steps} overrides a preset's own steps in both tests, e.g. {"klein": 16}; a list sweeps them
    ({"klein": [8, 16, 32]}: a column per step count, named klein@8, klein@8+ref, ...)."""
    presets = list(presets)
    prompts = _clean(prompts, "prompt") if prompts else []
    ref_prompts = _clean(ref_prompts, "ref prompt") if ref_prompts else []
    if not prompts and not ref_prompts:
        raise ValueError("bakeoff needs at least one prompt (fresh) or ref prompt (with --ref)")
    if bool(ref) != bool(ref_prompts):
        raise ValueError("--ref and --ref-prompt go together: the ref test needs both")
    conf = imagegen.load_config()
    pconf = conf["presets"]
    src = None
    if ref is not None:
        src = imagegen.to_linux_path(ref)
        if not src.is_file():
            raise FileNotFoundError(src)
    unknown = [p for p in presets if p not in pconf]
    if unknown:
        raise ValueError(f"unknown preset: {', '.join(unknown)}")
    if prompts:  # edit-only presets (kontext) can still be in the ref test
        _check_presets([p for p in presets if not (ref_prompts and "workflow" not in pconf[p])], pconf, t2i=True)
    elif bad := [p for p in presets if not _ref_preset(pconf, p)]:
        raise ValueError(f"no ref input: {', '.join(bad)} (only the ref test is running)")
    if len(set(presets)) != len(presets):
        raise ValueError("a preset is listed twice")
    steps = dict(steps or {})
    if bad := [p for p in steps if p not in presets]:
        raise ValueError(f"--steps names a preset that isn't in the bake-off: {', '.join(bad)}")
    steps = {p: [int(x) for x in (v if isinstance(v, (list, tuple)) else [v])] for p, v in steps.items()}
    if bad := [p for p, v in steps.items() if not v or not all(1 <= x <= 150 for x in v) or len(set(v)) != len(v)]:
        raise ValueError(f"steps must be 1-150, each value once ({', '.join(bad)})")
    for p in presets:
        _ref_preset(pconf, p)  # a broken ref_preset is a config error, not a quiet "no ref input"
    w, h = (int(v) for v in size)
    if w < 256 or h < 256 or w % 16 or h % 16:
        raise ValueError(f"size {w}x{h}: each side needs to be 256 or more and a multiple of 16")
    count = max(1, int(count))
    base_seed = seed if seed is not None else random.randrange(imagegen.MAX_SEED)
    comfy = imagegen.Comfy(conf["comfy_url"])
    comfy.wait_ready()
    _require_idle(comfy)
    out = _out_dir(conf, label or "t2i-" + (prompts or ref_prompts)[0])

    def text_call(preset, st):
        return lambda n, text: imagegen.generate(text, preset, count, base_seed, width=w, height=h, steps=st,
                                                 label=f"bakeoff-{preset}-{n}-{imagegen.slug(text, 20)}",
                                                 styles=[])  # the same prompt for every model

    def ref_call(preset, rp, st):  # rp: the preset that draws preset's model with the ref
        return lambda n, text: imagegen.generate(text + REF_SUFFIX, rp, count, base_seed, source_image=str(src),
                                                 edit=True, steps=st, styles=[],
                                                 label=f"bakeoff-{preset}-ref-{n}-{imagegen.slug(text, 20)}")
    text_items = list(enumerate(prompts, 1))
    ref_items = list(enumerate(ref_prompts, len(prompts) + 1))  # numbered after the fresh prompts
    def runnable(call, workflow):  # a missing custom node becomes a note up front, not a ComfyUI error
        missing = _missing_nodes(comfy, workflow)
        return f"custom node not installed (gen doctor): ComfyUI has no {', '.join(missing)}" \
            if missing else call
    columns = []
    for p in presets:
        cols = []
        sweep = steps.get(p, [None])  # None: the preset's own steps
        names = {st: p if len(sweep) == 1 else f"{p}@{st}" for st in sweep}
        if text_items and "workflow" in pconf[p]:
            for st in sweep:
                cols.append((names[st], _label(pconf[p], p, st), runnable(text_call(p, st), pconf[p]["workflow"]),
                             text_items))
        if ref_items:
            rp = _ref_preset(pconf, p)
            for st in sweep:
                cols.append((names[st] + "+ref", _label(pconf[rp or p], rp or p, st, ref=True),
                             runnable(ref_call(p, rp, st), pconf[rp]["edit_workflow"]) if rp else NO_REF,
                             ref_items))
                if not rp:
                    break  # one "no ref input" row per model is enough
        columns.append((p, cols))
    # two presets on the same model and steps (wallpaper, icon) would share a label: name the preset too
    seen: dict[str, int] = {}
    for _, cols in columns:
        for c in cols:
            seen[c[1]] = seen.get(c[1], 0) + 1
    columns = [(p, [(c[0], f"{c[1]} ({c[0]})" if seen[c[1]] > 1 else c[1], *c[2:]) for c in cols])
               for p, cols in columns]
    labels = [c[1] for _, cols in columns for c in cols if callable(c[2])]
    listed = prompts + [f"with the ref: {t}" for t in ref_prompts]

    def build(results, images, done=True):
        return {"preset": "bakeoff: " + " vs ".join(labels) + ("" if done else STOPPED),
                "prompt": " | ".join(f"{n}. {p}" for n, p in enumerate(listed, 1)),
                "base_seed": base_seed, "count": len(images), "run_dir": str(out), "images": images,
                "transparent": False, "source_image": str(src) if src else None, "mask": None, "denoise": None,
                "edit": False,  # only the ref test is edits; the sheet names the ref from bakeoff.ref
                "bakeoff": {"mode": "t2i", "instructions": listed, "count": count, "size": f"{w}x{h}",
                            "ref": str(src) if src else None, "ref_suffix": REF_SUFFIX if src else None,
                            "steps": steps, "complete": done,
                            "presets": results}}
    results, images = _bake(comfy, out, columns, free, interval,
                            save=lambda r, i, done: _finish(out, build(r, i, done)))
    return _finish(out, build(results, images))


_COPIED = re.compile(r"(\d+)-(.+)-(\d+-seed\d+(?:-\d+)?(?:-transparent)?\.png)")  # _bake's copies


def recover(path: str) -> dict:
    """Rebuild the sheet of a bake-off folder that has no run.json (stopped before this version wrote one
    per column) from the copied images and their sidecars. Times come from the sidecars; memory peaks are
    lost. Returns the run dict."""
    out = imagegen.to_linux_path(path)
    if not out.is_dir():
        out = Path(imagegen.load_config()["output_dir"]).expanduser() / path
    if not out.is_dir():
        raise FileNotFoundError(f"no such folder: {path}")
    pconf = imagegen.load_config()["presets"]
    files = sorted((f for f in out.glob("*.png") if _COPIED.fullmatch(f.name)), key=lambda f: f.stat().st_mtime)
    if not files:
        raise FileNotFoundError(f"no bake-off images in {out}")
    cols: dict[str, dict] = {}  # column name -> result, in the order they ran
    texts: dict[int, str] = {}
    entries: dict[tuple[int, str], list[dict]] = {}
    src = None
    edits = True
    for f in files:
        n, name, _ = _COPIED.fullmatch(f.name).groups()
        n = int(n)
        side = imagegen.find_sidecar(f) or {}
        preset = side.get("preset", name)
        prompt = side.get("prompt", "")
        edits &= bool(side.get("edit"))
        if prompt.endswith(REF_SUFFIX):
            prompt = "with the ref: " + prompt.removesuffix(REF_SUFFIX)
            src = src or side.get("source_image")
        texts.setdefault(n, prompt)
        size, steps = _size_steps_of(side)
        res = cols.setdefault(name, {
            "preset": name, "model": name.split("@")[0].removesuffix("+ref"), "runs": [], "seconds": [],
            "error": None, "size": size, "steps": steps, "peak": {},
            "label": _label(pconf.get(preset, {}), preset, steps, ref=name.endswith("+ref")),
            "note": "rebuilt from the files: no memory numbers"})
        if (secs := (side.get("timing") or {}).get("total_s")) is not None:
            res["seconds"].append(secs)
        entries.setdefault((n, name), []).append({"path": str(f), "seed": side.get("seed"), "seconds": secs,
                                                  "timing": side.get("timing"), "tag": f"{res['label']} · {n}"})
    for name, res in cols.items():
        secs = res["seconds"]
        res["images"] = sum(len(v) for (_, c), v in entries.items() if c == name)
        res["first_seconds"] = secs[0] if secs else None
        res["median_seconds"] = statistics.median(secs[1:]) if len(secs) > 1 else None
    numbers = sorted(texts)
    images = [e for n in numbers for c in cols for e in entries.get((n, c), [])]
    mode = "edit" if edits and not any(c.endswith("+ref") for c in cols) else "t2i"
    run = {"preset": "bakeoff: " + " vs ".join(r["label"] for r in cols.values()) + " (rebuilt)",
           "prompt": " | ".join(f"{n}. {texts[n]}" for n in numbers),
           "base_seed": images[0]["seed"], "count": len(images), "run_dir": str(out), "images": images,
           "transparent": False, "source_image": src, "mask": None, "denoise": None, "edit": mode == "edit",
           "bakeoff": {"mode": mode, "instructions": [texts[n] for n in numbers], "ref": src, "complete": False,
                       "rebuilt": True, "presets": list(cols.values())}}
    return _finish(out, run)


def table(run: dict) -> str:
    """The numbers as a plain-text table (also what gen bakeoff prints)."""
    def f(v, unit=""):
        return "-" if v is None else f"{v:.1f}{unit}"
    rows = [("model", "images", "size", "steps", "first (s)", "median (s)", "VRAM GiB", "sys RAM", "comfy RSS",
             "comfy swap", "sys swap", "error / note")]
    for r in run["bakeoff"]["presets"]:
        pk = r.get("peak", {})
        rows.append((r.get("label") or r["preset"], str(r.get("images", 0)), r.get("size") or "-",
                     str(r["steps"]) if r.get("steps") is not None else "-", f(r.get("first_seconds")),
                     f(r.get("median_seconds")), f(pk.get("vram_gib")), f(pk.get("ram_gib")),
                     f(pk.get("comfy_rss_gib")), f(pk.get("comfy_swap_gib")), f(pk.get("swap_gib")),
                     (r.get("error") or r.get("note") or "")[:100]))
    w = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(row)).rstrip() for row in rows)


def _parse_size(v: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", v)
    if not m:
        raise argparse.ArgumentTypeError(f"size must look like 1344x768, not {v!r}")
    return int(m.group(1)), int(m.group(2))


def _parse_steps(v: str) -> dict[str, list[int]]:
    out = {}
    for part in filter(None, (x.strip() for x in v.split(","))):
        m = re.fullmatch(r"([\w-]+)\s*=\s*(\d+(?:\s*/\s*\d+)*)", part)
        if not m:
            raise argparse.ArgumentTypeError(f"--steps takes PRESET=N or PRESET=A/B/C (comma-separated), not {part!r}")
        out[m.group(1)] = [int(x) for x in m.group(2).split("/")]
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="gen bakeoff", description="The same prompts through several models (same seeds), with time, "
        "VRAM and RAM per model, on one contact sheet. Models are unloaded before each preset, so its first "
        "image includes the load. Edit mode: gen bakeoff IMAGE \"instruction\"... Text-to-image: "
        "gen bakeoff --t2i \"prompt\"... [--ref IMAGE].")
    ap.add_argument("args", nargs="*", metavar="IMAGE|PROMPT",
                    help="edit mode: the source image (Windows or Linux path), then one or more instructions; "
                         "with --t2i: fresh prompts for the text-only test (optional with --ref-prompt)")
    ap.add_argument("--t2i", action="store_true", help="text-to-image bake-off (#51) instead of edits")
    ap.add_argument("--ref", help="with --t2i: the ref image for the ref test (a character to keep on-model)")
    ap.add_argument("--ref-prompt", action="append", dest="ref_prompts", metavar="PROMPT",
                    help="with --t2i --ref: a scene for the ref test (repeatable); only models that take a ref run it")
    ap.add_argument("--size", type=_parse_size,
                    help=f"with --t2i: WxH for every text-only image (default {T2I_SIZE[0]}x{T2I_SIZE[1]})")
    ap.add_argument("--steps", type=_parse_steps, metavar="PRESET=N[,...]",
                    help="with --t2i: sampling steps for some presets instead of their own (klein=16), or a sweep "
                         "with a column per value (klein=8/16/32,qwen=15/25/40)")
    ap.add_argument("-p", "--presets", help=f"comma-separated presets, in order (default {','.join(DEFAULT_PRESETS)}; "
                                            f"with --t2i {','.join(T2I_PRESETS)})")
    ap.add_argument("-n", "--count", type=int, default=2, help="images per prompt and preset (default 2)")
    ap.add_argument("--seed", type=int, help="base seed, the same for every preset (default random)")
    ap.add_argument("--no-free", action="store_true", help="don't unload models between presets")
    ap.add_argument("--label", help="folder name instead of bakeoff-<image or prompt>")
    ap.add_argument("--open", action="store_true", help="open the contact sheet in the Windows browser")
    ap.add_argument("--rebuild", metavar="FOLDER",
                    help="only rebuild the sheet of a stopped bake-off folder from its images (no new runs)")
    a = ap.parse_args(argv)
    if a.rebuild:
        run = recover(a.rebuild)
        print(table(run))
        print(f"\ncontact sheet: {run['contact_sheet_windows'] or run['contact_sheet']}")
        if a.open:
            imagegen.open_in_windows(run["contact_sheet"])
        return 0
    if not a.t2i and (a.ref or a.ref_prompts or a.size or a.steps):
        ap.error("--ref, --ref-prompt, --size and --steps go with --t2i")
    if a.t2i and bool(a.ref) != bool(a.ref_prompts):
        ap.error("--ref and --ref-prompt go together")
    if a.t2i and not a.args and not a.ref_prompts:
        ap.error("--t2i needs a prompt, or --ref with --ref-prompt")
    if not a.t2i and len(a.args) < 2:
        ap.error("edit mode needs an image and at least one instruction (text-to-image: --t2i)")
    default = T2I_PRESETS if a.t2i else DEFAULT_PRESETS
    presets = [p.strip() for p in re.split(r"[,\s]+", a.presets or ",".join(default)) if p.strip()]
    if a.t2i:
        run = t2i_bakeoff(a.args, presets, a.ref, a.ref_prompts, a.count, a.seed, a.size or T2I_SIZE,
                          not a.no_free, a.label, steps=a.steps)
    else:
        run = bakeoff(a.args[0], a.args[1:], presets, a.count, a.seed, not a.no_free, a.label)
    print(table(run))
    print(f"\ncontact sheet: {run['contact_sheet_windows'] or run['contact_sheet']}")
    print(f"numbers: {Path(run['run_dir']) / 'bakeoff.json'}")
    if a.open:
        imagegen.open_in_windows(run["contact_sheet"])
    return 1 if any(r["error"] for r in run["bakeoff"]["presets"]) else 0
