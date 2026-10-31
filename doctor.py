"""gen doctor: read-only health check for the whole pipeline.

Each check returns {"name", "status": ok|warn|fail|info, "detail", "fix"?}. Nothing here installs,
starts, restarts or writes anything (except a temp file in output_dir, deleted right away).
`gen doctor` exits 1 if any check failed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import imagegen

COMFY_DIR = Path.home() / "comfyui"
COMFY_VENV_PY = Path.home() / "venvs" / "comfy" / "bin" / "python"
EXPECTED_TORCH = "2.9.1+rocm7.2.0"
LIMITS_DROPIN = Path("/etc/systemd/system/comfyui.service.d/zz-imagegen-limits.conf")
# Keys generate() drops from a preset before filling the workflow, plus the ones it always adds.
NOT_PARAMS = {"workflow", "description", "suffix", "negative_extra", "post"}
STANDARD_PARAMS = {"seed", "prompt", "negative", "checkpoint"}
# Which ComfyUI model folder each model-like preset key lives in.
MODEL_KEYS = {"checkpoint": "checkpoints", "upscale_model": "upscale_models"}


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    c = {"name": name, "status": status, "detail": detail}
    if fix and status in ("warn", "fail"):
        c["fix"] = fix
    return c


def _get_json(url: str, timeout: float = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- checks

def check_service() -> dict:
    if not shutil.which("systemctl"):
        return _check("comfyui service", "info", "systemctl not found, skipped")
    r = subprocess.run(["systemctl", "is-active", "comfyui"], capture_output=True, text=True)
    state = (r.stdout.strip() or r.stderr.strip() or "unknown").splitlines()[0]
    if state == "active":
        return _check("comfyui service", "ok", "active")
    return _check("comfyui service", "fail", state,
                  "sudo systemctl start comfyui; logs: journalctl -u comfyui -n 50")


def check_comfy_api(url: str) -> tuple[dict, dict | None]:
    try:
        stats = _get_json(url.rstrip("/") + "/system_stats")
    except Exception as e:
        return _check("ComfyUI API", "fail", f"{url} not reachable ({e})",
                      "systemctl status comfyui; journalctl -u comfyui -n 50"), None
    devs = stats.get("devices") or []
    gpus = [d for d in devs if d.get("type") != "cpu"]
    if not gpus:
        return _check("ComfyUI API", "fail", f"{url} is up but reports no GPU (running on CPU)",
                      "check the ROCm torch in ~/venvs/comfy and `journalctl -u comfyui -n 50`"), stats
    g = gpus[0]
    gib = 1024 ** 3
    vram = f"{g.get('vram_free', 0) / gib:.1f}/{g.get('vram_total', 0) / gib:.1f} GiB free"
    return _check("ComfyUI API", "ok", f"{url} up, GPU: {g.get('name', '?')}, {vram}"), stats


def check_torch() -> dict:
    """Read package versions from ~/venvs/comfy without importing torch (fast, touches no GPU)."""
    if not COMFY_VENV_PY.exists():
        return _check("ROCm torch", "fail", f"{COMFY_VENV_PY} not found",
                      "the ComfyUI venv is missing; see the project setup notes")
    code = ("import json, importlib.metadata as m\n"
            "out = {}\n"
            "for p in ('torch', 'torchvision', 'torchaudio'):\n"
            "    try: out[p] = m.version(p)\n"
            "    except m.PackageNotFoundError: out[p] = None\n"
            "print(json.dumps(out))")
    try:
        r = subprocess.run([str(COMFY_VENV_PY), "-I", "-c", code], capture_output=True, text=True,
                           timeout=20)
        versions = json.loads(r.stdout)
    except Exception as e:
        return _check("ROCm torch", "fail", f"could not read versions from {COMFY_VENV_PY} ({e})")
    torch = versions.get("torch")
    if not torch:
        return _check("ROCm torch", "fail", "torch is not installed in ~/venvs/comfy",
                      "reinstall AMD's ROCm torch wheels (never a generic `pip install torch`)")
    generic = [f"{p} {v}" for p, v in versions.items() if v and "rocm" not in v]
    detail = ", ".join(f"{p} {v}" for p, v in versions.items() if v)
    if generic:
        return _check("ROCm torch", "fail", f"not ROCm builds: {', '.join(generic)}",
                      "a generic wheel replaced AMD's; reinstall the ROCm wheels for those packages "
                      "and filter torch/torchvision/torchaudio/triton out of future pip installs")
    if torch != EXPECTED_TORCH:
        return _check("ROCm torch", "warn", f"{detail} (expected torch {EXPECTED_TORCH})",
                      f"fine if you upgraded on purpose; then update EXPECTED_TORCH in doctor.py")
    return _check("ROCm torch", "ok", detail)


def _model_names(url: str, folder: str, api_up: bool) -> tuple[set[str], str]:
    """Model files ComfyUI sees in a folder (its API includes extra_model_paths), else the disk."""
    if api_up:
        try:
            return set(_get_json(f"{url.rstrip('/')}/models/{folder}")), "ComfyUI"
        except Exception:
            pass
    d = COMFY_DIR / "models" / folder
    names = {str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()} if d.is_dir() else set()
    return names, str(d)


def check_models(conf: dict, api_up: bool) -> list[dict]:
    url = conf["comfy_url"]
    wanted: dict[str, set[str]] = {}
    for p in conf["presets"].values():
        for key, folder in MODEL_KEYS.items():
            name = p.get(key, conf.get("checkpoint") if key == "checkpoint" else None)
            if name:
                wanted.setdefault(folder, set()).add(name)
    out = []
    for folder, names in sorted(wanted.items()):
        have, source = _model_names(url, folder, api_up)
        for name in sorted(names):
            if name in have:
                out.append(_check(f"model {name}", "ok", f"found in {folder} ({source})"))
            else:
                out.append(_check(f"model {name}", "fail", f"missing from {folder} ({source})",
                                  f"put it in {COMFY_DIR / 'models' / folder}/ (setup.sh fetches the upscaler)"))
    if any("remove_bg" in p.get("post", []) for p in conf["presets"].values()):
        u2 = Path(os.environ.get("U2NET_HOME", Path.home() / ".u2net")).expanduser() / "u2net.onnx"
        if u2.is_file():
            out.append(_check("model rembg u2net", "ok", str(u2)))
        else:
            out.append(_check("model rembg u2net", "warn",
                              f"{u2} missing; the first icon run will download it (~170 MB)",
                              "re-run ~/imagegen/setup.sh to fetch it now"))
    return out


def check_workflows(conf: dict) -> list[dict]:
    out = []
    for name, p in conf["presets"].items():
        wf = imagegen.ROOT / "workflows" / p.get("workflow", "")
        try:
            text = wf.read_text()
            json.loads(text)
        except Exception as e:
            out.append(_check(f"preset {name}", "fail", f"workflow {wf.name}: {e}",
                              "fix the preset's workflow = ... or the JSON file"))
            continue
        keys = set(imagegen._PLACEHOLDER.findall(text))
        have = (set(p) - NOT_PARAMS) | STANDARD_PARAMS
        missing = sorted(keys - have)
        bad_post = [s for s in p.get("post", []) if s not in imagegen.POST]
        if missing or bad_post:
            problems = ([f"no value for {', '.join('{{' + k + '}}' for k in missing)}"] if missing else []) \
                + ([f"unknown post step(s): {', '.join(bad_post)}"] if bad_post else [])
            out.append(_check(f"preset {name}", "fail", f"{wf.name}: {'; '.join(problems)}",
                              f"add the key(s) to [presets.{name}] in config.toml"))
        else:
            out.append(_check(f"preset {name}", "ok", f"{wf.name}, all placeholders filled"))
    return out


def check_output_dir(conf: dict) -> dict:
    d = Path(conf["output_dir"]).expanduser()
    if not d.exists():
        parent = next((a for a in d.parents if a.exists()), None)
        if parent and os.access(parent, os.W_OK):
            return _check("output_dir", "ok", f"{d} (created on the first run)")
        return _check("output_dir", "fail", f"{d} does not exist and can't be created",
                      "fix output_dir in config.toml, or mkdir -p it")
    try:
        with tempfile.NamedTemporaryFile(dir=d, prefix=".doctor-"):
            pass
    except Exception as e:
        return _check("output_dir", "fail", f"{d} not writable ({e})", "fix permissions or output_dir")
    return _check("output_dir", "ok", f"{d} writable")


def check_limits() -> dict:
    if LIMITS_DROPIN.is_file():
        return _check("resource limits", "info", "ON (comfy/limits.sh status for details)")
    return _check("resource limits", "info", "off (opt-in: comfy/limits.sh on)")


def check_mcp_import() -> dict:
    try:
        r = subprocess.run([sys.executable, "-c", "import mcp_server"], cwd=imagegen.ROOT,
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return _check("MCP server", "fail", "importing mcp_server.py took over 30s")
    if r.returncode == 0:
        return _check("MCP server", "ok", "mcp_server.py imports cleanly")
    last = (r.stderr.strip().splitlines() or ["?"])[-1]
    return _check("MCP server", "fail", last,
                  f"run: {sys.executable} -c 'import mcp_server' in {imagegen.ROOT} for the full error")


# ---------------------------------------------------------------- entry points

def run_checks(include_mcp: bool = True) -> list[dict]:
    try:
        conf = imagegen.load_config()
    except Exception as e:
        return [_check("config.toml", "fail", str(e), "fix the TOML syntax in config.toml")]
    checks = [_check("config.toml", "ok", f"{len(conf['presets'])} presets")]
    checks.append(check_service())
    api, stats = check_comfy_api(conf["comfy_url"])
    checks.append(api)
    checks.append(check_torch())
    checks += check_models(conf, api_up=stats is not None)
    checks += check_workflows(conf)
    checks.append(check_output_dir(conf))
    if include_mcp:
        checks.append(check_mcp_import())
    checks.append(check_limits())
    return checks


ICONS = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}


def main() -> int:
    checks = run_checks()
    for c in checks:
        print(f"{ICONS[c['status']]} {c['name']}: {c['detail']}")
        if "fix" in c:
            print(f"    fix: {c['fix']}")
    fails = sum(c["status"] == "fail" for c in checks)
    warns = sum(c["status"] == "warn" for c in checks)
    print(f"\n{'all good' if not fails else f'{fails} failed'}" + (f", {warns} warning(s)" if warns else ""))
    return 1 if fails else 0
