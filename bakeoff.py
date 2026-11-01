"""gen bakeoff: the same edits through several edit models, side by side on one contact sheet (#40).

    gen bakeoff IMAGE "instruction" ["instruction" ...] [-p kontext,klein,qwen] [-n 2] [--seed S]

For each preset in turn: ComfyUI unloads every model first (/free), so each model's first image
includes loading it from disk, then every instruction runs with the same seeds as the other presets.
While a preset runs, a sampler polls once a second for peak VRAM (whole GPU, from ComfyUI's
/system_stats), the comfyui process's RAM and swap (/proc/<MainPID>/status) and system swap
(/proc/meminfo). A preset that fails (models missing, ComfyUI OOM-killed) is recorded and the
bake-off moves on to the next one.

Output: <output_dir>/<date>/<time>-bakeoff-<slug>/ with copies of every image (and its sidecar),
run.json, bakeoff.json (the numbers) and index.html: the normal contact sheet with each image
tagged by preset and instruction, and a table of the numbers. The per-preset runs stay where
gen edit puts them too.
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


def _check_presets(presets: list[str], conf: dict) -> None:
    bad = [p for p in presets if "edit_workflow" not in conf.get(p, {})]
    if bad:
        edit_presets = [n for n, q in conf.items() if "edit_workflow" in q]
        raise ValueError(f"not an edit preset: {', '.join(bad)} (edit presets: {', '.join(edit_presets)})")
    if len(set(presets)) != len(presets):
        raise ValueError("a preset is listed twice")


def bakeoff(path: str, instructions: list[str], presets: list[str] | tuple[str, ...] = DEFAULT_PRESETS,
            count: int = 2, seed: int | None = None, free: bool = True, label: str | None = None,
            interval: float = 1.0) -> dict:
    """Run every instruction through every preset with the same seeds; returns the bake-off run dict."""
    presets = list(presets)
    instructions = [i.strip() for i in instructions]
    if not instructions or not all(instructions):
        raise ValueError("bakeoff needs at least one instruction (and no empty ones)")
    conf = imagegen.load_config()
    _check_presets(presets, conf["presets"])
    src = imagegen.to_linux_path(path)
    if not src.is_file():
        raise FileNotFoundError(src)
    count = max(1, int(count))
    base_seed = seed if seed is not None else random.randrange(imagegen.MAX_SEED)
    comfy = imagegen.Comfy(conf["comfy_url"])
    comfy.wait_ready()

    now = datetime.now()
    out = Path(conf["output_dir"]).expanduser() / now.strftime("%Y-%m-%d") / \
        f"{now:%H%M%S}-bakeoff-{imagegen.slug(label or src.stem, 30)}"
    out.mkdir(parents=True, exist_ok=True)

    results = []  # one per preset
    copied: dict[tuple[int, str], list[dict]] = {}  # (instruction #, preset) -> image entries
    for preset in presets:
        res = {"preset": preset, "runs": [], "seconds": [], "error": None}
        results.append(res)
        imagegen.log(f"== {preset}")
        try:
            if free:
                _unload(comfy)
        except Exception as e:
            imagegen.log(f"   could not unload models first ({e}); load times may be off")
        sampler = Sampler(comfy, interval).start()
        t0 = time.time()
        try:
            for n, instr in enumerate(instructions, 1):
                run = imagegen.edit(str(src), instr, count, preset, seed=base_seed,
                                    label=f"bakeoff-{preset}-{n}-{imagegen.slug(instr, 20)}")
                res["runs"].append(run["run_dir"])
                entries = copied.setdefault((n, preset), [])
                for im in run["images"]:
                    if im.get("seconds") is not None:  # one per job (a job may save several images)
                        res["seconds"].append(im["seconds"])
                    p = Path(im["path"])
                    dest = out / f"{n:02d}-{preset}-{p.name}"
                    shutil.copy2(p, dest)
                    if (side := p.with_suffix(".json")).is_file():
                        shutil.copy2(side, dest.with_suffix(".json"))
                    entries.append({"path": str(dest), "seed": im["seed"], "seconds": im.get("seconds"),
                                    "tag": f"{preset} · {n}"})
                if not (out / imagegen.SHEET_SOURCE).is_file() and \
                        (s := Path(run["run_dir"]) / imagegen.SHEET_SOURCE).is_file():
                    shutil.copy2(s, out / imagegen.SHEET_SOURCE)
        except Exception as e:  # missing models, rejected graph, OOM: note it and try the next model
            res["error"] = f"{type(e).__name__}: {e}"
            imagegen.log(f"   {preset} failed: {res['error']}")
        res["total_seconds"] = round(time.time() - t0, 1)
        res["peak"] = sampler.stop()
        secs = res["seconds"]
        res["first_seconds"] = secs[0] if secs else None  # includes loading the model
        res["median_seconds"] = statistics.median(secs[1:]) if len(secs) > 1 else None
        res["images"] = sum(len(v) for (_, p), v in copied.items() if p == preset)

    # instruction-major order: each instruction's images from every preset sit together
    images = [e for n in range(1, len(instructions) + 1) for p in presets for e in copied.get((n, p), [])]
    run = {"preset": "bakeoff: " + " vs ".join(presets),
           "prompt": " | ".join(f"{n}. {i}" for n, i in enumerate(instructions, 1)),
           "base_seed": base_seed, "count": len(images), "run_dir": str(out), "images": images,
           "transparent": False, "source_image": str(src), "mask": None, "denoise": None, "edit": True,
           "bakeoff": {"instructions": instructions, "count": count, "presets": results}}
    (out / "bakeoff.json").write_text(json.dumps(run["bakeoff"], indent=2))
    (out / "run.json").write_text(json.dumps(run, indent=2))
    sheet = imagegen.write_contact_sheet(out, run)
    run["contact_sheet"] = str(sheet)
    run["contact_sheet_windows"] = imagegen.to_windows_path(sheet)
    run["run_dir_windows"] = imagegen.to_windows_path(out)
    return run


def table(run: dict) -> str:
    """The numbers as a plain-text table (also what gen bakeoff prints)."""
    def f(v, unit=""):
        return "-" if v is None else f"{v:.1f}{unit}"
    rows = [("preset", "images", "first (s)", "median (s)", "VRAM GiB", "sys RAM", "comfy RSS", "comfy swap",
             "sys swap", "error")]
    for r in run["bakeoff"]["presets"]:
        pk = r.get("peak", {})
        rows.append((r["preset"], str(r.get("images", 0)), f(r.get("first_seconds")), f(r.get("median_seconds")),
                     f(pk.get("vram_gib")), f(pk.get("ram_gib")), f(pk.get("comfy_rss_gib")), f(pk.get("comfy_swap_gib")),
                     f(pk.get("swap_gib")), (r.get("error") or "")[:80]))
    w = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(row)).rstrip() for row in rows)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="gen bakeoff", description="The same edit instructions through several edit models "
        "(same seeds), with time, VRAM and RAM per model, on one contact sheet. Models are unloaded "
        "before each preset, so its first image includes the load.")
    ap.add_argument("path", help="the source image (Windows or Linux path)")
    ap.add_argument("instructions", nargs="+", help="one or more edit instructions")
    ap.add_argument("-p", "--presets", default=",".join(DEFAULT_PRESETS),
                    help=f"comma-separated edit presets, in order (default {','.join(DEFAULT_PRESETS)})")
    ap.add_argument("-n", "--count", type=int, default=2, help="images per instruction and preset (default 2)")
    ap.add_argument("--seed", type=int, help="base seed, the same for every preset (default random)")
    ap.add_argument("--no-free", action="store_true", help="don't unload models between presets")
    ap.add_argument("--label", help="folder name instead of bakeoff-<image>")
    ap.add_argument("--open", action="store_true", help="open the contact sheet in the Windows browser")
    a = ap.parse_args(argv)
    presets = [p.strip() for p in re.split(r"[,\s]+", a.presets) if p.strip()]
    run = bakeoff(a.path, a.instructions, presets, a.count, a.seed, not a.no_free, a.label)
    print(table(run))
    print(f"\ncontact sheet: {run['contact_sheet_windows'] or run['contact_sheet']}")
    print(f"numbers: {Path(run['run_dir']) / 'bakeoff.json'}")
    if a.open:
        imagegen.open_in_windows(run["contact_sheet"])
    return 1 if any(r["error"] for r in run["bakeoff"]["presets"]) else 0
