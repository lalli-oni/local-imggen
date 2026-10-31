#!/usr/bin/env python3
"""imagegen: run ComfyUI presets from the command line (and from mcp_server.py).

Usage:
  gen "a cozy cabin at dusk"                    # draft preset, 1 image
  gen "misty fjord" -p wallpaper -n 4           # 4 wallpapers (seeds base..base+3)
  gen "rocket" -p icon -n 6 --open              # icons, open contact sheet in browser
  gen "x" --seed 123 --size 832x1216 --steps 40 --neg "people"
  gen presets                                   # list presets
  gen recent [N]                                # last N runs
  gen set-wallpaper PATH                        # set Windows desktop wallpaper
"""
from __future__ import annotations

import base64
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
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


def log(msg: str) -> None:
    # stderr only: stdout is the MCP protocol channel when used by mcp_server.py
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    if v := os.environ.get("IMAGEGEN_OUTPUT_DIR"):
        cfg["output_dir"] = v
    if v := os.environ.get("IMAGEGEN_COMFY_URL"):
        cfg["comfy_url"] = v
    return cfg


def list_presets() -> dict[str, dict]:
    return load_config()["presets"]


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

    def queue(self, workflow: dict) -> str:
        body = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode()
        req = urllib.request.Request(self.url + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())["prompt_id"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI rejected the workflow: {e.read().decode(errors='replace')[:2000]}")

    def wait(self, prompt_id: str, timeout: float = 1800) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            hist = json.loads(self._get(f"/history/{prompt_id}"))
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI failed: {json.dumps(status.get('messages'))[:2000]}")
                return entry.get("outputs", {})
            time.sleep(1)
        raise TimeoutError(f"prompt {prompt_id} did not finish in {timeout}s")

    def fetch(self, image: dict) -> bytes:
        q = urllib.parse.urlencode({k: image.get(k, "") for k in ("filename", "subfolder", "type")})
        return self._get(f"/view?{q}", timeout=120)


# ---------------------------------------------------------------- post-processing

def remove_bg(path: Path) -> Path:
    from rembg import remove  # lazy: heavy import, only icons need it
    out = path.with_name(path.stem + "-transparent.png")
    out.write_bytes(remove(path.read_bytes()))
    return out


POST = {"remove_bg": remove_bg}


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

def generate(prompt: str, preset: str = "draft", count: int = 1, seed: int | None = None,
             negative: str | None = None, width: int | None = None, height: int | None = None,
             steps: int | None = None, cfg: float | None = None, label: str | None = None) -> dict:
    conf = load_config()
    presets = conf["presets"]
    if preset not in presets:
        raise ValueError(f"unknown preset '{preset}'. Available: {', '.join(presets)}")
    p = dict(presets[preset])
    count = max(1, int(count))

    template = json.loads((ROOT / "workflows" / p["workflow"]).read_text())
    base_seed = seed if seed is not None else random.randrange(MAX_SEED)
    full_prompt = prompt + p.get("suffix", "")
    full_negative = (negative if negative is not None else conf.get("default_negative", "")) \
        + p.get("negative_extra", "")

    params = {k: v for k, v in p.items() if k not in ("workflow", "description", "suffix",
                                                      "negative_extra", "post")}
    params.update(checkpoint=p.get("checkpoint", conf["checkpoint"]),
                  prompt=full_prompt, negative=full_negative)
    for k, v in (("width", width), ("height", height), ("steps", steps), ("cfg", cfg)):
        if v is not None:
            params[k] = v

    now = datetime.now()
    run_dir = Path(conf["output_dir"]).expanduser() / now.strftime("%Y-%m-%d") / \
        f"{now:%H%M%S}-{preset}-{slug(label or prompt)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    comfy = Comfy(conf["comfy_url"])
    comfy.wait_ready()

    # queue everything up front; ComfyUI works through it while we wait
    jobs = []
    for i in range(count):
        s = (base_seed + i) % MAX_SEED
        jobs.append((s, comfy.queue(fill(template, {**params, "seed": s}))))
    log(f"queued {count} x {preset} (seeds {base_seed}..) -> {run_dir}")

    images = []
    for i, (s, pid) in enumerate(jobs, 1):
        t0 = time.time()
        outputs = comfy.wait(pid)
        outs = [im for o in outputs.values() for im in o.get("images", []) if im.get("type") == "output"]
        for j, im in enumerate(outs):
            name = f"{i:02d}-seed{s}" + (f"-{j}" if len(outs) > 1 else "") + ".png"
            path = run_dir / name
            path.write_bytes(comfy.fetch(im))
            final = path
            for step in p.get("post", []):
                final = POST[step](final)
            meta = {"prompt": prompt, "full_prompt": full_prompt, "negative": full_negative,
                    "preset": preset, "seed": s, "params": {k: v for k, v in params.items()
                                                            if k not in ("prompt", "negative")},
                    "file": final.name, "raw_file": path.name, "created": now.isoformat(timespec="seconds")}
            path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
            images.append({"path": str(final), "windows_path": to_windows_path(final), "seed": s})
        log(f"  [{i}/{count}] seed {s} done in {time.time() - t0:.1f}s")

    run = {"preset": preset, "prompt": prompt, "base_seed": base_seed, "count": count,
           "run_dir": str(run_dir), "images": images}
    (run_dir / "run.json").write_text(json.dumps(run, indent=2))
    sheet = write_contact_sheet(run_dir, run, transparent=bool(p.get("post")))
    run["contact_sheet"] = str(sheet)
    run["contact_sheet_windows"] = to_windows_path(sheet)
    run["run_dir_windows"] = to_windows_path(run_dir)
    return run


def write_contact_sheet(run_dir: Path, run: dict, transparent: bool = False) -> Path:
    bg = ("background: repeating-conic-gradient(#3a3a3a 0 25%, #2a2a2a 0 50%) 0 0/20px 20px;"
          if transparent else "background:#111;")
    cards = "\n".join(
        f'<figure><a href="{html.escape(Path(im["path"]).name)}"><img src="{html.escape(Path(im["path"]).name)}" '
        f'loading="lazy"></a><figcaption>#{n} · seed {im["seed"]}</figcaption></figure>'
        for n, im in enumerate(run["images"], 1))
    page = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(run['prompt'][:60])}</title>
