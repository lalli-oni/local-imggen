#!/usr/bin/env python3
"""imagegen: run ComfyUI presets from the command line (and from mcp_server.py).

Usage:
  gen "a cozy cabin at dusk"                    # draft preset, 1 image
  gen "misty fjord" -p wallpaper -n 4           # 4 wallpapers (seeds base..base+3)
  gen "rocket" -p icon -n 6 --open              # icons, open contact sheet in browser
  gen "x" --seed 123 --size 832x1216 --steps 40 --neg "people"
  gen vary PATH -n 4 --strength 0.4             # img2img variations of an image
  gen vary PATH --mask MASK.png --prompt "..."  # inpaint: repaint only the white part of MASK
  gen edit PATH "make it wave, side view"      # instruction edit (FLUX.2 klein; -p kontext|qwen; opt-in models)
  gen bakeoff PATH "instr" ["instr"...]         # same edits through kontext, klein, qwen; time/VRAM/RAM
  gen bakeoff --t2i "prompt"... [--ref IMG --ref-prompt "scene"]  # compare text-to-image models (#51)
  gen cutout PATH [--clean]                     # remove the background -> PATH-transparent.png
                                                #   --clean: also drop stray shapes, centre on a square
  gen crt PATH... [--lines 360 --scanline 0.22] # CRT/VHS look on finished images -> PATH-crt.png
  gen styles                                    # list the [styles] looks (gen "..." --style NAME|none)
  gen presets                                   # list presets
  gen recent [N]                                # last N runs
  gen set-wallpaper PATH                        # set Windows desktop wallpaper
  gen daily [--show] [--no-set]                 # today's wallpaper from [daily] in config.toml, then set it
  gen doctor                                    # health check (service, GPU, torch, models, ...)
  gen sheet [RUN_DIR|DATE]                      # rebuild contact sheet(s); no arg = latest run
  gen project new|list|status|set|gen|promote|keep|import|doc ...  # creative projects (see projects.py)
  gen board serve [SLUG] [--open]               # the clickable project board on http://127.0.0.1:8190
  gen char new|list|status|set ...              # shared characters projects cast (see characters.py)
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import http.client
import io
import json
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_SEED = 2**32


def cast_seed(seed: int) -> int:
    """The seed of a cast preset's second sampler (#52): with the scene's own seed, and a latent of the same size
    (cast_resolution 1152^2 = 1536x864), the edit started from the scene's noise and gave the scene back unchanged."""
    return seed ^ 0x9E3779B9


_LIVE = None  # the live progress block (progress.Progress) while a gen run draws one; log() prints above it


def log(msg: str) -> None:
    # stderr only: stdout is the MCP protocol channel when used by mcp_server.py
    if _LIVE is not None:
        _LIVE.write_above(msg)
    else:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    if v := os.environ.get("IMAGEGEN_OUTPUT_DIR"):
        cfg["output_dir"] = v
    if v := os.environ.get("IMAGEGEN_COMFY_URL"):
        cfg["comfy_url"] = v
    return cfg


# Preset keys that are not workflow values (generate() drops them before filling the workflow).
NOT_PARAMS = {"workflow", "img2img_workflow", "inpaint_workflow", "edit_workflow", "description",
              "suffix", "negative_extra", "post", "download", "model", "ref_preset", "ref_canvas", "styles"}

# Default img2img/inpaint graphs (SDXL). Presets that load a diffusion model (`unet`: Flux, Qwen) get none.
DEFAULT_MODE_WORKFLOWS = {"img2img_workflow": "sdxl_img2img.json", "inpaint_workflow": "sdxl_inpaint.json"}


def mode_workflow(p: dict, key: str) -> str | None:
    """The workflow file a preset uses for a mode key (workflow, img2img_workflow, ...), or None."""
    if key in p:
        return p[key]
    if "workflow" in p and "unet" not in p:
        return DEFAULT_MODE_WORKFLOWS.get(key)
    return None


def list_presets() -> dict[str, dict]:
    return load_config()["presets"]


# ---------------------------------------------------------------- styles (#10)
# [styles.NAME] in config.toml: a look (suffix, optional negative_extra) that any preset or prompt can use.
# A preset's `styles = [...]` are its defaults; generate(styles=[...]) / gen --style NAME replace them.

def list_styles(conf: dict | None = None) -> dict[str, dict]:
    return (conf or load_config()).get("styles", {})


def split_styles(value) -> list[str]:
    """"a, b" or ["a", "b,c"] -> ["a", "b", "c"] (gen --style, [daily] style)."""
    values = [value] if isinstance(value, str) else list(value or [])
    return [n.strip() for v in values for n in str(v).split(",") if n.strip()]


def check_styles(names, conf: dict | None = None) -> list[str]:
    """The style names as a list, or ValueError naming the unknown ones and the known ones."""
    if isinstance(names, str):
        names = [names]
    names = [n.strip() for n in names if n and n.strip()]
    known = list_styles(conf)
    bad = [n for n in names if n not in known]
    if bad:
        raise ValueError(f"unknown style {', '.join(bad)} (styles: {', '.join(known) or 'none in config.toml'})")
    return names


def style_post(names, conf: dict | None = None, before: list[str] = ()) -> list[str]:
    """before + the styles' `post` steps (e.g. crt), each once, in order; ValueError on an unknown step."""
    conf = conf or load_config()
    out = list(before)
    for n in check_styles(names, conf):
        out += [s for s in list_styles(conf)[n].get("post", []) if s not in out]
    bad = [s for s in out if s not in POST]
    if bad:
        raise ValueError(f"unknown post step {', '.join(bad)} (known: {', '.join(POST)})")
    return sorted(out, key=list(POST).index)  # the order they must run in: remove_bg before trim, crt last


