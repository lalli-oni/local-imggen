# gen tunnel: imagegen tools for project threads (#71)

Project threads run in the cloud and can't reach tulu. `gen tunnel` lets them call a fixed set of the
imagegen MCP tools for as long as Þór keeps it open. It is a stopgap: remove it once project sessions
can use trusted devices (see "Removing it" below).

## Using it

On tulu, once:

```bash
~/imagegen/tunnel-cloudflared.sh install     # pinned cloudflared, sha256-checked, ~/.local/bin, no sudo
```

Each time:

```bash
gen tunnel                 # prints one line to paste into the thread; Ctrl-C closes it
gen tunnel --idle-min 30   # close sooner when unused (default 120)
```

In the thread (the line from Þór's message sets both variables; never save the token anywhere).
`python` is any Python with `mcp` installed, e.g. `.venv-test/bin/python` after one `./test.sh` run:

```bash
export IMAGEGEN_TUNNEL_URL=https://<random>.trycloudflare.com/mcp IMAGEGEN_TUNNEL_TOKEN=<token>
python tunnel.py tools
python tunnel.py call doctor
python tunnel.py call generate '{"prompt": "a fox", "preset": "draft"}'
python tunnel.py call get_job '{"job_id": "ab12cd34"}'
python tunnel.py call view_image '{"path": "/mnt/c/Users/lalli/Pictures/imagegen/..."}'   # saves a JPEG, prints its path
```

The cloud environment must allow the tunnel's host. A quick tunnel gets a new random
`<words>.trycloudflare.com` name every time, so either add `*.trycloudflare.com` under Allowed domains
in the project's cloud environment settings once, or have the thread request the exact host each time
(an approval card per tunnel; tighter, more clicks).

## What it exposes

Only these tools (`TOOLS` in tunnel.py), each with mcp_server.py's own code and schema:

| Kind | Tools |
|---|---|
| Read | doctor, list_presets, list_styles, recent_runs, run_info (a run's run.json, #103), view_image, get_job, project_list, project_get, character_list, character_get |
| Render | generate, vary, edit, cutout (new files inside the output folder) |

project_get records what the reader has seen as "thread", separate from the desktop app's "claude".

Not exposed: set_wallpaper and open_contact_sheet (they act on the Windows desktop), project_import
(reads any folder), and everything that changes project or character state (project_new/update/
generate/promote/keep/rate/note/doc, character_new/update). There is no shell, no file listing and no
download of arbitrary files. Adding a tool is an edit to `TOOLS`, reviewed like any change.

## Threat model

**Who can reach it.** The server binds 127.0.0.1 only. The one way in from outside is the
Cloudflare quick tunnel, whose hostname is random and new each time. Anyone who learns that hostname
can send requests, so the hostname is not treated as a secret: the token is.

**The token.** `secrets.token_urlsafe(32)` (256 bits) made fresh by every `gen tunnel`, held only in
that process's memory and printed once in Þór's terminal. It is never written to disk, the repo,
config, logs or project memory. Every request must carry `Authorization: Bearer <token>`, compared in
constant time (`hmac.compare_digest`) before the request body is read, so an unauthenticated request
never reaches JSON parsing, MCP or imagegen. Guessing is hopeless at 256 bits, and the tunnel closes
itself after 20 rejected requests through Cloudflare anyway (rejected local ones, e.g. a browser tab
on localhost, are logged but can't close it). WebSocket upgrades are dropped unanswered. When the tunnel closes, the token is gone; the next one is
unrelated. The token does pass through the project thread (Þór pastes it there) and so through
Anthropic's chat history; it is useless once that tunnel is closed.

**What a token holder can do.** Run the tools above: render images on the GPU, read imagegen's own
output (thumbnails, run JSON, project and character summaries) and doctor's report. Image paths
(`path`, `mask`) must resolve, after following symlinks, to an existing .png/.jpg/.jpeg/.webp inside
`output_dir`, and the tool gets the resolved path, so `../`, absolute paths elsewhere and symlinks
pointing out are refused. Numbers are range-checked (count <= 8, width/height <= 2048, steps <= 100,
...), strings are capped at 4000 characters, lists at 20 short strings. Worst case with a stolen
token: the GPU is kept busy and the output folder fills with renders until the tunnel closes.

**Limits.** Request bodies over 64 KB are refused (counted as a bad request). 120 authorised requests a
minute (a tool call is about 3), 8 concurrent connections. MCP runs stateless: there are no sessions
to hijack (the bug class of CVE-2026-52869).

**The log.** Every request is one line in Þór's terminal: time, tool and arguments (or the method), the
status and how long it took; rejected ones say why and from which IP (Cloudflare's CF-Connecting-IP).
Non-printable characters are escaped, so a request can't inject terminal escape sequences.

**Closing.** Ctrl-C stops the server and then cloudflared, so the hostname stops resolving to tulu.
It also closes itself after `--idle-min` (default 120) minutes without an authorised request, or after
20 rejected requests, or when cloudflared stops. Renders a thread started are dropped with the process;
ComfyUI keeps the jobs it already queued, so the log then prints the command that clears its queue. Nothing restarts it: there is no
service, timer or autostart.

**What's trusted.** Cloudflare sees the traffic in clear (TLS ends at their edge; the quick tunnel then
carries it to cloudflared): the token, prompts and thumbnails. The quick tunnel has no uptime promise.
cloudflared's own outbound connection to Cloudflare is the only network change on tulu; it opens no
listening port.

### Known vulnerabilities in what it uses (checked 2026-11-17)

- **cloudflared**: two CVEs ever, both Windows-only local privilege escalation (CVE-2020-24356, fixed
  2020.8.1; CVE-2023-1314 in the 32-bit Windows installer, fixed 2023.3.1). Neither applies to the
  Linux binary. Pinned to 2026.10.7 (released 2026-10-30, past the two-week rule) by sha256 in
  `tunnel-cloudflared.sh`; the script refuses a file whose hash differs.
- **MCP Python SDK** (`mcp`, requirements.txt `>=2,<3`): CVE-2025-53365 (DoS from an unhandled
  exception in streamable HTTP, fixed 1.10.0), CVE-2025-66416 (DNS rebinding protection off by
  default; we turn it off on purpose because the Host is the tunnel's name, and the token replaces it),
  CVE-2026-52869 (session routing by session id alone, stateful HTTP and SSE only, fixed 1.27.2; we run
  stateless). All fixed versions are below 2.0.
- **Starlette** (comes with mcp): a run of 2026 advisories: CVE-2026-48710 Host header validation
  (fixed 1.0.1), CVE-2026-48818 StaticFiles SSRF on Windows, CVE-2026-48817 HTTPEndpoint method
  dispatch, CVE-2026-54282 request.url hostname poisoning, CVE-2026-54283 form-parsing limits. The
  tunnel uses none of StaticFiles, HTTPEndpoint, forms or request.url, and unauthenticated requests
  never get past the token check into Starlette. Keep it current (`pip install -U` in
  ~/venvs/imagegen, never in ~/venvs/comfy).
- **uvicorn**: CVE-2020-7694 (log escape injection) and CVE-2020-7695 (header CRLF), both fixed in
  0.11.7 years ago; no newer advisories found. Its access log is off; the tunnel writes its own.

Sources: GitHub advisories GHSA-7mjv-x3jf-545x, GHSA-hgwp-4vp4-qmm2 (cloudflared),
GHSA-j975-95f5-7wqh, GHSA-9h52-p55h-vw2f (mcp), GHSA-86qp-5c8j-p5mr (starlette),
GHSA-33c7-2mpw-hg34, GHSA-f97h-2pfx-f59f (uvicorn), and NVD.

### Why the code is in the public repo

Þór chose this (2026-11-17). The protection is the per-run token, checked before anything else, not
the code being secret; the dependencies are listed in requirements.txt already. In the repo the tunnel
runs in CI with the rest of the suite and gets reviewed like any change.

## Removing it

Delete tunnel.py, tunnel-cloudflared.sh, this file, tests/test_tunnel*.py, the `tunnel` lines in
imagegen.py (usage and `main`) and the CLAUDE.md entry. On tulu: `~/imagegen/tunnel-cloudflared.sh remove`
(before deleting the script), and take `*.trycloudflare.com` out of the cloud environment's allowed domains.
