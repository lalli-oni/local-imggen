"""A fake ComfyUI HTTP server for tests: the endpoints imagegen and doctor use, no GPU.

Every queued job "finishes" after `delay` seconds and outputs one small PNG whose colour comes
from the job's seed, so different seeds give different files. Tests can inspect what was sent
(`prompts`, `uploads`, `requests`) and switch on failures (`reject`, `fail_jobs`, `no_gpu`).
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.parse
from pathlib import Path
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

PNG_SIZE = (64, 48)
WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"


def png_bytes(color=(200, 80, 40), size=PNG_SIZE, mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def _seed_of(workflow: dict):
    for node in workflow.values():
        seed = node.get("inputs", {}).get("seed")
        if seed is not None:
            return seed
    return 0


class FakeComfy:
    def __init__(self):
        self.prompts: dict[str, dict] = {}   # prompt_id -> {"workflow", "queued_at"}
        self.order: list[str] = []
        self.uploads: dict[str, bytes] = {}  # name -> bytes
        self.requests: list[tuple[str, str]] = []  # (method, path)
        self.delay = 0.0          # seconds until a queued job shows up in /history
        self.reject: str | None = None  # /prompt answers 400 with this text
        self.fail_jobs = False    # finished jobs report status_str "error"
        self.no_gpu = False       # /system_stats lists only a CPU
        self.vram_free = 12 * 1024 ** 3   # /system_stats numbers (tests may change them mid-run)
        self.ram_free = 20 * 1024 ** 3
        self.frees = 0            # POST /free calls (unload models)
        self.fail_preset: str | None = None  # /prompt rejects workflows whose unet_name contains this
        self.models = {"checkpoints": ["sd_xl_base_1.0.safetensors"],
                       "upscale_models": ["RealESRGAN_x4plus.pth"],
                       "diffusion_models": ["flux1-dev-kontext_fp8_scaled.safetensors",
                                            "flux-2-klein-4b-fp8.safetensors", "qwen_image_2.1_bf16.safetensors"],
                       "text_encoders": ["clip_l.safetensors", "t5xxl_fp8_e4m3fn_scaled.safetensors",
                                         "qwen_3_4b.safetensors", "qwen3vl_8b_bf16.safetensors"],
                       "vae": ["ae.safetensors", "flux2-vae.safetensors", "qwen_image_2.1_vae_bf16.safetensors"]}
        # node types /object_info lists: every class_type the repo's workflows use (tests may remove some)
        self.nodes = {n["class_type"] for wf in WORKFLOWS_DIR.glob("*.json")
                      for n in json.loads(wf.read_text()).values()}
        self.queue_running = 0
        self.queue_pending = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)

    def start(self) -> "FakeComfy":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # what tests usually want to look at
    def workflows(self) -> list[dict]:
        return [self.prompts[p]["workflow"] for p in self.order]

    def _history(self, pid: str) -> dict:
        job = self.prompts.get(pid)
        if job is None or time.time() - job["queued_at"] < self.delay:
            return {}
        if self.fail_jobs:
            return {pid: {"status": {"status_str": "error", "messages": [["execution_error", {"m": "boom"}]]},
                          "outputs": {}}}
        return {pid: {"status": {"status_str": "success"}, "outputs": {"9": {"images": [
            {"filename": f"imagegen_{pid}.png", "subfolder": "", "type": "output"},
            {"filename": f"preview_{pid}.png", "subfolder": "", "type": "temp"}]}}}}

    def _view(self, filename: str) -> bytes:
        pid = filename.removeprefix("imagegen_").removesuffix(".png")
        seed = _seed_of(self.prompts[pid]["workflow"]) if pid in self.prompts else 0
        return png_bytes(((seed * 37) % 256, (seed * 91) % 256, (seed * 13) % 256))

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep pytest output clean
                pass

            def _send(self, code: int, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                fake.requests.append(("GET", u.path))
                q = dict(urllib.parse.parse_qsl(u.query))
                if u.path == "/system_stats":
                    devs = [{"name": "cpu", "type": "cpu"}] if fake.no_gpu else [
                        {"name": "AMD Radeon RX 9070 XT", "type": "cuda",
                         "vram_total": 16 * 1024 ** 3, "vram_free": fake.vram_free}]
                    return self._send(200, {"system": {"comfyui_version": "fake", "ram_total": 24 * 1024 ** 3,
                                                       "ram_free": fake.ram_free}, "devices": devs})
                if u.path.startswith("/history/"):
                    return self._send(200, fake._history(u.path.rsplit("/", 1)[1]))
                if u.path == "/view":
                    return self._send(200, fake._view(q.get("filename", "")), "image/png")
                if u.path == "/queue":
                    return self._send(200, {"queue_running": [[0]] * fake.queue_running,
                                            "queue_pending": [[0]] * fake.queue_pending})
                if u.path == "/object_info":
                    return self._send(200, {c: {} for c in fake.nodes})
                if u.path.startswith("/models/"):
                    return self._send(200, fake.models.get(u.path.rsplit("/", 1)[1], []))
                self._send(404, {"error": "not found"})

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                fake.requests.append(("POST", u.path))
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if u.path == "/prompt":
                    wf = json.loads(body)["prompt"]
                    unets = [n["inputs"].get("unet_name", "") for n in wf.values() if n["class_type"] == "UNETLoader"]
                    if fake.reject or (fake.fail_preset and any(fake.fail_preset in u for u in unets)):
                        return self._send(400, {"error": fake.reject or "value_not_in_list: unet_name"})
                    pid = uuid.uuid4().hex
                    fake.prompts[pid] = {"workflow": wf, "queued_at": time.time()}
                    fake.order.append(pid)
                    return self._send(200, {"prompt_id": pid, "number": len(fake.order)})
                if u.path == "/free":
                    fake.frees += 1
                    return self._send(200, {})
                if u.path == "/upload/image":
                    name, data = _parse_upload(self.headers["Content-Type"], body)
                    fake.uploads[name] = data
                    return self._send(200, {"name": name, "subfolder": "", "type": "input"})
                self._send(404, {"error": "not found"})

        return H


def _parse_upload(ctype: str, body: bytes) -> tuple[str, bytes]:
    """Just enough multipart parsing for Comfy.upload(): the "image" part's filename and bytes."""
    boundary = ctype.split("boundary=", 1)[1].encode()
    for part in body.split(b"--" + boundary):
        head, _, data = part.partition(b"\r\n\r\n")
        if b'name="image"' in head:
            name = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            return name, data.removesuffix(b"\r\n")
    raise ValueError("no image part")
