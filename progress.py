"""Live progress for gen runs (#61): what the preset runs, which stage each image is in, ETA, memory.

    wallpaper: sd_xl_base_1.0 · 1 pass, 30 steps · 1344x768 -> 3840x2160 (RealESRGAN_x4plus) · 4 images
    image 2/4  ✓ load  ✓ encode  ▶ sample 18/30  ○ decode  ○ upscale  ○ save
      ████████████░░░░░░░░ 58%  1:52 elapsed · ~1:21 left
      VRAM ▓▓▓▓▓▓▓░░░ 11.2/16.0 GiB · RAM ▓▓▓▓▓░░░░░ 12.0/24.0 GiB · swap 0.4 GiB

The header line (`describe`) is always printed. The live block is drawn only on a terminal (or with
IMAGEGEN_PROGRESS=1; =0 turns it off), so daily.log and the MCP server's stderr stay as they were.
It is fed by imagegen.Events (ComfyUI's /ws: execution_start, execution_cached, executing, progress)
and redrawn in place twice a second; imagegen.log() prints above it.

ETA: per-stage medians of the same preset's recent runs (run.json timing, #45), replaced by this run's
own finished images once there are some; the sampler's live step rate for the stage that is running.
Memory: ComfyUI's /system_stats (VRAM of the GPU, RAM of the machine, which in WSL is the .wslconfig
cap) and swap from /proc/meminfo, polled every MEM_EVERY seconds; bars turn yellow at 75%, red at 90%.

Stdlib only. Nothing here imports imagegen (it may be running as __main__); generate() passes in what
it needs (stage_of, the stage order).
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

GIB = 1024 ** 3
PROC = Path("/proc")      # tests point this at a fake /proc
MEM_EVERY = 2.0           # seconds between memory polls
TICK = 0.5                # seconds between redraws
HISTORY_RUNS = 5          # recent runs of the same preset the ETA learns from
HISTORY_DAYS = 60         # date folders it looks through at most (output_dir is on the Windows drive: slow)
BAR = 20                  # cells in the progress bar
MEM_BAR = 10              # cells in each memory bar
WARN, DANGER = 0.75, 0.90

# model-file inputs, in the order the header lists them
MODEL_INPUTS = ("ckpt_name", "unet_name", "clip_name", "clip_name1", "clip_name2", "vae_name")


def enabled(stream=None) -> bool:
    v = os.environ.get("IMAGEGEN_PROGRESS", "").strip()
    if v in ("0", "1"):
        return v == "1"
    stream = stream or sys.stderr
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


def _stem(name) -> str:
    s = str(name).rsplit("/", 1)[-1]
    for ext in (".safetensors", ".ckpt", ".pth", ".pt", ".gguf", ".bin"):
        s = s.removesuffix(ext)
    return s


def _steps(workflow: dict, node: dict) -> int | None:
    """A sampler's steps: its own input, or the scheduler node feeding its sigmas (SamplerCustomAdvanced)."""
    ins = node.get("inputs", {})
    if isinstance(ins.get("steps"), int):
        return ins["steps"]
    src = ins.get("sigmas")
    if isinstance(src, list) and str(src[0]) in workflow:
        st = workflow[str(src[0])].get("inputs", {}).get("steps")
        return st if isinstance(st, int) else None
    return None


