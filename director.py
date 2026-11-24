"""Claude prompt pass (#94): the Claude subscription (Claude Code CLI, headless, no API key) as an optional
step before image generation.

`direct()` rewrites a wallpaper theme into a fuller scene: gags, what the cast is doing, how they interact, and
a rough layout (kept in run.json for a later layout-controlled pass). `ideas()` writes a batch of new themes
upfront (`gen ideas`), which [daily.ideas] can add to the theme pool.

Optional and never fatal: with `claude` missing, logged out, slow, or answering badly (not JSON, a quoted sign
text lost), the callers get (None, reason) and carry on with the plain theme. The CLI gets no tools
(`--tools ""`), no saved session, and runs from a temp folder so no CLAUDE.md is picked up.

Settings: [daily.director] enabled (default false), model ("" = the CLI's default), timeout (seconds).
          [daily.ideas] use (add ideas.json's themes to the pool), model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import date
from pathlib import Path

import imagegen

TIMEOUT = 180
MAX_SCENE = 900  # characters: past this the image model drops half of it anyway

HOUSE = """The look: synthwave / vaporwave magical realism, 80s and 90s nostalgia (arcades, CRTs, VHS, chrome, a DeLorean),
realistic but outrageous, extravagant, funny and quirky. Not storybook. Normal 80s interiors (shag pile, wood panels,
waterbeds), not purple everywhere. No abstract geometric shapes or glitch blocks."""

DIRECT_SYSTEM = f"""You are the art director for a funny wallpaper made by an image model (Qwen-Image). You get a theme, \
and you answer with ONE JSON object and nothing else:
{{"scene": "...", "gags": ["..."], "layout": "..."}}

scene: the rewritten theme as one image prompt, 50 to 120 words. It continues the sentence "A small creature, ..." so \
it starts with what the characters are doing (an -ing verb), then the place, the interactions, the gags, the light. \
Concrete visible things only: what is where and who does what. No explaining of the obvious, no mood words that \
draw nothing.
gags: 1 to 3 short jokes in the scene (a visual gag, a reaction of a side character, a punny prop), already worked into scene.
layout: one or two sentences of rough composition: foreground, midground, background, and where each character stands \
in the frame (left third, right third...), so the eye travels across a 16:9 picture.

Rules:
- Keep every quoted sign text from the theme exactly as given (same letters, same quotes). Add no other text anywhere \
in the image.
- Keep the theme's setting, props and mission. Add to them, don't replace them. Few elements and gags spread out, \
rather than many crowded ones.
- Name the characters only by the roles you are given (e.g. "the character on the left"), never by their real names.
- Other creatures may appear, but they must look clearly different from the characters.
{HOUSE}"""

IDEAS_SYSTEM = f"""You write wallpaper themes for a funny image generator. Answer with ONE JSON object and nothing else: \
{{"themes": ["...", "..."]}}.
Each theme is one line like the examples: what the little character is doing (an -ing verb first), where, one \
absurd set piece, and exactly one punny sign whose exact text is given in quotes ("a neon sign reads 'SQUID PRO QUO'"). \
No other text in the scene. Mix places (neon cities, space, underwater, the cosy countryside), keep each theme \
different from the examples and from each other. Under 45 words each.
{HOUSE}"""


class Unavailable(Exception):
    """The Claude CLI can't give a usable answer (reason in the message)."""


def available() -> bool:
    return shutil.which("claude") is not None


def ask(system: str, prompt: str, model: str = "", timeout: float = TIMEOUT) -> dict:
    """Run `claude -p` once and return the JSON object it answered with. Raises Unavailable."""
    exe = shutil.which("claude")
    if not exe:
        raise Unavailable("the claude CLI is not installed (check: claude --version)")
    cmd = [exe, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--disable-slash-commands", "--system-prompt", system]
    if model:
        cmd += ["--model", model]
    try:
        # cwd = a temp folder, so the CLI does not read this repo's CLAUDE.md. Own process group: on a timeout the
        # whole group is killed, or a child holding the pipes open would keep us waiting past the timeout.
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=tempfile.gettempdir(), start_new_session=True)
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
            raise Unavailable(f"claude did not answer within {timeout:.0f}s") from None
    except OSError as e:
        raise Unavailable(f"could not run claude: {e}") from None
    p = types.SimpleNamespace(returncode=proc.returncode, stdout=stdout, stderr=stderr)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = None
    if p.returncode != 0 or not isinstance(out, dict) or out.get("is_error"):
        why = (out.get("result") if isinstance(out, dict) else "") or p.stderr.strip() or p.stdout.strip()
        raise Unavailable(f"claude failed (exit {p.returncode}): {str(why)[:200] or 'no output'} "
                          "(logged in? run `claude` once to sign in)")
    return _json_object(str(out.get("result", "")))


def _json_object(text: str) -> dict:
    """The first {...} in the answer (models sometimes wrap it in a code fence)."""
    a, b = text.find("{"), text.rfind("}")
    try:
        obj = json.loads(text[a:b + 1]) if a != -1 and b > a else None
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        raise Unavailable(f"claude's answer was not a JSON object: {text[:120]!r}")
    return obj


def signs(text: str) -> list[str]:
    """Quoted sign texts in a theme: 'SQUID PRO QUO'. A quote opens after a space, bracket or the start (not Tulu's)
    and closes before a space or punctuation (so "CAN'T STOP" is one sign)."""
    found = re.findall(r"""(?:^|(?<=[\s(]))['"](.+?)['"](?=[\s,.;:)]|$)""", text)
    return [m.strip() for m in found if len(m.strip()) > 1]


