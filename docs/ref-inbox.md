# Reference inbox (#112)

Good reference images (worlds, characters, wallpapers) come from the free tiers of ChatGPT, Gemini, Designer,
Leonardo, Ideogram. Their terms ban bots and programmatic extraction of results, even of images you made, and a
browser agent is such a bot. So a **person clicks Generate and saves the file**; the tooling prepares and imports.

```
scheduled Claude session -> plan text (JSON)  -> extension side panel (copy buttons, outlines) + gen ref plan
you (a few clicks per image) -> save <id>.png into <output_dir>/refs-inbox/<date>/   (or a [refs] watch folder)
gen ref import --yes      -> project asset (+ ref) or character ref, with service, prompt, task, date
```

- Plan text, ids, targets: the docstring of `refs.py`. A project target makes a new asset version (optionally also a
  project ref); a character target makes a new character version (stales the assets that show it, like any ref change).
- Provenance: the asset version has `external` {service, prompt, task, date} (also in `gen project status` and the
  JSON the viewer app reads); the character version's note carries service and prompt.
- Free caps: `[ref_services.*] daily` in config.toml, unverified numbers: a plan above a cap gets a warning.
- Why not driving the sites: see the terms section of #112. A browser extension that fills the box, attaches files,
  presses Generate or saves images is a bot under those terms; this one does none of that (extension/README.md).

Not built yet (#112): planning from what is missing in a project/character (today the Claude session writes the
tasks), the scheduled task itself, a library view, API providers, notification of the daily checklist.
