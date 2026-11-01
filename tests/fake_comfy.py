"""A fake ComfyUI HTTP server for tests: the endpoints imagegen and doctor use, no GPU.

Every queued job "finishes" after `delay` seconds and outputs one small PNG whose colour comes
from the job's seed, so different seeds give different files. Tests can inspect what was sent
(`prompts`, `uploads`, `requests`) and switch on failures (`reject`, `fail_jobs`, `no_gpu`).

Like ComfyUI, it "runs" each job's nodes in dependency order and reports them on the websocket
(/ws?clientId=...) as execution_start, execution_cached, executing {node} ... executing None,
execution_success; /history carries the timestamps of start/cached/success. `node_s` sets seconds per
class_type (e.g. {"KSampler": 0.2}), `cached` the node ids reported as cached (they are skipped),
and `ws = False` turns the websocket off (404), as on a ComfyUI without one.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
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
        self.ws = True
        self.node_s: dict[str, float] = {}  # class_type -> seconds each node of it takes
        self.cached: list[str] = []         # node ids reported as cached (not run)
        self.ws_clients: dict[str, "_WS"] = {}  # client_id -> socket
        self.ws_sent: list[dict] = []       # every message sent on a websocket
        self.ws_received: list[tuple[int, bytes]] = []  # (opcode, data) of every client frame
        self.ws_end_lag = 0.0  # /history shows the job done this long before the socket's last frames
        self.outputs_per_job = 1
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

    def _execute(self, pid: str, client_id: str | None) -> None:
        """Play the job: websocket events per node (with node_s sleeps), then /history shows it as done."""
        job = self.prompts[pid]
        time.sleep(self.delay)
        msgs = job["messages"]

        def emit(kind: str, data: dict, history: bool = False) -> None:
            data = {**data, "prompt_id": pid, "timestamp": int(time.time() * 1000)}
            if history:
                msgs.append([kind, data])
            msg = {"type": kind, "data": data}
            ws = self.ws_clients.get(client_id)
            if ws is not None:
                self.ws_sent.append(msg)
                ws.send(1, json.dumps(msg).encode())

        ws = self.ws_clients.get(client_id)
        if ws is not None:  # things a real ComfyUI sends too, which the client must skip
            ws.send(1, json.dumps({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}}).encode())
            ws.send(9, b"ping")
            ws.send(2, b"\x00" * 70000)  # a big binary preview frame (64-bit length)
        emit("execution_start", {}, history=True)
        emit("execution_cached", {"nodes": list(self.cached)}, history=True)
        for node in _topo_order(job["workflow"]):
            if node in self.cached:
                continue
            emit("executing", {"node": node, "display_node": node})
            time.sleep(self.node_s.get(job["workflow"][node]["class_type"], 0))
        if self.ws_end_lag:
            msgs.append(["execution_success", {"prompt_id": pid, "timestamp": int(time.time() * 1000)}])
            job["done"] = True
            time.sleep(self.ws_end_lag)
            emit("executing", {"node": None})
            emit("execution_success", {})
            return
        emit("executing", {"node": None})
        emit("execution_success", {}, history=True)
        job["done"] = True

    def _history(self, pid: str) -> dict:
        job = self.prompts.get(pid)
        if job is None or time.time() - job["queued_at"] < self.delay or not job.get("done"):
            return {}
        if self.fail_jobs:
            return {pid: {"status": {"status_str": "error", "messages": [["execution_error", {"m": "boom"}]]},
                          "outputs": {}}}
        extra = [{"filename": f"imagegen_{pid}.png", "subfolder": "", "type": "output"}] * (self.outputs_per_job - 1)
        return {pid: {"status": {"status_str": "success", "messages": job["messages"]}, "outputs": {"9": {"images": [
            *extra, {"filename": f"imagegen_{pid}.png", "subfolder": "", "type": "output"},
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
                if u.path == "/ws" and fake.ws:
                    return self._websocket(q.get("clientId", ""))
                if u.path.startswith("/models/"):
                    return self._send(200, fake.models.get(u.path.rsplit("/", 1)[1], []))
                self._send(404, {"error": "not found"})

            def _websocket(self, client_id: str):
                key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                # registered before the 101 goes out: the client queues its jobs as soon as it has it
                # small frames go out at once, as aiohttp does in ComfyUI (Nagle would hold them ~40 ms)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                ws = fake.ws_clients[client_id] = _WS(self.rfile, self.wfile, fake.ws_received)
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
                self.end_headers()
                self.wfile.flush()
                ws.serve()  # until the client closes
                fake.ws_clients.pop(client_id, None)
                self.close_connection = True

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
                    req = json.loads(body)
                    fake.prompts[pid] = {"workflow": wf, "queued_at": time.time(), "messages": []}
                    fake.order.append(pid)
                    threading.Thread(target=fake._execute, args=(pid, req.get("client_id")), daemon=True).start()
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


def _topo_order(workflow: dict) -> list[str]:
    """Node ids in an order where every node comes after the nodes it takes inputs from (like ComfyUI)."""
    order: list[str] = []

    def visit(n: str) -> None:
        if n in order:
            return
        for v in workflow[n].get("inputs", {}).values():
            if isinstance(v, list) and len(v) == 2 and str(v[0]) in workflow:
                visit(str(v[0]))
        order.append(n)

    for n in workflow:
        visit(n)
    return order


class _WS:
    """The server side of one websocket: send frames, and read the client's (masked) ones."""

    def __init__(self, rfile, wfile, received: list):
        self.rfile, self.wfile = rfile, wfile
        self.lock = threading.Lock()
        self.received = received

    def send(self, op: int, data: bytes) -> None:
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big")
                                      if n < 65536 else bytes([127]) + n.to_bytes(8, "big"))
        with self.lock:
            try:
                self.wfile.write(head + data)
                self.wfile.flush()
            except OSError:
                pass

    def serve(self) -> None:
        try:
            while True:
                head = self.rfile.read(2)
                if len(head) < 2:
                    return
                n = head[1] & 0x7F
                if n == 126:
                    n = int.from_bytes(self.rfile.read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self.rfile.read(8), "big")
                mask = self.rfile.read(4) if head[1] & 0x80 else b"\0\0\0\0"
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(self.rfile.read(n)))
                self.received.append((head[0] & 0x0F, data))
                if head[0] & 0x0F == 8:
                    return
        except OSError:
            return