def describe(workflow: dict, preset: str, count: int, stage_of, mode: str = "") -> str:
    """One line: preset, models, passes and steps, size (-> upscale), image count."""
    models: list[str] = []
    upscaler = None
    size = target = None
    steps: list[int] = []
    for node in workflow.values():
        cls, ins = node.get("class_type", ""), node.get("inputs", {})
        for k in MODEL_INPUTS:
            if isinstance(ins.get(k), str) and _stem(ins[k]) not in models:
                models.append(_stem(ins[k]))
        if isinstance(ins.get("model_name"), str) and "Upscale" in cls:
            upscaler = _stem(ins["model_name"])
        if "Latent" in cls and "Empty" in cls and isinstance(ins.get("width"), int):
            size = (ins["width"], ins["height"])
        if cls == "ImageScale" and isinstance(ins.get("width"), int):
            target = (ins["width"], ins["height"])
        if stage_of(cls) == "sample":
            if (n := _steps(workflow, node)) is not None:
                steps.append(n)
    passes = sum(1 for n in workflow.values() if stage_of(n.get("class_type", "")) == "sample")
    parts = [" + ".join(models) or "?"]
    if passes:
        p = f"{passes} pass{'es' if passes != 1 else ''}"
        if steps:
            p += ", " + (f"{steps[0]} steps" if len(set(steps)) == 1 else "+".join(map(str, steps)) + " steps")
        parts.append(p)
    if size or target or upscaler:
        s = f"{size[0]}x{size[1]}" if size else "source size"
        if target or upscaler:
            s += " -> " + (f"{target[0]}x{target[1]}" if target else "upscaled")
            if upscaler:
                s += f" ({upscaler})"
        parts.append(s)
    parts.append(f"{count} image{'s' if count != 1 else ''}")
    return f"{preset}{' ' + mode if mode else ''}: " + " · ".join(parts)


# ---------------------------------------------------------------- ETA from past runs