<style>
body{{margin:0;padding:24px;background:#0b0b0c;color:#ddd;font:14px system-ui,sans-serif}}
h1{{font-size:16px;font-weight:600;margin:0 0 4px}} p{{margin:0 0 20px;color:#888}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}}
figure{{margin:0}} img{{width:100%;display:block;border-radius:8px;{bg}}}
figcaption{{padding:6px 2px;color:#999;font-size:12px}}
</style>
<h1>{html.escape(run['prompt'])}</h1>
<p>preset <b>{html.escape(run['preset'])}</b> · {run['count']} image(s) · base seed {run['base_seed']}</p>
<main>{cards}</main>"""
    out = run_dir / "index.html"
    out.write_text(page)
    return out


def recent_runs(n: int = 5) -> list[dict]:
    root = Path(load_config()["output_dir"]).expanduser()
    runs = sorted(root.glob("*/*/run.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    out = []
    for f in runs:
        r = json.loads(f.read_text())
        out.append({"run_dir": r["run_dir"], "preset": r["preset"], "prompt": r["prompt"],
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
    win = to_windows_path(path)
    if win and shutil.which("explorer.exe"):
        subprocess.run(["explorer.exe", win])  # explorer returns 1 even on success


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    import argparse
    if argv and argv[0] == "presets":
        for name, p in list_presets().items():
            print(f"{name:22} {p.get('description', '')}")
        return 0
    if argv and argv[0] == "recent":
        for r in recent_runs(int(argv[1]) if len(argv) > 1 else 5):
            print(f"{r['preset']:12} {r['prompt'][:50]:50} {r['run_dir']}")
        return 0
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
    ap.add_argument("--open", action="store_true", help="open contact sheet in Windows browser")
    ap.add_argument("--wallpaper", action="store_true", help="set the first result as wallpaper")
    a = ap.parse_args(argv)
    w = h = None
    if a.size:
        w, h = (int(x) for x in a.size.lower().split("x"))
    run = generate(a.prompt, a.preset, a.count, a.seed, a.negative, w, h, a.steps, a.cfg, a.label)
    for im in run["images"]:
        print(im["windows_path"] or im["path"])
    print("contact sheet:", run["contact_sheet_windows"] or run["contact_sheet"])
    if a.open:
        open_in_windows(run["contact_sheet"])
    if a.wallpaper and run["images"]:
        print("wallpaper set:", set_wallpaper(run["images"][0]["path"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (RuntimeError, ValueError, KeyError, FileNotFoundError, TimeoutError) as e:
        log(f"error: {e}")
        sys.exit(1)
