"""Character stages (#68, first iteration): explore -> converge -> polish.

A character is in one stage at a time (characters.py, state["stage"], default explore). A project works
in the stage of its first cast character, and each stage generates its own way ([stages.*] in config.toml):

  explore   text only (the cast descriptions + project prompt), many cheap images: a rough direction
  converge  an edit of one image (the selected board image, else the character's first ref), one idea
            per image ("sitting down", "seen from the side"): on-model variety
  polish    an edit with one targeted fix you type ("remove the bump on the right elbow"), a few images

A stage pass is an ordinary project pass (projects.record_pass) that also records stage, source and text,
so the board and project_get can show where each pass came from. Promoting the best one to the character
ref is still `gen char set NAME --ref main=PATH` (a new character version).
"""
from __future__ import annotations

from pathlib import Path

import imagegen
import projects

STAGES = ("explore", "converge", "polish")
MODES = ("text", "img2img", "edit")

# What each stage runs when config.toml has no [stages.NAME] (or leaves a key out).
DEFAULTS = {
    "explore": {"mode": "text", "preset": "draft", "count": 8},
    "converge": {"mode": "edit", "preset": "klein", "count": 4,
                 "instruction": "The same character with the same design, proportions and colours, {text}. "
                                "Plain light background, only this one character.",
                 "ideas": ["sitting down", "waving hello", "seen from the side", "a three-quarter view",
                           "jumping happily", "looking surprised", "walking towards the viewer", "sleeping"]},
    "polish": {"mode": "edit", "preset": "klein", "count": 2, "needs_text": True,
               "instruction": "{text}. Keep everything else exactly the same: the same character, pose, "
                              "colours and background."},
}


def settings(stage: str, conf: dict | None = None) -> dict:
    """One stage's settings: config.toml [stages.STAGE] over DEFAULTS (conf: a loaded config)."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage '{stage}' (stages: {', '.join(STAGES)})")
    conf = (conf if conf is not None else imagegen.load_config()).get("stages", {}).get(stage, {})
    s = {**DEFAULTS[stage], **conf}
    if s["mode"] not in MODES:
        raise ValueError(f"[stages.{stage}] mode = '{s['mode']}': use one of {', '.join(MODES)}")
    return s


def all_settings() -> dict:
    """{stage: what its generate does}, for the board's buttons and project_get."""
    out, conf = {}, imagegen.load_config()
    for st in STAGES:
        s = settings(st, conf)
        out[st] = {"mode": s["mode"], "preset": s["preset"], "count": int(s["count"]),
                   "needs_text": bool(s.get("needs_text")), "needs_source": s["mode"] != "text"}
    return out


def character_stage(ch: dict) -> str:
    """A character's stage (characters made before #68 have none: explore)."""
    return ch["state"].get("stage") or STAGES[0]


def of_project(state: dict) -> dict | None:
    """{"character", "stage"}: the stage the project works in, from its first cast character
    (the latest character version: a pin fixes the look, not the process). None without a cast."""
    import characters
    for name in state.get("cast", {}):
        try:
            return {"character": name, "stage": character_stage(characters.load(name))}
        except FileNotFoundError:
            continue
    return None


def first_ref(name: str) -> str | None:
    """The name of a character's first ref (what converge/polish edit, and what the board's ref button replaces)."""
    import characters
    return next(iter(characters.load(name)["state"]["refs"]), None)


def _source(slug: str, proj: dict, source: str | None, char: str | None) -> tuple[Path, str]:
    """(image path, how the pass records it) for P:N, an image path, CHARACTER:REF, or None: the character's
    first ref at its latest version (what the board's ref button just set, even in a project that pins it)."""
    import boards
    import characters
    st = proj["state"]
    if source is None or not source.strip():
        if char is None:
            raise ValueError("this stage edits an image: pick one on the board (or pass --from P:N or a path)")
        ch = characters.load(char)
        ref = next(iter(ch["state"]["refs"]), None)
        if ref is None:
            raise ValueError(f"character '{char}' has no ref yet: pick an image on the board "
                             f"(or gen char set {char} --ref main=PATH)")
        return characters.char_dir(char) / ch["state"]["refs"][ref]["file"], f"{char}:{ref}"
    source = source.strip()
    if boards._PASS_ID.match(source):
        key, pid = boards.resolve(proj, source)
        return boards._abs(key), pid
    path = imagegen.to_linux_path(source)
    if path.is_file():
        if path.suffix.lower() not in projects.IMAGE_EXTS:
            raise ValueError(f"{path.name}: not an image ({', '.join(sorted(projects.IMAGE_EXTS))})")
        return path, str(path)
    name, sep, _ = source.partition(":")
    if sep and name in st.get("cast", {}):
        return projects._ref_path(slug, st, source), source
    raise FileNotFoundError(f"no image '{source}' (use P:N, an image path, or CHARACTER:REF)")


