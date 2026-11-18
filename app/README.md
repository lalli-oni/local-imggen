# imagegen viewer app (#82)

A SvelteKit PWA that shows everything imagegen made: projects (board, ratings, notes, history),
characters (refs, stage, versions), wallpapers (daily runs + `daily.log`) and every run.
**Read-only**: it never runs the CLI or starts a job.

The data comes from `gen app serve` (`appserve.py`, stdlib Python, 127.0.0.1:8191), which also serves the
built app from `app/build/`. Node is only needed to build it.

## On tulu

```bash
# once: Node 22 LTS (if `node --version` says nothing or < 20)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs

cd ~/imagegen/app && npm ci && npm run build
gen app serve --open
```

Then in Edge or Chrome, use "Install app" in the address bar for its own window and a Start menu entry.
After a `git pull` that changes `app/`, run `npm ci && npm run build` again (the server picks it up on reload).

## Developing

```bash
gen app serve            # the API on :8191 (or point at another: IMAGEGEN_APP_API=http://127.0.0.1:PORT)
cd app && npm run dev    # http://127.0.0.1:5173 with hot reload, /api and /files proxied to the server
npm run check            # svelte-check, warnings fail (CI runs it)
```

- `src/lib/api.ts`: types for every endpoint (keep them in step with appserve.py) and `api()`.
- `src/routes/`: one folder per page; each `+page.ts` loads its JSON.
- `src/service-worker.ts`: caches the app shell only; `/api` and `/files` always hit the network.
- Tests: `tests/test_appserve.py` (API) and `tests/test_app_browser.py` (the built app in Chromium; skipped
  until `npm run build`, required in CI).