def style_text(names, conf: dict | None = None, key: str = "suffix") -> str:
    """The styles' text (suffix, or negative_extra) joined with ", ", without a leading comma."""
    conf = conf or load_config()
    known = list_styles(conf)
    parts = (str(known[n].get(key, "")).strip().strip(",").strip() for n in check_styles(names, conf))
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------- workflow templating

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def fill(node, params: dict):
    """Replace string values that are exactly "{{key}}" with params[key] (keeps type)."""
    if isinstance(node, dict):
        return {k: fill(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [fill(v, params) for v in node]
    if isinstance(node, str) and (m := _PLACEHOLDER.fullmatch(node)):
        key = m.group(1)
        if key not in params:
            raise KeyError(f"workflow placeholder '{{{{{key}}}}}' has no value (add it to the preset)")
        return params[key]
    return node


# ---------------------------------------------------------------- ComfyUI client

JOB_TIMEOUT = 3600  # s per job; quality first (#51): a swapping Qwen at 40 steps can take many minutes
POLL_TIMEOUT = 60  # s for one /history answer; a timeout means "slow", not "down"
DOWN_LIMIT = 90  # s of refused/dropped connections before a job counts as lost (ComfyUI crashed)
HANG_AFTER = 1200  # s without a single websocket frame while ComfyUI says our job is running: its worker is
# dead (#51: an HSA alloc failure left the server answering and queueing, but nothing ran for hours)
SLOW_TIMEOUT = 300  # s for requests a busy ComfyUI may sit on: queueing, /free, upload, /view


def _is_timeout(e: BaseException) -> bool:
    """A read/connect timeout (ComfyUI busy) rather than a refused or reset connection (ComfyUI gone)."""
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


class Comfy:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _get(self, path: str, timeout: float = 30) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return r.read()

    def wait_ready(self, timeout: float = 120) -> None:
        """ComfyUI may still be booting (e.g. WSL just started). Poll until it answers."""
        t0 = time.time()
        while True:
            try:
                self._get("/system_stats", timeout=5)
                return
            except Exception:
                if time.time() - t0 > timeout:
                    raise RuntimeError(f"ComfyUI not reachable at {self.url} "
                                       "(is the service running? `systemctl status comfyui`)")
                time.sleep(2)

    def queue(self, workflow: dict, front: bool = False) -> str:
        """Queue a job; front=True puts it ahead of the jobs already waiting."""
        body = json.dumps({"prompt": workflow, "client_id": self.client_id, **({"front": True} if front else {})}).encode()
        req = urllib.request.Request(self.url + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=SLOW_TIMEOUT) as r:
                return json.loads(r.read())["prompt_id"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI rejected the workflow: {e.read().decode(errors='replace')[:2000]}")

    def free(self) -> None:
        """Unload all models and free their memory (ComfyUI's /free), so the next job loads from scratch."""
        req = urllib.request.Request(self.url + "/free", data=json.dumps(
            {"unload_models": True, "free_memory": True}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=SLOW_TIMEOUT) as r:
            r.read()

    def system_stats(self) -> dict:
        return json.loads(self._get("/system_stats", timeout=5))

    CRASH_HINT = ("ComfyUI probably crashed (often out of RAM/VRAM): "
                  "journalctl -u comfyui -n 50; sudo dmesg | grep -i oom")

    def _running(self, prompt_id: str) -> bool:
        """Is this job the one ComfyUI is running now? False when unsure."""
        try:
            q = json.loads(self._get("/queue", timeout=POLL_TIMEOUT))
        except Exception:
            return False
        return any(len(item) > 1 and item[1] == prompt_id for item in q.get("queue_running", []))

    def _queued(self, prompt_id: str) -> bool:
        q = json.loads(self._get("/queue"))
        return any(item[1] == prompt_id for k in ("queue_running", "queue_pending") for item in q.get(k, []))

    def busy(self) -> tuple[int, int]:
        """(running, waiting) jobs in ComfyUI's queue, anyone's."""
        q = json.loads(self._get("/queue", timeout=POLL_TIMEOUT))
        return len(q.get("queue_running", [])), len(q.get("queue_pending", []))

    def cancel(self, prompt_ids: list[str]) -> None:
        """Take our jobs out of ComfyUI (Ctrl-C, timeout): delete the waiting ones, interrupt the running one.
        Without this they keep ComfyUI busy for whoever comes next (#51: a stopped Qwen sweep held it for an
        hour). Best effort: never raises."""
        if not prompt_ids:
            return
        def post(path, body):
            req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
        try:
            post("/queue", {"delete": list(prompt_ids)})
            q = json.loads(self._get("/queue", timeout=15))
            running = [item[1] for item in q.get("queue_running", []) if len(item) > 1]
            for pid in (p for p in prompt_ids if p in running):
                post("/interrupt", {"prompt_id": pid})  # older ComfyUI ignores the id: stops the running job
            log(f"  cancelled {len(prompt_ids)} job(s) in ComfyUI")
        except Exception as e:
            log(f"  could not cancel the queued jobs ({type(e).__name__}: {e}); clear them with: "
                f"curl --request POST --data '{{\"clear\": true}}' {self.url}/queue && "
                f"curl --request POST {self.url}/interrupt")

    def wait(self, prompt_id: str, timeout: float = JOB_TIMEOUT) -> dict:
        """The job's outputs, once it is done."""
        return self.wait_entry(prompt_id, timeout).get("outputs", {})

    HANG_HINT = ("ComfyUI looks hung: nothing from it for {mins} min. Look for the cause, then restart it:\n"
                 "  journalctl --unit comfyui --lines 40 --no-pager | grep --ignore-case --extended-regexp "
                 "'hsa|error|out of memory'\n  sudo systemctl restart comfyui")

    def wait_entry(self, prompt_id: str, timeout: float = JOB_TIMEOUT, idle=None) -> dict:
        """The job's whole /history entry (outputs, status messages with timestamps), once it is done.
        A ComfyUI that is up but slow to answer (a big model swapping, #51: Qwen) is waited for until `timeout`;
        only refused or dropped connections (a crash) give up after DOWN_LIMIT seconds.
        idle() -> seconds since ComfyUI last showed life (Events.idle), or None: over HANG_AFTER it is hung."""
        t0 = time.time()
        down_since = None  # set while ComfyUI is unreachable (crashed, systemd restarting it)
        was_down = False  # it was unreachable at some point: on its return, check it still has the job
        slow_logged = False
        last_check = 0.0  # last /queue look for the hang check
        while time.time() - t0 < timeout:
            try:
                hist = json.loads(self._get(f"/history/{prompt_id}", timeout=POLL_TIMEOUT))
                if was_down and prompt_id not in hist and not self._queued(prompt_id):
                    raise RuntimeError(f"ComfyUI restarted and lost the job. {self.CRASH_HINT}")
                down_since, was_down = None, False
            except (urllib.error.URLError, ConnectionError, http.client.HTTPException, TimeoutError) as e:
                if _is_timeout(e):  # it's there, just busy: not a crash
                    down_since = None
                    if not slow_logged:
                        log(f"  ComfyUI is slow to answer (swapping?); still waiting, up to {timeout:.0f}s per job")
                        slow_logged = True
                    continue
                down_since, was_down = down_since or time.time(), True
                if time.time() - down_since > DOWN_LIMIT:
                    raise RuntimeError(f"lost the connection to ComfyUI for {DOWN_LIMIT}s. {self.CRASH_HINT}")
                time.sleep(2)
                continue
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI failed: {json.dumps(status.get('messages'))[:2000]}")
                return entry
            if idle is not None and (quiet := idle()) is not None and quiet > HANG_AFTER \
                    and time.time() - last_check > 30:
                last_check = time.time()
                if self._running(prompt_id):  # ComfyUI only talks to the client whose job runs: one of
                    raise RuntimeError(self.HANG_HINT.format(mins=round(quiet / 60)))  # someone else's may be long
            time.sleep(1)
        raise TimeoutError(f"prompt {prompt_id} did not finish in {timeout:.0f}s")

    def upload(self, data: bytes, name: str) -> str:
        """Upload a PNG to ComfyUI's input folder; returns the name LoadImage expects."""
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + data + \
            (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue"
             f"\r\n--{boundary}--\r\n").encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=SLOW_TIMEOUT) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI rejected the upload: {e.read().decode(errors='replace')[:500]}")
        return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]

    def fetch(self, image: dict) -> bytes:
        q = urllib.parse.urlencode({k: image.get(k, "") for k in ("filename", "subfolder", "type")})
        return self._get(f"/view?{q}", timeout=SLOW_TIMEOUT)


# ---------------------------------------------------------------- timing (per stage, from ComfyUI events)

class Events:
    """Listens on ComfyUI's websocket (/ws?clientId=...) and stamps each job's events with the local time.

    ComfyUI sends `executing` {node} when a node starts (node None when the job is done), plus
    execution_start / execution_cached / execution_success. That is the only place per-node times
    come from: /history only has the start and end. Best effort: if the socket can't connect or drops,
    timing falls back to /history (queue and total only). Stdlib only, so no new dependency.
    """

    def __init__(self, url: str, client_id: str, timeout: float = 5):
        self.by_prompt: dict[str, list[tuple[float, str, dict]]] = {}
        self.last_frame = time.time()  # any frame (events, sampler previews): ComfyUI's worker is alive
        self.listeners: list = []  # called as f(prompt_id, type, data, time) for every job event (live progress)
        self.ok = False
        self._sock = None
        self._alive = False
        self._send_lock = threading.Lock()
        self._changed = threading.Condition()
        try:
            self._sock = self._connect(url, client_id, timeout)
            self.ok = self._alive = True
            threading.Thread(target=self._loop, daemon=True).start()
        except Exception as e:
            log(f"note: no ComfyUI websocket, timing has no per-stage split ({type(e).__name__}: {e})")
            self.close()

    @staticmethod
    def _connect(url: str, client_id: str, timeout: float):
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if u.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path.rstrip("/") or "") + f"/ws?clientId={urllib.parse.quote(client_id)}"
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError("closed during the handshake")
            head += chunk
            if len(head) > 16384:
                raise ConnectionError("handshake answer too long")
        head, _, rest = head.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(status.decode(errors="replace"))
        sock.settimeout(None)
        return _Buffered(sock, rest)

    def _loop(self) -> None:
        parts: list[bytes] = []
        try:
            while True:
                fin, op, data = self._frame()
                self.last_frame = time.time()
                if op == 8:  # close
                    return
                if op == 9:  # ping
                    self._send(10, data)
                    continue
                if op in (1, 0) and (op == 1 or parts):  # text, or the continuation of one
                    parts.append(data)
                    if fin:
                        self._record(b"".join(parts))
                        parts = []
                elif op == 2:  # binary: sampler previews, not needed
                    parts = []
        except Exception:
            pass  # closed by us or by ComfyUI: keep what was recorded
        finally:
            with self._changed:
                self._alive = False
                self._changed.notify_all()

    def idle(self) -> float | None:
        """Seconds since ComfyUI last sent anything, or None without a working socket (then nobody knows)."""
        return time.time() - self.last_frame if self.ok and self._alive else None

    def wait_done(self, prompt_id: str, timeout: float = 2.0) -> list | None:
        """The job's events, once the socket has its end too. /history can show a job as done a moment
        before its last websocket frame is read; waiting briefly here keeps the per-node split."""
        def done() -> bool:
            return any(kind == "execution_success" or (kind == "executing" and d.get("node") is None)
                       for _, kind, d in self.by_prompt.get(prompt_id, []))
        with self._changed:
            self._changed.wait_for(lambda: done() or not self._alive, timeout)
        return self.by_prompt.get(prompt_id)

    def _frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._sock.read(2)
        n = b2 & 0x7F
        if n == 126:
            n = int.from_bytes(self._sock.read(2), "big")
        elif n == 127:
            n = int.from_bytes(self._sock.read(8), "big")
        mask = self._sock.read(4) if b2 & 0x80 else None
        data = self._sock.read(n)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return bool(b1 & 0x80), b1 & 0x0F, data

    def _send(self, op: int, data: bytes = b"") -> None:
        """One masked frame (client frames must be); only pong and close, so always under 126 bytes."""
        mask = os.urandom(4)
        with self._send_lock:  # the reader thread answers pings while close() may run on the main one
            if self._sock is not None:
                self._sock.sendall(bytes([0x80 | op, 0x80 | len(data[:125])]) + mask +
                                   bytes(c ^ mask[i % 4] for i, c in enumerate(data[:125])))

    def _record(self, raw: bytes) -> None:
        t = time.time()
        try:
            msg = json.loads(raw)
            data = msg.get("data") or {}
            if pid := data.get("prompt_id"):
                with self._changed:
                    self.by_prompt.setdefault(pid, []).append((t, msg.get("type", ""), data))
                    self._changed.notify_all()
                for f in list(self.listeners):
                    try:
                        f(pid, msg.get("type", ""), data, t)
                    except Exception:
                        pass  # the progress view must never stop the recording
        except (ValueError, AttributeError):
            pass

    def __enter__(self) -> "Events":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._send(8)
        except Exception:
            pass
        with self._send_lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


class _Buffered:
    """A socket with the bytes already read past the handshake put back in front."""

    def __init__(self, sock, rest: bytes):
        self.sock, self.buf = sock, rest

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


# Node class -> stage. Loaders read weights from disk; moving them onto the GPU happens inside the
# sampler, so a cold start shows up in sample_s as well as load_s.
# Exact class names first, then substrings (so UNETLoader, KSamplerAdvanced, VAEEncodeForInpaint match).
STAGE_CLASSES = {"LoadImage": "load", "LoadImageMask": "load",
                 "ImageUpscaleWithModel": "upscale", "ImageScale": "upscale", "ImageScaleBy": "upscale",
                 "LatentUpscale": "upscale", "LatentUpscaleBy": "upscale",
                 "SaveImage": "save", "PreviewImage": "save", "KSamplerSelect": "other"}
STAGES = (("load", ("Loader",)), ("sample", ("KSampler", "SamplerCustom")),
          ("encode", ("TextEncode", "VAEEncode")), ("decode", ("VAEDecode",)))  # TextEncode: also Qwen's
STAGE_ORDER = ("queue", "load", "encode", "sample", "decode", "upscale", "save", "other", "fetch")
COMFY_STAGES = tuple(f"{st}_s" for st in STAGE_ORDER[1:-1])  # the parts of comfy_s


def stage_of(class_type: str) -> str:
    if class_type in STAGE_CLASSES:
        return STAGE_CLASSES[class_type]
    for stage, keys in STAGES:
        if any(k in class_type for k in keys):
            return stage
    return "other"


def job_timing(workflow: dict, queued_at: float, events: list | None, history: dict) -> dict:
    """Seconds per stage for one ComfyUI job: queue wait, per node (grouped into stages) and ComfyUI total.

    queue_s is from queueing to ComfyUI starting it, so in a multi-image run it includes the images
    before it. Node times need the websocket events; without them only queue_s and comfy_s are known.
    """
    def secs(x: float) -> float:
        return round(max(0.0, x), 2)

    hist = {m[0]: m[1] for m in (history.get("status") or {}).get("messages") or []
            if isinstance(m, (list, tuple)) and len(m) == 2 and isinstance(m[1], dict)}
    ev = events or []
    start = next((t for t, kind, _ in ev if kind == "execution_start"), None)
    end = next((t for t, kind, d in ev if kind == "execution_success"
                or (kind == "executing" and d.get("node") is None)), None)
    if end is None:  # the socket missed the end (or there is none): /history's start and end, same clock
        ev, start = [], None
        h_start, h_end = (hist.get(k, {}).get("timestamp") for k in ("execution_start", "execution_success"))
        if isinstance(h_start, (int, float)):
            start = h_start / 1000
            if isinstance(h_end, (int, float)):
                end = h_end / 1000

    out: dict = {}
    if start is not None:
        out["queue_s"] = secs(start - queued_at)
    if start is not None and end is not None:
        out["comfy_s"] = secs(end - start)
    starts = [(t, d.get("display_node") or d.get("node")) for t, kind, d in ev
              if kind == "executing" and d.get("node") is not None]
    if starts and end is not None:
        nodes = []
        for (t, node), (t_next, _) in zip(starts, starts[1:] + [(end, None)]):
            cls = (workflow.get(str(node)) or {}).get("class_type", "?")
            nodes.append({"node": str(node), "class": cls, "s": secs(t_next - t)})
            key = stage_of(cls) + "_s"
            out[key] = round(out.get(key, 0) + nodes[-1]["s"], 2)
        out["nodes"] = nodes
    cached = next((d.get("nodes") for _, kind, d in ev if kind == "execution_cached"), None)
    if cached is None:
        cached = hist.get("execution_cached", {}).get("nodes")
    if cached:
        out["cached"] = [str(n) for n in cached]
    return out


def _flat(t: dict) -> dict:
    """A timing dict without its node lists (what run.json keeps per image)."""
    return {k: v for k, v in t.items() if k not in ("nodes", "cached")}


def _stage_parts(t: dict) -> list[str]:
    """ "sample 41.2s" etc. for each stage in t, in pipeline order (not the totals or the queue wait)."""
    keys = [k[:-2] for k in t if k.endswith("_s") and k not in ("total_s", "comfy_s", "setup_s", "queue_s")]
    keys.sort(key=lambda k: (STAGE_ORDER.index(k) if k in STAGE_ORDER else len(STAGE_ORDER), k))
    return [f"{k} {t[k + '_s']:.1f}s" for k in keys if t[k + "_s"] >= 0.05]


def timing_summary(run_timing: dict, image_timings: list[dict]) -> str:
    """One line, e.g. "4 images in 210.3s (first 80.1s, then ~43.4s each): sample 160.2s, upscale 30.0s, ..."."""
    n = len(image_timings)
    head = f"{n} image{'s' if n != 1 else ''} in {run_timing['total_s']:.1f}s"
    per = [t["total_s"] for t in image_timings if "total_s" in t]
    if len(per) > 1:
        head += f" (first {per[0]:.1f}s, then ~{sum(per[1:]) / (len(per) - 1):.1f}s each)"
    parts = _stage_parts(run_timing)
    return head + (": " + ", ".join(parts) if parts else "")


# ---------------------------------------------------------------- post-processing

def remove_bg(path: Path) -> Path:
    from rembg import remove  # lazy: heavy import, only icons need it
    out = path.with_name(path.stem + "-transparent.png")
    out.write_bytes(remove(path.read_bytes()))
    return out


def cutout(path: Path, clean: bool = False) -> Path:
    """remove_bg, then with clean also the CLEANUP steps (drop stray shapes, centre on a square)."""
    out = remove_bg(path)
    for step in CLEANUP if clean else []:
        out = POST[step](out)
    return out


ALPHA_SOLID = 8         # alpha above this counts as part of a shape
ISLAND_GRID = 256       # keep_largest finds shapes on a grid this big (fast in pure Python, no numpy)
ISLAND_KEEP = 0.25      # keep_largest keeps every shape at least this share of the largest one
TRIM_MARGIN = 0.06      # trim leaves this share of the canvas free on each side


def _rgba(path: Path):
    """The image as RGBA, or None if it has no transparency to work with (nothing to clean)."""
    from PIL import Image
    with Image.open(path) as im:
        im.load()
    if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
        return None
    return im.convert("RGBA")


def keep_largest(path: Path) -> Path:
    """Drop stray shapes around a cut-out: keep the largest connected opaque shape, plus any other
    shape at least ISLAND_KEEP of its size (so a two-part icon survives). Rewrites path in place.
    Runs after remove_bg; an image without transparency is left as it is."""
    from PIL import Image, ImageChops, ImageFilter
    im = _rgba(path)
    if im is None:
        return path
    alpha = im.getchannel("A")
    w, h = im.size
    k = min(1.0, ISLAND_GRID / max(w, h))
    gw, gh = max(1, round(w * k)), max(1, round(h * k))
    # BOX keeps thin connecting lines as low (but non-zero) alpha, so shapes don't fall apart
    grid = alpha.resize((gw, gh), Image.BOX).tobytes()
    labels = [0] * (gw * gh)
    sizes = [0]  # sizes[label]
    for start in range(gw * gh):
        if labels[start] or grid[start] <= ALPHA_SOLID:
            continue
        label = len(sizes)
        labels[start] = label
        stack, n = [start], 0
        while stack:
            i = stack.pop()
            n += 1
            y, x = divmod(i, gw)
            for ny in (y - 1, y, y + 1):
                if 0 <= ny < gh:
                    for nx in (x - 1, x, x + 1):
                        j = ny * gw + nx
                        if 0 <= nx < gw and not labels[j] and grid[j] > ALPHA_SOLID:
                            labels[j] = label
                            stack.append(j)
        sizes.append(n)
    if len(sizes) <= 2:  # zero or one shape: nothing to drop
        return path
    keep_min = max(sizes) * ISLAND_KEEP
    removed = sum(1 for n in sizes[1:] if n < keep_min)
    if not removed:
        return path
    keep = bytes(255 if sizes[lb] >= keep_min else 0 for lb in labels)
    mask = Image.frombytes("L", (gw, gh), keep).resize((w, h), Image.NEAREST)
    cell = max(1, round(1 / k))
    mask = mask.filter(ImageFilter.MaxFilter(2 * cell + 1))  # grow by a grid cell: keeps soft edges
    im.putalpha(ImageChops.darker(alpha, mask))
    im.save(path)
    log(f"  keep_largest: dropped {removed} stray shape(s) from {path.name}")
    return path


def trim(path: Path) -> Path:
    """Crop a cut-out to its content, centre it on a square with a TRIM_MARGIN border and scale it to
    the canvas's shorter side (1024 for icons), so a set of icons comes out the same size. A non-square
    canvas becomes square. Rewrites path in place.
    Runs after remove_bg (and keep_largest); an image without transparency is left as it is."""
    from PIL import Image
    im = _rgba(path)
    if im is None:
        return path
    box = im.getchannel("A").point(lambda v: 255 if v > ALPHA_SOLID else 0).getbbox()
    if box is None:  # fully transparent: nothing to centre
        return path
    w, h = im.size
    content = im.crop(box)
    side = max(content.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(content, ((side - content.width) // 2, (side - content.height) // 2))
    out_side = min(w, h)
    inner = max(1, round(out_side * (1 - 2 * TRIM_MARGIN)))
    out = Image.new("RGBA", (out_side, out_side), (0, 0, 0, 0))
    off = (out_side - inner) // 2
    out.paste(canvas.resize((inner, inner), resample=Image.LANCZOS), (off, off))
    out.save(path)
    return path


# crt: the warm look of an old CRT/VHS screen, added to a finished image (no model, no GPU). The knobs live in
# [crt] in config.toml; CRT_DEFAULTS fills any that are missing. Sizes are for a 1080-line image and scale with it.
CRT_DEFAULTS = {
    "lines": 360,      # scanlines over the image height (fewer = thicker, further apart)
    "scanline": 0.22,  # how dark the gaps between lines get, 0-1 (0 = no lines)
    "blur": 0.6,       # softness, in px at 1080 lines (old screens were never sharp)
    "bleed": 1.0,      # red/blue fringes, px at 1080 lines (colour bleeding sideways, like VHS)
    "glow": 0.2,       # bright parts bloom into their surroundings, 0-1
    "warmth": 0.04,    # warmer colours (a bit more red, less blue), 0-0.2
    "vignette": 0.18,  # darker corners, 0-1
}


def crt_settings(conf: dict | None = None, **overrides) -> dict:
    s = {**CRT_DEFAULTS, **(conf or load_config()).get("crt", {}),
         **{k: v for k, v in overrides.items() if v is not None}}
    unknown = set(s) - set(CRT_DEFAULTS)
    if unknown:
        raise ValueError(f"[crt]: unknown {', '.join(sorted(unknown))} (known: {', '.join(CRT_DEFAULTS)})")
    return {k: float(v) for k, v in s.items()}


def crt_image(im, s: dict):
    """The CRT/VHS look on a PIL image (RGB or RGBA; alpha is kept): bleed, blur, glow, warmth, then one mask for
    scanlines and vignette. Pillow only (no numpy), a few seconds for 4K."""
    import math
    from PIL import Image, ImageChops, ImageFilter
    alpha = im.getchannel("A") if im.mode == "RGBA" else None
    im = im.convert("RGB")
    w, h = im.size
    k = h / 1080
    r, g, b = im.split()
    if (shift := min(round(s["bleed"] * k), w - 1)) > 0:  # red right, blue left; the edge columns stay put
        r2, b2 = r.copy(), b.copy()
        r2.paste(r.crop((0, 0, w - shift, h)), (shift, 0))
        b2.paste(b.crop((shift, 0, w, h)), (0, 0))
        r, b = r2, b2
    if s["warmth"] > 0:
        r = r.point(lambda v: min(255, round(v * (1 + s["warmth"]))))
        b = b.point(lambda v: round(v * (1 - s["warmth"])))
    im = Image.merge("RGB", (r, g, b))
    if s["blur"] > 0:
        im = im.filter(ImageFilter.GaussianBlur(s["blur"] * k))
    if s["glow"] > 0:
        bright = im.point(lambda v: max(0, v - 160) * 255 // 95)  # only the bright parts glow
        halo = bright.filter(ImageFilter.GaussianBlur(12 * k)).point(lambda v: round(v * s["glow"]))
        im = ImageChops.screen(im, halo)
    mask = None
    if s["scanline"] > 0 and s["lines"] > 0:
        period = h / s["lines"]  # a soft wave, not hard black lines
        col = bytes(round(255 * (1 - s["scanline"] * (0.5 - 0.5 * math.cos(2 * math.pi * y / period))))
                    for y in range(h))
        mask = Image.frombytes("L", (1, h), col).resize((w, h), Image.NEAREST)
        # the lines darken by scanline/2 on average: lift the mid-tones back with a gamma curve, which
        # (unlike a gain) never clips the highlights
        gamma = 1 - s["scanline"] / 2
        im = im.point(lambda v: round(255 * (v / 255) ** gamma))
    if s["vignette"] > 0:
        gw, gh = 64, max(2, round(64 * h / w))
        grid = bytes(round(255 * (1 - s["vignette"] * min(1.0, ((x / (gw - 1) - 0.5) ** 2 + (y / (gh - 1) - 0.5) ** 2) * 2) ** 1.5))
                     for y in range(gh) for x in range(gw))
        vig = Image.frombytes("L", (gw, gh), grid).resize((w, h), Image.BILINEAR)
        mask = vig if mask is None else ImageChops.multiply(mask, vig)
    if mask is not None:
        im = ImageChops.multiply(im, Image.merge("RGB", (mask, mask, mask)))
    if alpha is not None:
        im.putalpha(alpha)
    return im


def crt_source(path: Path) -> Path:
    """The clean image a -crt.png was made from (so re-running crt never stacks the effect)."""
    if path.stem.endswith("-crt"):
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            raw = path.with_name(path.stem.removesuffix("-crt") + ext)
            if raw.is_file():
                return raw
    return path


def crt(path: Path, **overrides) -> Path:
    """Writes <stem>-crt.png next to the image (the clean one stays) and returns it. overrides: [crt] knobs."""
    from PIL import Image
    src = crt_source(Path(path))
    s = crt_settings(**overrides)
    with Image.open(src) as im:
        im.load()
    out = src.with_name(src.stem + "-crt.png")
    crt_image(im, s).save(out)
    return out


POST = {"remove_bg": remove_bg, "keep_largest": keep_largest, "trim": trim, "crt": crt}
CLEANUP = ["keep_largest", "trim"]  # what `gen cutout --clean` runs after remove_bg


# ---------------------------------------------------------------- source images (img2img / inpaint)

SOURCE_PIXELS = 1024 * 1024  # SDXL's native area; sources are resized to about this
MASK_FEATHER = 4  # px of blur on mask edges


def _source_size(w: int, h: int) -> tuple[int, int]:
    """Keep the aspect ratio, scale to ~1 MP, round each side to a multiple of 64."""
    k = (SOURCE_PIXELS / (w * h)) ** 0.5
    return max(64, round(w * k / 64) * 64), max(64, round(h * k / 64) * 64)


# Kontext's preferred sizes (ComfyUI's FluxKontextImageScale picks the closest aspect ratio from these).
KONTEXT_SIZES = [(672, 1568), (688, 1504), (720, 1456), (752, 1392), (800, 1328), (832, 1248), (880, 1184),
                 (944, 1104), (1024, 1024), (1104, 944), (1184, 880), (1248, 832), (1328, 800), (1392, 752),
                 (1456, 720), (1504, 688), (1568, 672)]


def kontext_size(w: int, h: int) -> tuple[int, int]:
    """The Kontext size with the closest aspect ratio, so FluxKontextImageScale leaves the image as is."""
    return min(KONTEXT_SIZES, key=lambda s: abs(s[0] / s[1] - w / h))


def _on_canvas(im, canvas: tuple[int, int]):
    """im scaled to fit inside canvas (w, h), centred on a canvas of its corners' average colour."""
    from PIL import Image
    cw, ch = canvas
    w, h = im.size
    corners = [im.getpixel(xy) for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    fill = tuple(round(sum(c[i] for c in corners) / 4) for i in range(3))
    scale = min(cw / w, ch / h)
    fit = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    out = Image.new("RGB", canvas, fill)
    out.paste(fit, ((cw - fit.width) // 2, (ch - fit.height) // 2))
    return out


def prepare_source(path: Path, mask: Path | None = None, size_fn=None,
                   canvas: tuple[int, int] | None = None) -> tuple[bytes, bytes | None, tuple[int, int]]:
    """Load the source (and optional mask) as PNG bytes at an SDXL-friendly size.

    Transparency is flattened onto white, so icons and cut-outs work as sources.
    Mask: white = repaint, black = keep. It is resized to the source's size and its edge is
    feathered by MASK_FEATHER px, so pixels just outside the white area blend a little.
    Output is at this ~1 MP size, not the source's resolution. size_fn(w, h) picks another size
    (edit uses kontext_size). canvas=(w, h): fit the whole image into a w x h canvas instead, centred on its
    corner colour (#51: Qwen draws at the ref's size, so a portrait ref on a 16:9 canvas gives a 16:9 wallpaper).
    """
    from PIL import Image  # lazy: only img2img needs it

    def png(im) -> bytes:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    from PIL import ImageOps
    im = ImageOps.exif_transpose(Image.open(path))  # phone photos: apply the EXIF rotation
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    if canvas is not None:
        im = _on_canvas(im.convert("RGB"), canvas)
    size = canvas or (size_fn or _source_size)(*im.size)
    src = png(im.convert("RGB").resize(size, Image.LANCZOS))
    m = None
    if mask is not None:
        mk = ImageOps.exif_transpose(Image.open(mask))
        from PIL import ImageFilter
        # feather the edge a little so the repainted part blends into the kept part
        m = png(mk.convert("L").resize(size, Image.LANCZOS)
                .filter(ImageFilter.GaussianBlur(MASK_FEATHER)).convert("RGB"))
    return src, m, size


POST_SUFFIXES = ("-crt", "-transparent")  # what post steps add to an image's name, outermost first


def sidecar_paths(path: Path) -> list[Path]:
    """Where an image's sidecar may be: next to it, or next to the raw image a post step made it from
    (NAME-transparent.png, NAME-crt.png, NAME-transparent-crt.png -> NAME.json)."""
    stem, out = path.stem, [path.with_suffix(".json")]
    for suffix in POST_SUFFIXES:
        stem = stem.removesuffix(suffix)
        out.append(path.with_name(stem + ".json"))
    return list(dict.fromkeys(out))


def find_sidecar(path: Path) -> dict | None:
    """The JSON sidecar written next to a generated image (also for -transparent.png / -crt.png files)."""
    for cand in sidecar_paths(path):
        if cand.is_file():
            try:
                meta = json.loads(cand.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(meta, dict):
                return meta
    return None


# ---------------------------------------------------------------- helpers

def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "image"


def to_windows_path(p: Path | str) -> str | None:
    try:
        return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def to_linux_path(p: str) -> Path:
    """Accept C:\\... or \\\\wsl$\\... (Windows) or a normal Linux path."""
    if re.match(r"^[A-Za-z]:[\\/]", p) or p.startswith("\\\\"):
        out = subprocess.run(["wslpath", "-u", p], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    return Path(p).expanduser()


# ---------------------------------------------------------------- main API

def _filled_or(template: dict, params: dict) -> dict:
    """The workflow as the first job gets it (so the live view knows each sampler's steps), else the template."""
    try:
        return fill(template, params)
    except Exception:
        return template


@contextlib.contextmanager
def _progress(template: dict, params: dict, preset: str, count: int, comfy: "Comfy", events: "Events",
              run_dir: Path, mode: str):
    """Print what is about to run (#61); on a terminal, also draw the live block until the run ends.
    params: what the first job is filled with (the header reads models, steps and sizes from it)."""
    global _LIVE
    import progress  # lazy: only generate needs it
    try:
        log(progress.describe(fill(template, params), preset, count, stage_of, mode))
    except Exception as e:  # an odd workflow must not stop the run
        log(f"note: no preset summary ({type(e).__name__}: {e})")
    live = None
    if progress.enabled() and events.ok and _LIVE is None:  # without the websocket there is nothing to show
        try:
            est = progress.history_estimate(run_dir.parent.parent, preset, mode, COMFY_STAGES)
            live = progress.Progress(_filled_or(template, params), count, stage_of, STAGE_ORDER, comfy.url, est)
        except Exception as e:  # the view must never stop a run
            log(f"note: no live progress ({type(e).__name__}: {e})")
    if live is None:
        yield None
        return
    events.listeners.append(live.on_event)
    _LIVE = live.start()
    try:
        yield live
    finally:
        try:
            peak = live.stop()
        finally:
            _LIVE = None
        if peak:
            log(peak)


BLANK_RETRIES = 1  # a job whose image comes out black is rendered again this many times
BLANK_MAX = 8       # "black": no channel of any pixel brighter than this (ComfyUI turns NaNs into 0)


def is_blank(data: bytes) -> bool:
    """True for an all-black (or unreadable) PNG: what a NaN render looks like."""
    try:
        from PIL import Image  # lazy, like the other image helpers
        with Image.open(io.BytesIO(data)) as im:
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            return all(hi <= BLANK_MAX for _, hi in im.getextrema()[:3])
    except Exception:
        return False  # not ours to judge; the post steps or the viewer will complain


def _finish_job(comfy: "Comfy", events: "Events", pid: str, side_nodes: frozenset = frozenset()):
    """Wait for a job and fetch its output images: (history entry, outs, [{png, fetch_s}], [blank?], side).
    side_nodes: SaveImage nodes whose images are by-products (a cast run's scene): fetched into side as
    [(out, png)], not in outs and not checked for black."""
    entry = comfy.wait_entry(pid, idle=events.idle)
    if _LIVE is not None:  # in case the socket dropped the job's end
        _LIVE.on_event(pid, "execution_success", {}, time.time())
    found = [(nid, im) for nid, o in entry.get("outputs", {}).items() for im in o.get("images", [])
             if im.get("type") == "output"]
    outs = [im for nid, im in found if nid not in side_nodes]
    datas, side = [], []
    for im in outs:
        t0 = time.time()
        datas.append({"png": comfy.fetch(im), "fetch_s": round(time.time() - t0, 2)})
    for nid, im in found:
        if nid in side_nodes:
            try:  # a by-product: losing it must not lose the run
                side.append((im, comfy.fetch(im)))
            except Exception as e:
                log(f"note: could not fetch {im.get('filename')} ({type(e).__name__}: {e})")
    return entry, outs, datas, [is_blank(d["png"]) for d in datas], side


def _upstream(wf: dict, ids) -> set:
    """The nodes the given ones depend on (themselves included)."""
    seen, todo = set(), list(ids)
    while todo:
        nid = todo.pop()
        if nid in seen or nid not in wf:
            continue
        seen.add(nid)
        todo += [v[0] for v in wf[nid].get("inputs", {}).values() if isinstance(v, list) and v and v[0] in wf]
    return seen


def _uncached(wf: dict, n: int, scene_too: bool = False) -> dict:
    """The same graph with an extra input on its seed node(s), which ComfyUI ignores when running the node but
    counts in its cache key: re-queued as it was, every node would be a cache hit and give the black image back.
    A cast graph's scene pass (#52) stays cached, so only the edit is redrawn, unless the scene is black too."""
    wf = json.loads(json.dumps(wf))
    keep = set() if scene_too else _upstream(wf, scene_nodes(wf))
    nodes = [nd for nid, nd in wf.items() if {"seed", "noise_seed"} & set(nd.get("inputs", {})) and nid not in keep] \
        or list(wf.values())
    for nd in nodes:
        nd.setdefault("inputs", {})["imagegen_retry"] = n
    return wf


# ---------------------------------------------------------------- scene + cast (#52)
# A cast workflow draws the scene from text, then a Qwen edit puts the characters from the ref images into it
# (images.image_1 = the scene, image_2.. = one ref each). Its LoadImage node with "{{ref}}" is cloned per ref.

REF_PLACEHOLDER = "{{ref}}"
SCENE_PREFIX = "imagegen-scene"  # SaveImage filename_prefix of a cast workflow's scene (kept as NN-seedS-scene.png)
MAX_REFS = 15                    # TextEncodeQwenImage21 takes 16 images: the scene + 15 refs
_IMAGE_SLOT = re.compile(r"images\.image_(\d+)")


def takes_refs(workflow_text: str) -> bool:
    return REF_PLACEHOLDER in workflow_text


def is_cast_preset(p: dict) -> bool:
    """A preset whose text workflow takes ref images (wallpaper_cast); a missing workflow file is not one."""
    try:
        return bool(p.get("workflow")) and takes_refs((ROOT / "workflows" / p["workflow"]).read_text())
    except OSError:
        return False


def scene_nodes(wf: dict) -> frozenset:
    return frozenset(nid for nid, n in wf.items() if n.get("class_type") == "SaveImage"
                     and n.get("inputs", {}).get("filename_prefix") == SCENE_PREFIX)


def expand_refs(template: dict, n: int) -> dict:
    """The template with its "{{ref}}" LoadImage node cloned n times ("{{ref_1}}".."{{ref_n}}"), and every
    images.image_K input that links to it repeated as image_K, image_K+1, ... one per clone."""
    wf = json.loads(json.dumps(template))
    loaders = [nid for nid, nd in wf.items() if nd.get("inputs", {}).get("image") == REF_PLACEHOLDER]
    if len(loaders) != 1 or n < 1:
        raise ValueError(f"a cast workflow needs exactly one LoadImage with {REF_PLACEHOLDER} (found {len(loaders)})"
                         if n >= 1 else "a cast workflow needs at least one ref image")
    nid = loaders[0]
    ids = [nid] + [f"{nid}{k:02d}" for k in range(2, n + 1)]
    taken = set(wf) & set(ids[1:])
    if taken:
        raise ValueError(f"cast workflow: node id(s) {', '.join(sorted(taken))} clash with the ref clones")
    base = wf[nid]
    for k, cid in enumerate(ids, 1):
        wf[cid] = {**json.loads(json.dumps(base)), "inputs": {**base["inputs"], "image": "{{ref_%d}}" % k}}
    for node in wf.values():
        inputs = node.get("inputs", {})
        for key, v in list(inputs.items()):
            m = _IMAGE_SLOT.fullmatch(key)
            if m and isinstance(v, list) and v and v[0] == nid:
                first = int(m.group(1))
                for k, cid in enumerate(ids):
                    slot = f"images.image_{first + k}"
                    if k and slot in inputs:
                        raise ValueError(f"cast workflow: {slot} is taken, the refs need image_{first}..")
                    inputs[slot] = [cid, v[1]]
    return wf


def image_list(n: int, first: int = 2) -> str:
    """"image 2", "images 2 and 3", "images 2, 3 and 4": how a cast prompt names the ref images."""
    nums = [str(first + k) for k in range(n)]
    return f"image {nums[0]}" if n == 1 else f"images {', '.join(nums[:-1])} and {nums[-1]}"


def generate(prompt: str, preset: str = "draft", count: int = 1, seed: int | None = None,
             negative: str | None = None, width: int | None = None, height: int | None = None,
             steps: int | None = None, cfg: float | None = None, label: str | None = None,
             source_image: str | None = None, mask: str | None = None,
             denoise: float | None = None, edit: bool = False, guidance: float | None = None,
             styles: list[str] | None = None, post: list[str] | None = None,
             prompts: list[str] | None = None, refs: list[str] | None = None,
             cast_prompt: str | None = None) -> dict:
    """txt2img by default. With source_image: img2img (denoise = how much may change, 0-1).
    With source_image + mask: inpaint, only the white part of the mask is repainted.
    With source_image + edit=True: the prompt is an edit instruction for the preset's edit_workflow.
    styles: [styles.NAME] looks added after the preset suffix (None = the preset's `styles`, [] = none);
    a style's `post` steps run after the preset's. post: more steps after those (gen daily: its styles' posts).
    prompts: one prompt per image instead (image i gets prompts[i % len]; a converge pass gives each
    image its own idea, #68); `prompt` stays the run's label prompt.
    refs (#52): for a cast preset (its workflow has "{{ref}}", e.g. wallpaper_cast): the prompt draws the scene,
    then cast_prompt (default: the preset's, "{images}" = "image 2"..) puts the ref images' characters into it."""
    t_start = time.time()
    conf = load_config()
    presets = conf["presets"]
    if preset not in presets:
        raise ValueError(f"unknown preset '{preset}'. Available: {', '.join(presets)}")
    p = dict(presets[preset])
    count = max(1, int(count))
    styles_arg = styles
    if styles is None and edit and refs:
        styles = []  # gen edit SCENE "Replace ..." --ref (#52): an instruction, not a scene: no look, no style post
    styles = check_styles(p.get("styles", []) if styles is None else styles, conf)
    post = style_post(styles, conf, p.get("post", []) + list(post or []))
    if "crt" in post:
        crt_settings(conf)  # a bad [crt] knob fails now, not after ComfyUI's work
    if mask is not None and source_image is None:
        raise ValueError("a mask needs a source image")
    if edit:
        if source_image is None:
            raise ValueError("edit needs a source image")
        if mask is not None:
            raise ValueError("edit takes no mask (describe what to change instead)")
        ignored = [k for k, v in (("negative", negative), ("denoise", denoise), ("cfg", cfg),
                                  ("width", width), ("height", height)) if v is not None]
        if ignored:  # the Kontext graph has none of these (cfg is fixed at 1, size follows the source)
            raise ValueError(f"edit does not take {', '.join(ignored)}")
        if "edit_workflow" not in p:
            edit_presets = [n for n, q in presets.items() if "edit_workflow" in q and not is_cast_preset(q)]
            raise ValueError(f"preset '{preset}' can't edit; edit presets: {', '.join(edit_presets) or 'none'}")
    elif "workflow" not in p:
        raise ValueError(f"preset '{preset}' is edit-only: use gen edit IMAGE \"instruction\"")

    workflow = p.get("workflow")
    src_path = mask_path = None
    if source_image is not None:
        src_path = to_linux_path(source_image)
        if not src_path.is_file():
            raise FileNotFoundError(src_path)
    if edit:
        workflow = p["edit_workflow"]
        canvas = tuple(int(v) for v in p["ref_canvas"]) if p.get("ref_canvas") else None
        src_png, mask_png, src_size = prepare_source(src_path, size_fn=kontext_size, canvas=canvas)
    elif src_path is not None:
        if mask is not None:
            mask_path = to_linux_path(mask)
            if not mask_path.is_file():
                raise FileNotFoundError(mask_path)
            key = "inpaint_workflow"
        else:
            key = "img2img_workflow"
        workflow = mode_workflow(p, key)
        if workflow is None:
            raise ValueError(f"preset '{preset}' has no {key.split('_')[0]} (gen vary)"
                             + (f"; for this model use gen edit IMAGE \"instruction\" -p {preset}"
                                if "edit_workflow" in p else ""))
        denoise = float(denoise if denoise is not None else (0.85 if mask_path else 0.4))
        if not 0 < denoise <= 1:
            raise ValueError("strength/denoise must be in (0, 1]")
        src_png, mask_png, src_size = prepare_source(src_path, mask_path)
    template_text = (ROOT / "workflows" / workflow).read_text()
    template = json.loads(template_text)
    refs = [r for r in (refs or []) if r]
    ref_paths: list[Path] = []
    if takes_refs(template_text):
        if src_path is not None and not edit:
            raise ValueError(f"preset '{preset}' draws a new scene: it takes refs, not a source image")
        if not refs:
            raise ValueError(f"preset '{preset}' puts characters into the scene: give it ref images (--ref)")
        if len(refs) > MAX_REFS:
            raise ValueError(f"at most {MAX_REFS} refs (got {len(refs)})")
        for r in refs:
            path = to_linux_path(r)
            if not path.is_file():
                raise FileNotFoundError(path)
            ref_paths.append(path)
        template = expand_refs(template, len(ref_paths))
    elif refs or cast_prompt:
        raise ValueError(f"preset '{preset}' takes no refs; cast presets: "
                         + (", ".join(n for n, q in presets.items() if is_cast_preset(q)) or "none"))
    unused = [k for k, v in (("steps", steps), ("cfg", cfg), ("guidance", guidance), ("negative", negative))
              if v is not None and "{{" + k + "}}" not in template_text]
    if unused:  # e.g. -g on klein, or a project's cfg after switching it to klein: say so, then go on
        log(f"note: {workflow} has no {', '.join(unused)}, ignoring {'it' if len(unused) == 1 else 'them'}")
    base_seed = seed if seed is not None else random.randrange(MAX_SEED)
    cast_edit = edit and bool(ref_paths)  # gen edit SCENE "Replace ..." --ref: only the cast pass (#52)
    if cast_edit:
        prompt = prompt.replace("{images}", image_list(len(ref_paths)))  # (styles: [] already, unless given)
    look = style_text(styles, conf)

    def dress(q: str) -> str:  # the preset suffix, then the styles
        full = q + ("" if cast_edit else p.get("suffix", ""))
        return f"{full.rstrip().rstrip(',')}, {look}" if look else full
    full_prompt = dress(prompt)
    per_image = [q for q in (prompts or []) if q and q.strip()]
    if prompts is not None and not per_image:
        raise ValueError("prompts: give at least one non-empty prompt")
    per_image = [per_image[i % len(per_image)] for i in range(count)] if per_image else [prompt] * count
    full_negative = "" if edit else (negative if negative is not None else conf.get("default_negative", "")) \
        + p.get("negative_extra", "")  # edit workflows (Kontext, cfg 1) have no negative
    if not edit and (style_neg := style_text(styles, conf, "negative_extra")):
        full_negative = f"{full_negative.rstrip().rstrip(',')}, {style_neg}" if full_negative.strip() else style_neg

    params = {k: v for k, v in p.items() if k not in NOT_PARAMS}
    params.update(prompt=full_prompt, negative=full_negative)
    if not edit:
        params["checkpoint"] = p.get("checkpoint", conf["checkpoint"])
    for k, v in (("width", width), ("height", height), ("steps", steps), ("cfg", cfg), ("guidance", guidance)):
        if v is not None:
            params[k] = v
    if ref_paths:
        tmpl = cast_prompt if cast_prompt is not None else str(p.get("cast_prompt") or "")
        if not tmpl.strip() and not edit:
            raise ValueError(f"preset '{preset}' has no cast_prompt: pass one")
        params["cast_prompt"] = tmpl.replace("{images}", image_list(len(ref_paths)))
        ref_pngs = [prepare_source(r, size_fn=kontext_size)[0] for r in ref_paths]

    now = datetime.now()
    run_dir = Path(conf["output_dir"]).expanduser() / now.strftime("%Y-%m-%d") / \
        f"{now:%H%M%S}-{preset}-{slug(label or prompt)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    comfy = Comfy(conf["comfy_url"])
    comfy.wait_ready()
    mode = "edit" if edit else "inpaint" if mask_path else "img2img" if src_path else ""
    first = {**params, "seed": base_seed, "cast_seed": cast_seed(base_seed)}
    if src_path is not None:  # what the uploads will fill in (the header only reads sizes and model names)
        # (keep in step with the uploads below; a missing key only costs the header line)
        first.update(image=src_path.name, mask=mask_path.name if mask_path else "",
                     width=src_size[0], height=src_size[1], **({} if edit else {"denoise": denoise}))
    first.update({f"ref_{k}": r.name for k, r in enumerate(ref_paths, 1)})
    with Events(comfy.url, comfy.client_id) as events, \
            _progress(template, first, preset, count, comfy, events, run_dir, mode):
        # Events: per-node times, the live block: what runs and how far it is (#61); both closed even on errors
        source = None
        if src_path is not None:
            # content-addressed names: re-running on the same source/mask overwrites instead of piling up
            def name(kind: str, data: bytes) -> str:
                return f"imagegen-{kind}-{hashlib.sha1(data).hexdigest()[:16]}.png"
            params.update(image=comfy.upload(src_png, name("src", src_png)),
                          width=src_size[0], height=src_size[1])
            if not edit:
                params["denoise"] = denoise
            if mask_png is not None:
                params["mask"] = comfy.upload(mask_png, name("mask", mask_png))
            source = {"source_image": str(src_path), "mask": str(mask_path) if mask_path else None,
                      "denoise": None if edit else denoise, "edit": edit}
            try:  # exactly what the model got, for the contact sheet's input card
                (run_dir / SHEET_SOURCE).write_bytes(src_png)
            except OSError as e:
                log(f"warning: could not save {SHEET_SOURCE}: {e}")
        if ref_paths:
            source = {**(source or {}), "refs": [],
                      "cast_prompt": full_prompt if cast_edit else params["cast_prompt"]}
            for k, (path, data) in enumerate(zip(ref_paths, ref_pngs), 1):
                params[f"ref_{k}"] = comfy.upload(data, f"imagegen-ref-{hashlib.sha1(data).hexdigest()[:16]}.png")
                ref = {"path": str(path), "file": f"ref-{k}.png"}
                try:  # the sheet's ref cards, as the model got them
                    (run_dir / ref["file"]).write_bytes(data)
                except OSError as e:
                    log(f"warning: could not save {ref['file']}: {e}")
                    ref["file"] = None
                source["refs"].append(ref)
        side_nodes = scene_nodes(template)

        # queue everything up front; ComfyUI works through it while we wait
        jobs = []
        done = 0  # jobs finished: on an error/Ctrl-C the rest are cancelled in ComfyUI
        try:
            t_queue = time.time()
            for i in range(count):
                s = (base_seed + i) % MAX_SEED
                wf = fill(template, {**params, "seed": s, "cast_seed": cast_seed(s), "prompt": dress(per_image[i])})
                jobs.append((s, comfy.queue(wf), time.time(), wf))
            log(f"queued {count} x {preset} (seeds {base_seed}..) -> {run_dir}")

            images, job_timings = [], []
            t_wait = time.time()
            for i, (s, pid, queued_at, wf) in enumerate(jobs, 1):
                entry, outs, datas, blanks, side = _finish_job(comfy, events, pid, side_nodes)
                for attempt in range(1, BLANK_RETRIES + 1):
                    if not any(blanks):
                        break
                    # a black image (NaNs, seen with Qwen when RAM ran out): the same seed usually renders fine
                    log(f"  seed {s} came out blank (black), rendering it again (ComfyUI may be short of memory)")
                    retry = None
                    try:  # best effort: if the re-render fails, keep the blank image and the rest of the run
                        scene_blank = any(is_blank(png) for _, png in side)
                        retry = comfy.queue(_uncached(wf, attempt, scene_blank), front=True)  # next, not behind
                        jobs[i - 1] = (s, retry, time.time(), wf)  # so a stop (Ctrl-C) cancels it
                        if _LIVE is not None:
                            _LIVE.forget(pid)  # the blank job no longer counts as a finished image
                        result = _finish_job(comfy, events, retry, side_nodes)
                    except Exception as e:
                        log(f"  the re-render failed ({type(e).__name__}: {e}); keeping the blank image")
                        jobs[i - 1] = (s, pid, queued_at, wf)
                        if retry is not None:
                            comfy.cancel([retry])
                        break
                    pid, queued_at = jobs[i - 1][1], jobs[i - 1][2]
                    entry, outs, datas, blanks, side = result
                if any(blanks):
                    log(f"warning: seed {s} is still blank; kept and marked \"blank\" (gen daily won't use it)")
                secs = round(time.time() - t_wait, 1)  # wall time; the first one includes loading the model
                try:  # best effort: a surprise in the events must not lose the finished images
                    jt = job_timing(wf, queued_at, events.wait_done(pid), entry)
                except Exception as e:
                    log(f"note: no timing for seed {s} ({type(e).__name__}: {e})")
                    jt = {}
                job_timings.append(jt)
                scene = None
                for j, (_, png) in enumerate(side):  # the scene before the cast went in (#52); the first one counts
                    f = run_dir / (f"{i:02d}-seed{s}-scene" + (f"-{j}" if len(side) > 1 else "") + ".png")
                    try:
                        f.write_bytes(png)
                        scene = scene or f
                    except OSError as e:
                        log(f"note: could not save {f.name}: {e}")
                for j, data in enumerate(datas):
                    name = f"{i:02d}-seed{s}" + (f"-{j}" if len(outs) > 1 else "") + ".png"
                    path = run_dir / name
                    path.write_bytes(data["png"])
                    timing = {**_flat(jt), "fetch_s": data["fetch_s"]}
                    flag = {"blank": True} if blanks[j] else {}
                    final = path
                    for step in post:
                        t0 = time.time()
                        final = POST[step](final)
                        timing[f"{step}_s"] = round(time.time() - t0, 2)
                    # this image's own time: ComfyUI's work (counted on a job's first image only) + fetch + post
                    own = [v for k, v in timing.items() if k not in ("queue_s", "comfy_s") + COMFY_STAGES]
                    timing["total_s"] = round(sum(own) + (jt.get("comfy_s", 0) if j == 0 else 0), 2)
                    meta = {"prompt": per_image[i - 1], "full_prompt": dress(per_image[i - 1]),
                            "negative": full_negative,
                            "user_negative": negative,
                            "preset": preset, "styles": styles, "seed": s, "params": {k: v for k, v in params.items()
                                                                    if k not in ("prompt", "negative")},
                            "file": final.name, "raw_file": path.name, "created": now.isoformat(timespec="seconds"),
                            "timing": {**timing, **{k: jt[k] for k in ("nodes", "cached") if k in jt}},
                            **flag, **(source or {}), **({"scene": scene.name} if scene else {})}
                    path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
                    images.append({"path": str(final), "windows_path": to_windows_path(final), "seed": s,
                                   "seconds": secs if j == 0 else None,  # per job, not per output (gen bakeoff)
                                   "timing": timing, **flag, **({"scene": str(scene)} if scene else {}),
                                   **({"prompt": per_image[i - 1]} if prompts else {})})
                parts = _stage_parts(images[-1]["timing"] if outs else jt)
                log(f"  [{i}/{count}] seed {s} done in {secs:.1f}s"
                    + (f" ({', '.join(parts)})" if parts else ""))
                t_wait = time.time()
                done = i
        except BaseException:
            comfy.cancel([j[1] for j in jobs[done:]])
            raise

        run_timing = {"total_s": round(time.time() - t_start, 2), "setup_s": round(t_queue - t_start, 2)}
        if job_timings and "queue_s" in job_timings[0]:
            run_timing["queue_s"] = job_timings[0]["queue_s"]  # waiting for other jobs ahead of this run
        comfy_keys = ("comfy_s",) + COMFY_STAGES
        for key in comfy_keys:  # once per job (a job may save more than one image)
            if any(key in jt for jt in job_timings):
                run_timing[key] = round(sum(jt.get(key, 0) for jt in job_timings), 2)
        for key in [k for im in images for k in im["timing"]]:  # fetch and post steps: once per image
            if key not in run_timing and key not in comfy_keys + ("queue_s", "total_s"):
                run_timing[key] = round(sum(im["timing"].get(key, 0) for im in images), 2)
        if not events.ok:
            run_timing["note"] = "no websocket: only queue and ComfyUI totals"
        summary = timing_summary(run_timing, [im["timing"] for im in images])
        run_timing["summary"] = summary
        run = {"preset": preset, "styles": styles, "prompt": prompt, "base_seed": base_seed, "count": count,
               "run_dir": str(run_dir), "images": images, "transparent": "remove_bg" in post, **(source or {}),
               "timing": run_timing}
        (run_dir / "run.json").write_text(json.dumps(run, indent=2))
        sheet = write_contact_sheet(run_dir, run)
        run["contact_sheet"] = str(sheet)
        run["contact_sheet_windows"] = to_windows_path(sheet)
        run["run_dir_windows"] = to_windows_path(run_dir)
        return run


def vary(path: str, count: int = 4, strength: float | None = None, prompt: str | None = None,
         negative: str | None = None, preset: str | None = None, mask: str | None = None,
         seed: int | None = None, steps: int | None = None, cfg: float | None = None,
         label: str | None = None, styles: list[str] | None = None) -> dict:
    """Variations of an existing image (img2img), or inpaint with a mask.

    Prompt, preset, negative and styles default to the image's JSON sidecar when it has one;
    an outside image (no sidecar) needs a prompt.
    """
    src = to_linux_path(path)
    if not src.is_file():
        raise FileNotFoundError(src)
    meta = find_sidecar(src) or {}
    if meta.get("edit"):  # an edit's prompt is an instruction and its preset is edit-only: not reusable here
        if prompt is None:
            raise ValueError(f"{src.name} came from gen edit, so its prompt is an instruction; pass a prompt")
        meta = {}
    if prompt is None:
        prompt = meta.get("prompt")
        if prompt is None:
            raise ValueError(f"no sidecar JSON next to {src.name}; pass a prompt")
    if negative is None:
        negative = meta.get("user_negative")
    if styles is None and meta:  # the image's own look; a sidecar from before #10 had none
        known = list_styles()
        styles = [n for n in meta.get("styles", []) if n in known]  # a style removed since: skip it
    if preset is None:
        preset = meta.get("preset") or "draft"
        key = "inpaint_workflow" if mask else "img2img_workflow"
        if mode_workflow(list_presets().get(preset, {}), key) is None:  # e.g. made with klein/qwen/kontext
            log(f"note: preset '{preset}' has no {key.split('_')[0]}, using draft (pick one with -p)")
            preset = "draft"
    return generate(prompt, preset, count, seed, negative, steps=steps, cfg=cfg,
                    label=label or ("inpaint-" if mask else "vary-") + slug(src.stem, 30),
                    source_image=str(src), mask=mask, denoise=strength, styles=styles)


EDIT_PRESET = "klein"  # picked by the #40 bake-off: fastest, followed instructions; -p kontext|qwen still work


def edit(path: str, instruction: str, count: int = 1, preset: str = EDIT_PRESET, seed: int | None = None,
         steps: int | None = None, guidance: float | None = None, label: str | None = None,
         styles: list[str] | None = None, refs: list[str] | None = None) -> dict:
    """Edit an image by instruction ("make it wave, keep the same character") with an edit preset
    (default FLUX.2 klein). Output is ~1 MP at the Kontext bucket closest to the source's aspect ratio.

    Each image is a fresh edit of the source with its own seed, not a chain of edits.
    refs (#52, -p wallpaper_cast): the source is image 1 (a scene), the refs images 2..: only the cast pass.
    """
    if not instruction.strip():
        raise ValueError("edit needs an instruction")
    stem = Path(path.replace("\\", "/")).stem
    return generate(instruction, preset, count, seed, steps=steps, guidance=guidance,
                    label=label or f"edit-{slug(stem, 20)}-{slug(instruction, 20)}",
                    source_image=path, edit=True, styles=styles, refs=refs)


SHEET_TEMPLATE = ROOT / "templates" / "contact_sheet.html"
SHEET_SOURCE = "source.png"  # an img2img/inpaint run's input as the model got it, shown on the sheet
SHEET_PARAMS = ("width", "height", "steps", "cfg", "guidance")  # shown per image when the sidecar has them


def write_contact_sheet(run_dir: Path, run: dict) -> Path:
    """Write <run_dir>/index.html: the viewer in templates/contact_sheet.html with this run's data inlined.

    Checkerboard background when run["transparent"]; older run.json files lack it, so then
    it is guessed from the file names (rembg output ends in -transparent.png).
    """
    images, first, scenes = [], None, []
    for im in run["images"]:
        path = Path(im["path"])
        meta = find_sidecar(run_dir / path.name) or {}
        first = first if first is not None else meta
        entry = {"file": path.name, "seed": im["seed"]}
        timing = im.get("timing") or meta.get("timing") or {}
        if "total_s" in timing:
            entry["time_s"] = timing["total_s"]
            entry["timing"] = ", ".join(_stage_parts(timing))
        entry.update({k: im[k] for k in ("tag", "seconds") if im.get(k) is not None})  # tag: gen bakeoff
        if im.get("scene") and (run_dir / Path(im["scene"]).name).is_file():  # a cast run's scene (#52)
            entry["scene"] = Path(im["scene"]).name
            if entry["scene"] not in [x["file"] for x in scenes]:  # a job with 2 outputs shares one scene
                scenes.append({"file": entry["scene"], "caption": f"scene · seed {im['seed']}"})
        params = meta.get("params") or {}
        entry.update({k: params[k] for k in SHEET_PARAMS if k in params})
        images.append(entry)
    transparent = run.get("transparent")
    if transparent is None:
        transparent = any(e["file"].endswith("-transparent.png") for e in images)
    data = {"prompt": run["prompt"], "preset": run["preset"], "base_seed": run["base_seed"],
            "count": run["count"], "transparent": transparent, "images": images,
            "negative": None if run.get("bakeoff") else (first or {}).get("negative"),  # bake-off: differs per model
            "source_image": Path(run["source_image"]).name if run.get("source_image") else None,
            "source_file": _sheet_source(run_dir, run.get("source_image")),
            "inputs": [{"file": r["file"], "caption": f"ref {k} · {Path(r['path']).name}"}
                       for k, r in enumerate(run.get("refs") or [], 1)
                       if r.get("file") and (run_dir / r["file"]).is_file()] + scenes,
            "cast_prompt": run.get("cast_prompt"),
            "mask": bool(run.get("mask")), "denoise": run.get("denoise"), "edit": bool(run.get("edit")),
            "run_dir": str(run_dir), "project": run.get("project"), "bakeoff": run.get("bakeoff"),
            "timing": (run.get("timing") or {}).get("summary")}
    # "<" escaped so no value can close the <script> block it is inlined into
    fills = {"TITLE": html.escape(run["prompt"][:60]),
             "RUN_JSON": json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")}
    page = re.sub(r"\{\{(TITLE|RUN_JSON)\}\}", lambda m: fills[m.group(1)], SHEET_TEMPLATE.read_text(encoding="utf-8"))
    out = run_dir / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


def _sheet_source(run_dir: Path, source: str | None) -> str | None:
    """The input image of an img2img/inpaint run, as the source.png that generate() saves next to the sheet.

    A copy rather than a link: file:// pages cannot reach WSL paths, and the original may change later.
    Runs made before generate() saved it get one made from the original now (same ~1 MP prep the model
    got; it may differ if the original changed since).
    """
    if not source:
        return None
    dest = run_dir / SHEET_SOURCE
    if not dest.is_file():
        try:
            dest.write_bytes(prepare_source(Path(source))[0])
        except Exception as e:  # moved, deleted, unreadable: the header still names the source
            log(f"note: no input image for the sheet ({type(e).__name__}: {e})")
            return None
    return dest.name


def find_run_files(path: str | None = None) -> list[Path]:
    """The run.json files a user means by `path`.

    None = the latest run. Otherwise a run folder, its run.json or index.html, or a date folder
    (all its runs); relative paths are also tried under output_dir, so `2026-10-31` works anywhere.
    """
    root = Path(load_config()["output_dir"]).expanduser()
    if path is None:
        runs = sorted(root.glob("*/*/run.json"), key=lambda f: f.stat().st_mtime)
        return runs[-1:]
    if re.match(r"^[A-Za-z]:[^\\/]", path):  # bash ate the backslashes of an unquoted C:\... path
        raise FileNotFoundError(f"{path} (put Windows paths in quotes: gen sheet 'C:\\...')")
    p = to_linux_path(path)
    # relative: as given, under output_dir, and as absolute (a dropped leading slash: mnt/c/...)
    for c in (p, root / p, Path("/") / p) if not p.is_absolute() else (p,):
        if c.name == "run.json" and c.is_file():
            return [c]
        if c.suffix and (c.parent / "run.json").is_file():  # index.html or an image
            return [c.parent / "run.json"]
        if (c / "run.json").is_file():
            return [c / "run.json"]
        if c.is_dir() and (runs := sorted(c.glob("*/run.json"))):  # a date folder
            return runs
    raise FileNotFoundError(f"no run.json in or under {p}")


def rebuild_contact_sheets(path: str | None = None) -> list[Path]:
    """Re-write index.html from run.json for every run find_run_files(path) finds; returns the sheets."""
    files = find_run_files(path)
    if not files:
        raise FileNotFoundError("no runs yet")
    return [write_contact_sheet(f.parent, json.loads(f.read_text())) for f in files]


def recent_runs(n: int = 5) -> list[dict]:
    root = Path(load_config()["output_dir"]).expanduser()
    runs = sorted(root.glob("*/*/run.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    out = []
    for f in runs:
        r = json.loads(f.read_text())
        out.append({"run_dir": r["run_dir"], "preset": r["preset"], "styles": r.get("styles", []),
                    "prompt": r["prompt"],
                    "images": [im["path"] for im in r["images"]]})
    return out


def set_wallpaper(path: str) -> str:
    p = to_linux_path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    win = to_windows_path(p)
    if not win:
        raise RuntimeError("wslpath failed (are we running inside WSL?)")
    if win.startswith("\\\\"):
        # Windows can't reliably use \\wsl$ paths as wallpaper
        raise RuntimeError("wallpaper must live on a Windows drive (set output_dir under /mnt/c)")
    ps = ("Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class W { "
          "[DllImport(\"user32.dll\", CharSet=CharSet.Unicode)] public static extern int "
          "SystemParametersInfo(int a, int b, string c, int d); }'; "
          f"[void][W]::SystemParametersInfo(20, 0, '{win.replace(chr(39), chr(39) * 2)}', 3)")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                   check=True, capture_output=True)
    return win


def open_in_windows(path: str) -> None:
    """A file (or folder) in its Windows app, or an http(s) URL in the default browser."""
    win = path if path.startswith(("http://", "https://")) else to_windows_path(path)
    if win and shutil.which("explorer.exe"):
        subprocess.run(["explorer.exe", win])  # explorer returns 1 even on success


# ---------------------------------------------------------------- CLI

def _print_run(run: dict, open_sheet: bool) -> None:
    for im in run["images"]:
        print(im["windows_path"] or im["path"])
    print("contact sheet:", run["contact_sheet_windows"] or run["contact_sheet"])
    if summary := (run.get("timing") or {}).get("summary"):
        print("timing:", summary)
    if open_sheet:
        open_in_windows(run["contact_sheet"])


def _style_arg(names: list[str] | None) -> list[str] | None:
    """--style values: None (not given) = the default, 'none' = no style; commas split too."""
    if names is None:
        return None
    names = split_styles(names)
    return [] if names in ([], ["none"]) else names


def _add_style_arg(parser, help: str = "a [styles.NAME] look from config.toml (repeatable or a, b; replaces "
                   "the preset's default styles; 'none' = no style). gen styles lists them") -> None:
    parser.add_argument("--style", action="append", dest="styles", metavar="NAME", help=help)


def main(argv: list[str]) -> int:
    import argparse
    if argv and argv[0] == "presets":
        for name, p in list_presets().items():
            look = f" [style: {', '.join(p['styles'])}]" if p.get("styles") else ""
            print(f"{name:22} {'(gen edit only) ' if 'workflow' not in p else ''}{p.get('description', '')}{look}")
        return 0
    if argv and argv[0] == "styles":
        conf = load_config()
        if not list_styles(conf):
            print("no styles: add [styles.NAME] tables to config.toml")
        for name, st in list_styles(conf).items():
            users = [n for n, p in conf["presets"].items() if name in p.get("styles", [])]
            print(f"{name:14} {st.get('description', '')}" + (f" (default in {', '.join(users)})" if users else ""))
            print(f"{'':14} + {style_text([name], conf)}")
        return 0
    if argv and argv[0] == "recent":
        for r in recent_runs(int(argv[1]) if len(argv) > 1 else 5):
            label = "+".join([r["preset"], *r["styles"]])
            print(f"{label:24} {r['prompt'][:50]:50} {r['run_dir']}")
        return 0
    if argv and argv[0] == "project":
        import projects
        return projects.main(argv[1:])
    if argv and argv[0] == "board":
        import boardserve
        return boardserve.main(argv[1:])
    if argv and argv[0] in ("char", "character"):
        import characters
        return characters.main(argv[1:])
    if argv and argv[0] == "daily":
        import daily
        return daily.main(argv[1:])
    if argv and argv[0] == "doctor":
        import doctor
        return doctor.main()
    if argv and argv[0] == "vary":
        vp = argparse.ArgumentParser(prog="gen vary",
                                     description="img2img variations of an image, or inpaint with --mask")
        vp.add_argument("path")
        vp.add_argument("-n", "--count", type=int, default=4)
        vp.add_argument("-s", "--strength", type=float,
                        help="how much may change, 0-1 (default 0.4; 0.85 with --mask)")
        vp.add_argument("--prompt", help="default: the image's own prompt from its sidecar")
        vp.add_argument("--neg", dest="negative")
        vp.add_argument("-p", "--preset", help="default: the image's own preset, else draft")
        vp.add_argument("--mask", help="PNG, white = repaint, black = keep (any size)")
        vp.add_argument("--seed", type=int)
        vp.add_argument("--steps", type=int)
        vp.add_argument("--cfg", type=float)
        vp.add_argument("--label", help="folder name instead of vary-<image>")
        _add_style_arg(vp, "default: the image's own styles (its sidecar); 'none' = no style; gen styles lists them")
        vp.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
        a = vp.parse_args(argv[1:])
        run = vary(a.path, a.count, a.strength, a.prompt, a.negative, a.preset, a.mask,
                   a.seed, a.steps, a.cfg, a.label, _style_arg(a.styles))
        _print_run(run, a.open)
        return 0
    if argv and argv[0] == "edit":
        ep = argparse.ArgumentParser(
            prog="gen edit", description="Edit an image by instruction (FLUX.2 klein by default; "
            "-p kontext / -p qwen for FLUX.1 Kontext / Qwen-Image-2.1). "
            "Say what to change and what to keep, e.g. \"Make the character wave with one tentacle, "
            "seen from the side. Keep the same character design, colors and art style.\"")
        ep.add_argument("path")
        ep.add_argument("instruction")
        ep.add_argument("-n", "--count", type=int, default=1, help="edits to make, one seed each (default 1)")
        ep.add_argument("-p", "--preset", default=EDIT_PRESET)
        ep.add_argument("--seed", type=int)
        ep.add_argument("--steps", type=int, help="default from the preset (kontext 20, klein 4, qwen 25)")
        ep.add_argument("-g", "--guidance", type=float, help="kontext only: how strongly to follow the text (2.5)")
        ep.add_argument("--label", help="folder name instead of edit-<image>-<instruction>")
        _add_style_arg(ep)
        ep.add_argument("--ref", action="append", dest="refs", metavar="IMAGE",
                        help="-p wallpaper_cast: put the character from this ref into the scene PATH (repeatable; "
                             "PATH = image 1, refs = image 2, 3..). Re-runs only the cast pass of a daily")
        ep.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
        a = ep.parse_args(argv[1:])
        run = edit(a.path, a.instruction, a.count, a.preset, a.seed, a.steps, a.guidance, a.label,
                   _style_arg(a.styles), a.refs)
        _print_run(run, a.open)
        return 0
    if argv and argv[0] == "bakeoff":
        import bakeoff  # lazy: only gen bakeoff needs it
        return bakeoff.main(argv[1:])
    if argv and argv[0] == "cutout":
        cp = argparse.ArgumentParser(prog="gen cutout",
                                     description="Remove the background -> PATH-transparent.png")
        cp.add_argument("path")
        cp.add_argument("--clean", action="store_true",
                        help="also drop stray shapes and centre the result on a square (like the icon preset)")
        a = cp.parse_args(argv[1:])
        src = to_linux_path(a.path)
        if not src.is_file():
            print(f"not a file: {src}", file=sys.stderr)
            return 1
        print(cutout(src, a.clean))
        return 0
    if argv and argv[0] == "crt":
        cp = argparse.ArgumentParser(
            prog="gen crt", description="Add the CRT/VHS look to images -> NAME-crt.png (the clean image stays). "
            "Knobs default to [crt] in config.toml; try some here, then put the ones you like there. "
            "On a -crt.png it starts again from the clean image, so it never stacks.")
        cp.add_argument("paths", nargs="+", metavar="PATH")
        for k, v in CRT_DEFAULTS.items():
            cp.add_argument(f"--{k}", type=float, help=f"default {v} (or [crt] {k})")
        a = cp.parse_args(argv[1:])
        knobs = {k: getattr(a, k) for k in CRT_DEFAULTS}
        status = 0
        for p in a.paths:
            src = to_linux_path(p)
            if not src.is_file():
                print(f"not a file: {src}", file=sys.stderr)
                status = 1
                continue
            out = crt(src, **knobs)
            print(to_windows_path(out) or out)
        return status
    if argv and argv[0] == "sheet":
        if argv[1:2] in (["-h"], ["--help"]):
            print("usage: gen sheet [PATH...]  rebuild index.html from run.json\n"
                  "  PATH: a run folder (or its index.html), or a date folder for all its runs;\n"
                  "  relative paths are looked up under output_dir. No PATH = the latest run.")
            return 0
        status = 0
        for d in argv[1:] or [None]:
            try:
                for out in rebuild_contact_sheets(d):
                    print(to_windows_path(out) or out)
            except (FileNotFoundError, KeyError, ValueError) as e:  # ValueError covers bad JSON
                print(f"{d or 'latest run'}: cannot rebuild ({type(e).__name__}: {e})", file=sys.stderr)
                status = 1
        return status
    if argv and argv[0] == "set-wallpaper":
        print("wallpaper set:", set_wallpaper(argv[1]))
        return 0

    ap = argparse.ArgumentParser(prog="gen", description="Generate images via ComfyUI presets")
    ap.add_argument("prompt")
    ap.add_argument("-p", "--preset", default="draft")
    ap.add_argument("-n", "--count", type=int, default=1)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--neg", dest="negative")
    ap.add_argument("--size", help="WxH generation size, e.g. 832x1216")
    ap.add_argument("--steps", type=int)
    ap.add_argument("--cfg", type=float)
    ap.add_argument("--label", help="folder name instead of prompt slug")
    _add_style_arg(ap)
    ap.add_argument("--ref", action="append", dest="refs", metavar="IMAGE",
                    help="cast presets (wallpaper_cast): a character's ref image to put into the scene (repeatable)")
    ap.add_argument("--cast-prompt", help="cast presets: the edit that puts the refs in (\"image 1\" = the scene, "
                    "\"image 2\".. = the refs); default: the preset's cast_prompt")
    ap.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
    ap.add_argument("--wallpaper", action="store_true", help="set the first result as wallpaper")
    a = ap.parse_args(argv)
    w = h = None
    if a.size:
        w, h = (int(x) for x in a.size.lower().split("x"))
    run = generate(a.prompt, a.preset, a.count, a.seed, a.negative, w, h, a.steps, a.cfg, a.label,
                   styles=_style_arg(a.styles), refs=a.refs, cast_prompt=a.cast_prompt)
    _print_run(run, a.open)
    if a.wallpaper and run["images"]:
        good = [im for im in run["images"] if not im.get("blank")]
        if not good:
            log("not setting the wallpaper: every image came out blank (black)")
            return 1
        print("wallpaper set:", set_wallpaper(good[0]["path"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (RuntimeError, ValueError, KeyError, FileNotFoundError, TimeoutError) as e:
        log(f"error: {e}")
        sys.exit(1)