def _offset(proj: dict, stage: str) -> int:
    """How many ideas earlier passes of this stage took from the list, so "generate more" moves on."""
    return sum(p.get("ideas", 0) for p in proj["passes"] if p.get("stage") == stage)


def run_stage(slug: str, stage: str | None = None, count: int | None = None, source: str | None = None,
              text: str | None = None, seed: int | None = None, author: str = "human") -> dict:
    """A pass the way a stage generates (stage None = the project's current stage).

    source: the image converge/polish start from (P:N, a path, or CHARACTER:REF; default the character's
    first ref). text: explore = extra prompt text (CLI/MCP; the board leaves it out); converge = one idea for every image (instead of the
    idea list); polish = the fix (required)."""
    import characters
    proj = projects.load(slug)
    st = proj["state"]
    cur = of_project(st)
    stage = stage or (cur["stage"] if cur else None)
    if stage is None:
        raise ValueError(f"project '{slug}' casts no character, so it has no stage: "
                         f"gen project set {slug} --cast NAME (or pass --stage)")
    s = settings(stage)
    count = max(1, int(s["count"] if count is None else count))
    text = (text or "").strip() or None
    if s.get("needs_text") and not text:
        raise ValueError(f"{stage} needs an instruction: the one thing to fix")
    char = cur["character"] if cur else None
    all_used = projects.cast_versions(st)
    kwargs = dict(preset=s["preset"], count=count, seed=seed, label=f"{slug}-v{proj['version']}-{stage}")
    record = {"stage": stage, "text": text}

    if s["mode"] == "text":
        used = all_used
        cast_text = [characters.state_at(characters.load(c), v)["description"] for c, v in used.items()]
        prompt = projects.full_prompt(st, text, cast_text)
        if not prompt:
            raise ValueError(f"project '{slug}' has no prompt yet: gen project set {slug} --prompt '...'")
        kwargs.update(prompt=prompt, negative=st["negative"])
        extra = text
    else:
        used = {char: all_used[char]} if char else {}
        path, record["source"] = _source(slug, proj, source, char)
        kwargs["source_image"] = str(path)
        extra = None
        if s["mode"] == "img2img":
            cast_text = [characters.state_at(characters.load(c), v)["description"] for c, v in used.items()]
            prompt = projects.full_prompt(st, text, cast_text)
            if not prompt:
                raise ValueError(f"project '{slug}' has no prompt yet: gen project set {slug} --prompt '...'")
            kwargs.update(prompt=prompt, negative=st["negative"], denoise=s.get("strength"))
            extra = text
        else:
            tmpl = s.get("instruction") or "{text}"
            if text:
                prompts = [tmpl.replace("{text}", text)]
            else:
                ideas = list(s.get("ideas") or [])
                if not ideas:
                    raise ValueError(f"{stage} needs an instruction ([stages.{stage}] has no ideas list)")
                off = _offset(proj, stage)
                prompts = [tmpl.replace("{text}", ideas[(off + i) % len(ideas)]) for i in range(count)]
                record["ideas"] = count
            # the run's own prompt (run.json, contact sheet): the template, when each image has its own idea
            kwargs.update(prompt=prompts[0] if text else tmpl.replace("{text}", "[one idea per image]"),
                          prompts=prompts, edit=True)
    run = imagegen.generate(**kwargs)
    return projects.record_pass(slug, run, proj["version"], used, author, extra=extra,
                                **{k: v for k, v in record.items() if v is not None})