def history_estimate(output_dir: Path, preset: str, mode: str, stage_keys: tuple[str, ...]) -> dict[str, float]:
    """Median seconds per stage per job over the preset's last HISTORY_RUNS runs in the same mode.

    One entry per ComfyUI job (a job that saves several images repeats its times on each). A stage a job
    has no time for counts as 0 (its loaders were cached), so a cold first image doesn't make every
    image look like it loads the model."""
    jobs: list[dict] = []
    found = 0
    try:
        days = sorted((d for d in Path(output_dir).iterdir() if d.is_dir() and d.name[:4].isdigit()),
                      key=lambda d: d.name, reverse=True)[:HISTORY_DAYS]
    except OSError:
        return {}
    for day in days:
        try:
            runs = sorted((d for d in day.glob(f"*-{preset}-*") if d.is_dir()), key=lambda d: d.name, reverse=True)
        except OSError:
            continue
        for d in runs:
            try:
                run = json.loads((d / "run.json").read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(run, dict) or run.get("preset") != preset or run_mode(run) != mode:
                continue
            ts = [im["timing"] for im in run.get("images") or [] if isinstance(im, dict)
                  and isinstance(im.get("timing"), dict) and im.get("seconds", 0) is not None]
            ts = [t for t in ts if any(isinstance(t.get(k), (int, float)) for k in stage_keys)]
            if ts:
                jobs += ts
                found += 1
            if found >= HISTORY_RUNS:
                break
        if found >= HISTORY_RUNS:
            break
    out = {}
    for k in stage_keys:
        vals = [t[k] if isinstance(t.get(k), (int, float)) else 0.0 for t in jobs]
        if any(vals):
            out[k.removesuffix("_s")] = statistics.median(vals)
    return out


def run_mode(run: dict) -> str:
    if run.get("edit"):
        return "edit"
    if run.get("mask"):
        return "inpaint"
    if run.get("source_image"):
        return "img2img"
    return ""


def fmt_secs(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------- memory

def _meminfo_swap() -> float | None:
    vals = {}
    try:
        for line in (PROC / "meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("SwapTotal", "SwapFree") and v.split():
                vals[k] = int(v.split()[0])
    except (OSError, ValueError):
        return None
    if len(vals) < 2:
        return None
    return (vals["SwapTotal"] - vals["SwapFree"]) / 1024 ** 2


def memory_snapshot(comfy_url: str) -> dict[str, float]:
    """GiB used/total: vram, vram_total, ram, ram_total (from ComfyUI) and swap (from /proc). Best effort."""
    out: dict[str, float] = {}
    try:
        with urllib.request.urlopen(comfy_url.rstrip("/") + "/system_stats", timeout=2) as r:
            st = json.loads(r.read())
    except Exception:
        st = {}  # ComfyUI busy or restarting: the view keeps the last numbers
    if not isinstance(st, dict):
        st = {}

    def pair(d, total: str, free: str) -> tuple[float, float] | None:
        if isinstance(d, dict) and isinstance(d.get(total), (int, float)) and d[total] > 0 \
                and isinstance(d.get(free), (int, float)):
            return (d[total] - d[free]) / GIB, d[total] / GIB
        return None

    devs = st.get("devices") if isinstance(st.get("devices"), list) else []
    for key, src, total, free in (("vram", next((d for d in devs if pair(d, "vram_total", "vram_free")), None),
                                   "vram_total", "vram_free"),
                                  ("ram", st.get("system"), "ram_total", "ram_free")):
        if (p := pair(src, total, free)) is not None:
            out[key], out[key + "_total"] = p
    if (swap := _meminfo_swap()) is not None:
        out["swap"] = swap
    return out


# ---------------------------------------------------------------- the live view

GLYPH = {"done": "✓", "run": "▶", "part": "◐", "todo": "○", "cached": "✓"}


class _Job:
    def __init__(self):
        self.started: float | None = None
        self.ended: float | None = None
        self.cached: set[str] = set()
        self.done: set[str] = set()
        self.node: str | None = None
        self.node_t = 0.0
        self.step: tuple[int, int] | None = None   # (value, max) of the running sampler
        self.step_t: tuple[float, int] | None = None  # (time, value) of the sampler's first step report
        self.step_last = 0.0                           # time of its latest step report
        self.stage_s: dict[str, float] = {}


class Progress:
    """The live block. on_event() is called from the websocket thread, log lines via write_above()."""

    def __init__(self, workflow: dict, count: int, stage_of, stage_order: tuple[str, ...],
                 comfy_url: str, estimate: dict[str, float] | None = None, stream=None, color: bool | None = None):
        self.stream = stream or sys.stderr
        self.count = count
        self.stage_of = stage_of
        self.comfy_url = comfy_url
        self.estimate = dict(estimate or {})
        self.color = (os.environ.get("NO_COLOR") is None) if color is None else color
        self.node_stage = {nid: stage_of(n.get("class_type", "")) for nid, n in workflow.items()}
        present = set(self.node_stage.values()) - {"other"}
        self.stages = [s for s in stage_order if s in present]
        self.jobs: dict[str, _Job] = {}
        self.order: list[str] = []
        self.t0 = time.time()
        self.mem: dict[str, float] = {}
        self.peak: dict[str, float] = {}
        self._lines = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mem_thread: threading.Thread | None = None

    # ------------------------------------------------ events (websocket thread)

    def on_event(self, pid: str, kind: str, data: dict, t: float | None = None) -> None:
        t = time.time() if t is None else t
        with self._lock:
            job = self.jobs.get(pid)
            if job is None:
                job = self.jobs[pid] = _Job()
                self.order.append(pid)
            if kind == "execution_start":
                job.started = t if job.started is None else job.started
            elif kind == "execution_cached":
                job.cached |= {str(n) for n in data.get("nodes") or []}
            elif kind == "executing":
                job.started = t if job.started is None else job.started
                self._leave_node(job, t)
                if data.get("node") is None:
                    job.ended = t if job.ended is None else job.ended
                else:  # display_node: the workflow's id for a node ComfyUI expanded (as job_timing does)
                    job.node, job.node_t = str(data.get("display_node") or data["node"]), t
            elif kind == "execution_success":
                self._leave_node(job, t)
                job.ended = t if job.ended is None else job.ended
            elif kind == "progress":
                v, m = data.get("value"), data.get("max")
                if isinstance(v, int) and isinstance(m, int) and m > 0:
                    if job.step_t is None or v < job.step_t[1]:
                        job.step_t = (t, v)
                    job.step, job.step_last = (v, m), t

    def _leave_node(self, job: _Job, t: float) -> None:
        if job.node is not None:
            st = self.node_stage.get(job.node, "other")
            job.stage_s[st] = job.stage_s.get(st, 0.0) + (t - job.node_t)
            job.done.add(job.node)
            job.node, job.step, job.step_t = None, None, None

    # ------------------------------------------------ state -> text

    def _finished(self) -> int:
        return sum(1 for p in self.order if self.jobs[p].ended is not None)

    def _active(self) -> tuple[int, _Job | None]:
        """(index of the image being worked on, from 1; its job). The first not finished, in start order."""
        finished = self._finished()
        running = [self.jobs[p] for p in self.order
                   if self.jobs[p].started is not None and self.jobs[p].ended is None]
        return min(finished + 1, self.count), (running[0] if running else None)

    def _stage_state(self, job: _Job, stage: str) -> tuple[str, int, int]:
        nodes = [n for n, s in self.node_stage.items() if s == stage]
        finished = sum(1 for n in nodes if n in job.done or n in job.cached)
        if job.node is not None and self.node_stage.get(job.node) == stage:
            return "run", finished, len(nodes)
        if finished == len(nodes):
            return "done", finished, len(nodes)
        return ("part" if finished else "todo"), finished, len(nodes)

    def _per_image(self) -> dict[str, float]:
        """Seconds per stage per image: this run's finished images if any, else past runs."""
        done = [self.jobs[p].stage_s for p in self.order if self.jobs[p].ended is not None and self.jobs[p].stage_s]
        if done:
            return {s: statistics.median(d.get(s, 0.0) for d in done) for s in self.stages}
        return {s: v for s, v in self.estimate.items() if s in self.stages}

    def eta(self, now: float | None = None) -> float | None:
        """Seconds left for the whole run, or None when there is nothing to go on yet."""
        now = time.time() if now is None else now
        with self._lock:
            per = self._per_image()
            idx, job = self._active()
            finished = self._finished()
            left = 0.0
            known = bool(per)
            if job is not None:
                for st in self.stages:
                    state, n_done, n = self._stage_state(job, st)
                    spent = job.stage_s.get(st, 0.0)
                    if state == "run":
                        spent += now - job.node_t
                        if st == "sample" and job.step and job.step_t and job.step[0] > job.step_t[1]:
                            v, m = job.step
                            rate = (job.step_last - job.step_t[0]) / (v - job.step_t[1])
                            left += max(0.0, (m - v) * rate - (now - job.step_last))
                            known = True
                            continue
                    if state != "done" and st in per:
                        left += max(0.0, per[st] - spent)
            if not per:
                return left if known and job is not None and self.count - finished <= 1 else None
            later = self.count - finished - (1 if job is not None else 0)
            return left + max(0, later) * sum(per.values())

    def _paint(self, text: str, kind: str) -> str:
        if not self.color:
            return text
        code = {"done": "32", "run": "36;1", "part": "33", "todo": "2", "warn": "33", "danger": "31;1",
                "ok": "32", "dim": "2", "bold": "1"}.get(kind)
        return f"\x1b[{code}m{text}\x1b[0m" if code else text

    def _bar(self, frac: float, cells: int, fill="█", empty="░", kind="run") -> str:
        frac = min(1.0, max(0.0, frac))
        n = int(round(frac * cells))
        return self._paint(fill * n, kind) + self._paint(empty * (cells - n), "dim")

    def lines(self, now: float | None = None) -> list[tuple[str, int]]:
        """The block as (text, visible length) pairs."""
        now = time.time() if now is None else now
        with self._lock:
            idx, job = self._active()
            finished = self._finished()
            segs: list[tuple[str, str]] = [(f"image {idx}/{self.count} ", "bold")]
            if job is None:
                segs.append(("ComfyUI done, saving…" if finished >= self.count else "waiting for ComfyUI…", "dim"))
            else:
                for st in self.stages:
                    state, n_done, n = self._stage_state(job, st)
                    label = st
                    if state == "run" and st == "sample" and job.step:
                        label += f" {job.step[0]}/{job.step[1]}"
                    elif state == "part" and n > 1:
                        label += f" {n_done}/{n}"
                    segs.append((f" {GLYPH[state]} {label} ", state))
            first = segs
            eta = self.eta(now)
            elapsed = now - self.t0
            if eta is not None:
                frac = elapsed / (elapsed + eta) if elapsed + eta > 0 else 0.0
            else:
                step = job.step[0] / job.step[1] if job is not None and job.step else 0.0
                frac = (finished + step) / self.count
            second = [("  ", ""), (self._bar(frac, BAR), "raw"), (f" {int(frac * 100):3d}%  ", ""),
                      (f"{fmt_secs(elapsed)} elapsed", "dim")]
            if eta is not None:
                second.append((f" · ~{fmt_secs(eta)} left", "bold"))
            out = [first, second]
            if self.mem:
                third: list[tuple[str, str]] = [("  ", "")]
                for key, label in (("vram", "VRAM"), ("ram", "RAM")):
                    if key in self.mem and self.mem.get(key + "_total"):
                        used, total = self.mem[key], self.mem[key + "_total"]
                        f = used / total
                        kind = "danger" if f >= DANGER else "warn" if f >= WARN else "ok"
                        if len(third) > 1:
                            third.append((" · ", "dim"))
                        third += [(f"{label} ", ""), (self._bar(f, MEM_BAR, "▓", "░", kind), "raw"),
                                  (f" {used:.1f}/{total:.1f} GiB", kind if kind != "ok" else "")]
                if self.mem.get("swap", 0) >= 0.05:
                    third.append((f" · swap {self.mem['swap']:.1f} GiB", "warn"))
                if len(third) > 1:
                    out.append(third)
        return [self._render(segs) for segs in out]

    def _render(self, segs: list[tuple[str, str]]) -> tuple[str, int]:
        width = max(20, shutil.get_terminal_size((100, 24)).columns - 1)
        text, vis = "", 0
        for s, kind in segs:
            n = len(s) if kind != "raw" else len(_strip(s))
            if vis + n > width:  # cut, so a line never wraps (the redraw counts lines)
                room = width - vis
                if room > 1 and kind != "raw":
                    text += self._paint(s[:room - 1] + "…", kind)
                    vis += room
                break
            text += s if kind == "raw" else self._paint(s, kind)
            vis += n
        return text, vis

    # ------------------------------------------------ drawing

    def _clear(self) -> None:
        if self._lines:
            self.stream.write(f"\x1b[{self._lines}F\x1b[J")
            self._lines = 0

    def draw(self) -> None:
        with self._lock:
            block = self.lines()
            self._clear()
            self.stream.write("".join(t + "\n" for t, _ in block))
            self._lines = len(block)
            self.stream.flush()

    def write_above(self, msg: str) -> None:
        """A log line, printed above the live block (which is redrawn under it)."""
        with self._lock:
            self._clear()
            self.stream.write(msg + "\n")
            if self._thread is not None and not self._stop.is_set():
                self.draw()
            else:
                self.stream.flush()

    def _poll_mem(self) -> None:
        while not self._stop.is_set():
            snap = memory_snapshot(self.comfy_url)
            with self._lock:
                self.mem.update(snap)
                for k, v in snap.items():
                    self.peak[k] = max(self.peak.get(k, 0.0), v)
            self._stop.wait(MEM_EVERY)

    def _loop(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.draw()
            except Exception:
                pass  # a closed terminal must not break the run

    def start(self) -> "Progress":
        self.stream.write("\x1b[?25l")  # hide the cursor while redrawing
        self.draw()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._mem_thread = threading.Thread(target=self._poll_mem, daemon=True)
        self._thread.start()
        self._mem_thread.start()
        return self

    def stop(self) -> str | None:
        """Erase the block, cursor back; returns a one-line memory peak summary (None if nothing was sampled)."""
        self._stop.set()
        for th in (self._thread, self._mem_thread):
            if th is not None:
                th.join(timeout=3)
        with self._lock:
            try:
                self._clear()  # the [i/n] lines and the result say the rest
            finally:
                self.stream.write("\x1b[?25h")
                self.stream.flush()
                self._lines = 0
            return peak_summary(self.peak)


def peak_summary(peak: dict[str, float]) -> str | None:
    parts = []
    for key, label in (("vram", "VRAM"), ("ram", "RAM")):
        if key in peak and peak.get(key + "_total"):
            parts.append(f"{label} {peak[key]:.1f}/{peak[key + '_total']:.1f} GiB "
                         f"({int(round(100 * peak[key] / peak[key + '_total']))}%)")
    if peak.get("swap", 0) >= 0.05:
        parts.append(f"swap {peak['swap']:.1f} GiB")
    return ("peak memory: " + ", ".join(parts)) if parts else None


def _strip(s: str) -> str:
    """Text without ANSI colour codes (its visible part)."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\x1b":
            j = s.find("m", i)
            i = j + 1 if j != -1 else len(s)
            continue
        out.append(s[i])
        i += 1
    return "".join(out)
