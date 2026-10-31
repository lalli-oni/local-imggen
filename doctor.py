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
NOT_PARAMS = imagegen.NOT_PARAMS
STANDARD_PARAMS = {"seed", "prompt", "negative", "checkpoint"}
# Each mode a preset can run in: (preset key, default workflow, extra params generate() adds).
MODES = [("workflow", None, set()),
         ("img2img_workflow", "sdxl_img2img.json", {"image", "denoise"}),
         ("inpaint_workflow", "sdxl_inpaint.json", {"image", "denoise", "mask"})]
# Edit-only presets (edit_workflow, no workflow) run in this mode instead.
EDIT_MODE = ("edit_workflow", None, {"image", "width", "height"})
# Which ComfyUI model folder each model-like preset key lives in.
MODEL_KEYS = {"checkpoint": "checkpoints", "upscale_model": "upscale_models", "unet": "diffusion_models",
              "clip_l": "text_encoders", "t5": "text_encoders", "vae": "vae"}
OPT_IN_MODELS = "~/imagegen/models/flux-kontext.sh"


def _modes(p: dict) -> list[tuple[str, str | None, set[str]]]:
    """The modes a preset runs in: text/img2img/inpaint if it has a workflow, edit if it has an edit_workflow."""
    return (MODES if "workflow" in p else []) + ([EDIT_MODE] if "edit_workflow" in p else [])


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


def _placeholders(p: dict, edit: bool) -> set[str]:
    """{{keys}} used by the preset's edit workflow (edit=True) or by its other workflows."""
    keys: set[str] = set()
    for key, default, _ in _modes(p):
        if (key == EDIT_MODE[0]) == edit:
            try:
                keys |= set(imagegen._PLACEHOLDER.findall((imagegen.ROOT / "workflows" / p.get(key, default)).read_text()))
            except OSError:
                pass  # check_workflows reports it
    return keys


def check_models(conf: dict, api_up: bool) -> list[dict]:
    url = conf["comfy_url"]
    # folder -> model -> required. Models only an edit workflow uses are opt-in downloads: missing = warn.
    wanted: dict[str, dict[str, bool]] = {}
    for p in conf["presets"].values():
        edit_only_keys = _placeholders(p, edit=True) - _placeholders(p, edit=False)
        for key, folder in MODEL_KEYS.items():
            required = "workflow" in p and key not in edit_only_keys
            name = p.get(key, conf.get("checkpoint") if key == "checkpoint" and "workflow" in p else None)
            if name:
                names = wanted.setdefault(folder, {})
                names[name] = names.get(name, False) or required
    out = []
    for folder, names in sorted(wanted.items()):
        have, source = _model_names(url, folder, api_up)
        for name, required in sorted(names.items()):
            if name in have:
                out.append(_check(f"model {name}", "ok", f"found in {folder} ({source})"))
            elif required:
                out.append(_check(f"model {name}", "fail", f"missing from {folder} ({source})",
                                  f"put it in {COMFY_DIR / 'models' / folder}/ (setup.sh fetches the upscaler)"))
            else:
                out.append(_check(f"model {name}", "warn", f"missing from {folder} ({source}); "
                                  "only gen edit needs it", f"opt-in download: {OPT_IN_MODELS}"))
    # rembg: used by the icon preset's remove_bg step and by `gen cutout`
    u2 = Path(os.environ.get("U2NET_HOME", Path.home() / ".u2net")).expanduser() / "u2net.onnx"
    if u2.is_file():
        out.append(_check("model rembg u2net", "ok", str(u2)))
    else:
        out.append(_check("model rembg u2net", "warn",
                          f"{u2} missing; the first background removal will download it (~170 MB)",
                          "re-run ~/imagegen/setup.sh to fetch it now"))
    return out


def check_workflows(conf: dict) -> list[dict]:
    """Every {{key}} in each workflow a preset can use (text, img2img, inpaint, edit) must get a value."""
    out = []
    for name, p in conf["presets"].items():
        have = (set(p) - NOT_PARAMS) | STANDARD_PARAMS
        problems, used = [], []
        if not _modes(p):
            problems.append("no workflow or edit_workflow set")
        for key, default, extra in _modes(p):
            wf_name = p.get(key, default)
            # generate() adds checkpoint only outside edit mode (unless the preset sets it)
            mode_have = have - {"checkpoint"} if key == EDIT_MODE[0] and "checkpoint" not in p else have
            if not wf_name:
                problems.append(f"no {key} set")
                continue
            wf = imagegen.ROOT / "workflows" / wf_name
            try:
                text = wf.read_text()
                json.loads(text)
            except Exception as e:
                problems.append(f"{wf_name}: {e}")
                continue
            used.append(wf_name)
            missing = sorted(set(imagegen._PLACEHOLDER.findall(text)) - mode_have - extra)
            if missing:
                problems.append(f"{wf_name}: no value for {', '.join('{{' + k + '}}' for k in missing)}")
        bad_post = [s for s in p.get("post", []) if s not in imagegen.POST]
        if bad_post:
            problems.append(f"unknown post step(s): {', '.join(bad_post)}")
        if problems:
            out.append(_check(f"preset {name}", "fail", "; ".join(problems),
                              f"fix [presets.{name}] in config.toml or the workflow file"))
        else:
            out.append(_check(f"preset {name}", "ok", f"{', '.join(used)}: all placeholders filled"))
    return out


def check_nodes(conf: dict, url: str) -> dict:
    """Every node type the presets' workflows use must exist in this ComfyUI (Kontext needs a mid-2025+ one).

    Missing nodes that only edit workflows use are a warning: gen edit is opt-in.
    """
    classes: dict[str, set[str]] = {}  # class_type -> workflows using it
    required: set[str] = set()  # class_types some non-edit workflow uses
    for p in conf["presets"].values():
        for key, default, _ in _modes(p):
            wf_name = p.get(key, default)
            try:
                wf = json.loads((imagegen.ROOT / "workflows" / wf_name).read_text())
            except Exception:
                continue  # check_workflows reports it
            for node in wf.values():
                if isinstance(node, dict) and "class_type" in node:
                    classes.setdefault(node["class_type"], set()).add(wf_name)
                    if key != EDIT_MODE[0]:
                        required.add(node["class_type"])
    try:
        available = _get_json(f"{url.rstrip('/')}/object_info", timeout=30)
        missing = {c: wfs for c, wfs in classes.items() if c not in available}
    except Exception as e:
        return _check("ComfyUI nodes", "warn", f"could not list nodes ({e})")
    if not missing:
        return _check("ComfyUI nodes", "ok", f"all {len(classes)} node types the workflows use are available")
    detail = "; ".join(f"{c} (in {', '.join(sorted(w))})" for c, w in sorted(missing.items()))
    return _check("ComfyUI nodes", "fail" if required & set(missing) else "warn", f"missing: {detail}",
                  "update ComfyUI: cd ~/comfyui && git pull, then pip install its requirements.txt into "
                  "~/venvs/comfy with torch/torchvision/torchaudio/triton filtered out; "
                  "then sudo systemctl restart comfyui")


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
    if stats is not None:
        checks.append(check_nodes(conf, conf["comfy_url"]))
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