def check_scene(theme: str, obj: dict) -> dict:
    """Validate direct()'s answer: {scene, gags, layout}. Raises Unavailable on a bad one."""
    if not isinstance(obj, dict):
        raise Unavailable("claude's answer is not a JSON object")
    scene = obj.get("scene")
    if not isinstance(scene, str) or not scene.strip():
        raise Unavailable("claude's answer has no scene")
    scene = " ".join(scene.split())
    if len(scene) > MAX_SCENE:
        raise Unavailable(f"claude's scene is {len(scene)} characters (max {MAX_SCENE})")
    lost = [s for s in signs(theme) if s not in scene]
    if lost:  # Qwen writes gibberish on signs it makes up, so a lost sign text is worse than no rewrite
        raise Unavailable(f"claude's scene lost the sign text {lost[0]!r}")
    raw = obj.get("gags")
    gags = [g.strip() for g in (raw if isinstance(raw, list) else []) if isinstance(g, str) and g.strip()][:5]
    layout = obj.get("layout")
    return {"scene": scene, "gags": gags, "layout": " ".join(layout.split()) if isinstance(layout, str) else ""}


def direct(theme: str, who: list[str], model: str = "", timeout: float = TIMEOUT) -> dict:
    """Rewrite `theme` for the characters in `who` (roles like "the main character"). Returns
    {scene, gags, layout}; raises Unavailable."""
    prompt = (f"Characters in the picture: {', '.join(who) or 'the main character'}.\n"
              f"Theme: {theme}\n"
              + (f"Sign texts to keep exactly: {', '.join(repr(s) for s in signs(theme))}\n" if signs(theme) else ""))
    return check_scene(theme, ask(DIRECT_SYSTEM, prompt, model, timeout))


def cache_file(conf: dict, day: date, theme: str, who: list[str]) -> Path:
    key = hashlib.sha1(json.dumps([theme, who, DIRECT_SYSTEM], sort_keys=True).encode()).hexdigest()[:10]
    return Path(conf["output_dir"]).expanduser() / "director" / f"{day.isoformat()}-{key}.json"


def direct_cached(conf: dict, day: date, theme: str, who: list[str], model: str = "", timeout: float = TIMEOUT) -> dict:
    """direct(), remembered per day and theme: `gen daily --show` and the real run (or a re-run) agree, and
    Claude is asked once. A different theme or cast asks again."""
    f = cache_file(conf, day, theme, who)
    try:
        return check_scene(theme, json.loads(f.read_text()))
    except (OSError, ValueError, TypeError, AttributeError, Unavailable):
        pass  # no cache, or a damaged one: ask again (and overwrite it)
    res = direct(theme, who, model, timeout)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(res, indent=2))
    except OSError:
        pass  # a cache that can't be written only costs a second call
    return res


# ---------------------------------------------------------------- ideas

def ideas_file(conf: dict) -> Path:
    return Path(conf["output_dir"]).expanduser() / "ideas.json"


def load_ideas(conf: dict) -> list[str]:
    try:
        data = json.loads(ideas_file(conf).read_text())
        return [i["theme"] for i in data if isinstance(i, dict) and isinstance(i.get("theme"), str) and i["theme"].strip()]
    except (OSError, ValueError, TypeError):
        return []


def make_ideas(n: int, conf: dict, model: str = "", timeout: float = TIMEOUT * 2) -> list[str]:
    """Ask for n new themes (different from [daily] themes and earlier ideas), append them to ideas.json. Returns the new ones."""
    cfg = conf.get("daily", {}).get("themes", [])
    have = list(dict.fromkeys([*([cfg] if isinstance(cfg, str) else cfg), *load_ideas(conf)]))
    examples = have[-12:]
    prompt = (f"Write {n} new themes.\nExamples of the style (and things not to repeat):\n"
              + "\n".join(f"- {t}" for t in examples))
    obj = ask(IDEAS_SYSTEM, prompt, model, timeout)
    raw = obj.get("themes")
    if not isinstance(raw, list):
        raise Unavailable("claude's answer has no list of themes")
    fresh = [" ".join(t.split()) for t in raw if isinstance(t, str) and 10 < len(t.strip()) <= 400]
    fresh = [t for t in dict.fromkeys(fresh) if t not in have][:n]
    if not fresh:
        raise Unavailable("claude gave no new themes")
    f = ideas_file(conf)
    try:
        old = json.loads(f.read_text())
        old = old if isinstance(old, list) else []
    except (OSError, ValueError):
        old = []
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")  # write + rename: a Ctrl-C half way can't leave a broken ideas.json
    tmp.write_text(json.dumps(old + [{"theme": t, "added": date.today().isoformat()} for t in fresh], indent=2,
                              ensure_ascii=False))
    tmp.replace(f)
    return fresh


def main(argv: list[str]) -> int:
    """gen ideas [-n N]"""
    import argparse
    ap = argparse.ArgumentParser(prog="gen ideas", description="Ask Claude (the CLI, your subscription) for new "
                                 "wallpaper themes and keep them in <output_dir>/ideas.json. [daily.ideas] use = true "
                                 "adds them to the theme pool.")
    ap.add_argument("-n", type=int, default=10, help="how many themes (default 10)")
    ap.add_argument("--list", action="store_true", help="print the saved ideas and exit")
    a = ap.parse_args(argv)
    conf = imagegen.load_config()
    if a.list:
        print("\n".join(load_ideas(conf)) or "no ideas yet: gen ideas -n 10")
        return 0
    model = (conf.get("daily", {}).get("ideas") or {}).get("model", "")
    try:
        fresh = make_ideas(max(1, min(a.n, 40)), conf, model)
    except Unavailable as e:
        print(f"gen ideas: {e}", file=sys.stderr)
        return 1
    print("\n".join(f"- {t}" for t in fresh))
    print(f"{len(fresh)} saved to {ideas_file(conf)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
