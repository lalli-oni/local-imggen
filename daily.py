"""gen daily (#3): make one new wallpaper and set it as the Windows desktop background.

Settings live in config.toml under [daily]:
  preset   the wallpaper preset: wallpaper_cast by default (#52: the scene from text, then the cast put into
           it from their ref images); wallpaper_ref (the first ref drawn into a scene); wallpaper from text only
  count    images to make; the first becomes the wallpaper (the rest stay in the run folder)
  cast     characters (gen char) in every daily. Text-only presets: their descriptions lead the prompt, like a
           project pass. With a ref (wallpaper_ref) the ref shows them and the descriptions are left out
  style    the look added after the theme: a [styles.NAME] (or several, "a, b"), or a phrase of literal
           text; "" = the preset's own `styles`. With a ref the text goes into ref_prompt's {scene};
           text-only presets get the styles from generate() (with their negative_extra)
  ref, ref_prompt  for an edit-only preset (wallpaper_ref): the image that shows the cast (CHARACTER:REF or a
           path; default the first cast character's first ref), and the prompt template ({scene}, {name}).
           For a cast preset, ref may be a list: one image per character (default each cast character's first ref)
  scene_prompt, stand_in, cast_prompt  for a cast preset (wallpaper_cast): the scene pass draws {stand_ins}
           (one stand_in per character, "on the left", ...) doing the theme, then the cast pass replaces each with
           its character ({replace}: "Replace the ... in image 1 with tulu, the character from image 2."). With
           stand_in = "" the scene has nobody in it and the cast pass adds the characters instead
  props, props_per_day  [daily.props] category = [items]: each day props_per_day items, from as many different
           categories, picked by the date, are added to the theme ("..., with a boombox and a DeLorean")
  themes   the scene for the day, picked at random (by the date: a re-run that day gets the same theme,
           with a new seed) from the ones the past daily runs haven't used in the last no_repeat_days
  no_repeat_days  at least this many days between two uses of a theme (default 7)
  [daily.dates]    "MM-DD" (every year) or "YYYY-MM-DD" (one year: movable feasts) = a theme or a list
  [daily.months]   jan..dec = the same for a month
  [daily.weekdays] mon..sun = the same for a weekday
                   The most specific entry for a day replaces `themes`: YYYY-MM-DD, MM-DD, month, weekday.

Runs land in the normal output folder (<date>/<time>-<preset>-daily-<theme>/), so `gen recent` lists them;
run.json gets a "daily" entry (date, theme, character versions). Every attempt adds one line to
<output_dir>/daily.log. On any failure (ComfyUI down, a cast character missing, ...) the wallpaper is left
alone, the reason goes to the log and stderr, and the exit code is 1.

Scheduling (Windows Task Scheduler, starts WSL if needed): windows/daily-task.sh on [--at HH:MM].
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

import imagegen

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DEFAULT_WAIT = 600  # seconds for ComfyUI to come up: a scheduled run may have just started WSL


def settings(conf: dict | None = None) -> dict:
    conf = conf or imagegen.load_config()
    d = {"preset": "wallpaper_cast", "count": 1, "cast": [], "style": "", "no_repeat_days": 7, "themes": [],
         "dates": {}, "months": {}, "weekdays": {}, "ref": "", "ref_prompt": "", "scene_prompt": "",
         "stand_in": STAND_IN, "cast_prompt": "", "props": {}, "props_per_day": 0, **conf.get("daily", {})}
    if not isinstance(d["props"], dict) or not all(isinstance(v, list) for v in d["props"].values()):
        raise ValueError("[daily.props]: use category = [\"item\", ...]")
    for key, names in (("months", MONTHS), ("weekdays", DAYS)):
        bad = [k for k in d[key] if k not in names]
        if bad:
            raise ValueError(f"[daily.{key}]: unknown {', '.join(bad)} (use {', '.join(names)})")
    bad = [k for k in d["dates"] if not _date_key_ok(k)]
    if bad:
        raise ValueError(f"[daily.dates]: {', '.join(bad)} is not \"MM-DD\" or \"YYYY-MM-DD\"")
    if d["preset"] not in conf["presets"]:
        raise ValueError(f"[daily] preset '{d['preset']}' is not a preset ({', '.join(conf['presets'])})")
    p = conf["presets"][d["preset"]]
    if "workflow" not in p and "edit_workflow" not in p:
        raise ValueError(f"[daily] preset '{d['preset']}' has no workflow")
    d["refs"] = _pool(d["ref"])
    d["casting"] = imagegen.is_cast_preset(p)
    if d["refs"] and "edit_workflow" not in p and not d["casting"]:
        raise ValueError(f"[daily] ref is set, but preset '{d['preset']}' can't use a ref (no edit_workflow)")
    if len(d["refs"]) > 1 and not d["casting"]:
        raise ValueError(f"[daily] ref: preset '{d['preset']}' takes one ref image, not {len(d['refs'])}")
    raw = str(d["style"] or "").strip()
    names = imagegen.split_styles(raw) if raw else imagegen.check_styles(p.get("styles", []), conf)
    known = imagegen.list_styles(conf)
    if names and all(n in known for n in names):
        d["styles"], d["style"] = names, imagegen.style_text(names, conf)
    elif any(n in known for n in names) or (raw and " " not in raw):  # a typo, or names mixed with text
        raise ValueError(f"[daily] style '{raw}': use style names ({', '.join(known)}), or a phrase of plain text")
    else:  # literal text (the old way)
        d["styles"] = []
    # an edit-only preset always draws the cast from a ref image; one that does both does when ref is set
    d["uses_ref"] = not d["casting"] and ("workflow" not in p or bool(d["refs"]))
    return d


def find_ref(d: dict) -> Path:
    """The reference image for an edit preset: [daily] ref (CHARACTER:REF or a path), else the first cast
    character's first ref."""
    if d["refs"]:
        return _resolve_ref(d["refs"][0])[1]
    cast = _pool(d["cast"])
    if not cast:
        raise ValueError(f"[daily] preset '{d['preset']}' needs a reference image: set cast or ref")
    return _char_ref(cast[0])


def find_refs(d: dict) -> list[tuple[str | None, Path]]:
    """A cast preset's ref images as (character or None, path): [daily] ref's entries, else each cast
    character's first ref."""
    cast = _pool(d["cast"])
    if d["refs"]:  # a plain path is the cast character in the same place, for the edit's wording
        found = [_resolve_ref(r) for r in d["refs"]]
        return [(name or (cast[k] if k < len(cast) else None), path) for k, (name, path) in enumerate(found)]
    if not cast:
        raise ValueError(f"[daily] preset '{d['preset']}' needs the cast's ref images: set cast or ref")
    return [(name, _char_ref(name)) for name in cast]


def _resolve_ref(ref: str) -> tuple[str | None, Path]:
    """CHARACTER:REF -> (character, its ref file); a path (relative = under output_dir) -> (None, path)."""
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*:[A-Za-z0-9][A-Za-z0-9_-]*", ref):
        name, _, r = ref.partition(":")
        return name, _char_ref(name, r)
    path = imagegen.to_linux_path(ref)
    if not path.is_absolute():  # the scheduled task runs from anywhere: relative = under output_dir
        path = Path(imagegen.load_config()["output_dir"]).expanduser() / path
    if not path.is_file():
        raise FileNotFoundError(f"[daily] ref: no file {path}")
    return None, path


def _char_ref(name: str, r: str | None = None) -> Path:
    """A character's ref image file: r, or its first ref."""
    import characters
    refs = characters.state_at(characters.load(name))["refs"]
    if not refs:
        raise ValueError(f"character '{name}' has no ref image: gen char set {name} --ref NAME=PATH")
    r = r or next(iter(refs))
    if r not in refs:
        raise ValueError(f"character '{name}' has no ref '{r}' (refs: {', '.join(refs)})")
    return characters.char_dir(name) / refs[r]["file"]


def _date_key_ok(k: str) -> bool:
    try:
        date.fromisoformat(k if len(k) == 10 else f"2000-{k}")  # 2000: a leap year, so 02-29 is fine
        return len(k) in (5, 10)
    except ValueError:
        return False


def _pool(value) -> list[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    return [t.strip() for t in items if isinstance(t, str) and t.strip()]


def recent_themes(conf: dict, day: date, days: int) -> dict[str, date]:
    """{theme: the last day it was used} over the `days` days before `day`, from past daily runs' run.json."""
    out: dict[str, date] = {}
    root = Path(conf["output_dir"]).expanduser()
    for back in range(days, 0, -1):  # oldest first, so a later use overwrites an earlier one
        d = day - timedelta(days=back)
        for f in sorted((root / d.isoformat()).glob("*/run.json")):
            try:
                theme = (json.loads(f.read_text()).get("daily") or {}).get("theme")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(theme, str):
                out[theme] = d
    return out


def pick_theme(d: dict, day: date, recent: dict[str, date] | None = None) -> tuple[str, str]:
    """(theme, where it came from) for a day: its date's list, else its month's, weekday's, else `themes`.

    recent: {theme: last day used}. A theme used within the last no_repeat_days - 1 days is skipped
    (so no_repeat_days = 7 means at least 7 days between uses). If that rules out the whole pool,
    the theme(s) used longest ago are picked from. The date decides among the rest, so re-running
    on the same day gets the same theme (with a new seed)."""
    recent = recent or {}
    floor = max(1, int(d["no_repeat_days"]))
    for where, pool in ((f"dates.{day.isoformat()}", d["dates"].get(day.isoformat())),
                        (f"dates.{day:%m-%d}", d["dates"].get(f"{day:%m-%d}")),
                        (f"months.{MONTHS[day.month - 1]}", d["months"].get(MONTHS[day.month - 1])),
                        (f"weekdays.{DAYS[day.weekday()]}", d["weekdays"].get(DAYS[day.weekday()])),
                        ("themes", d["themes"])):
        pool = sorted(set(_pool(pool)))  # only the date decides, not how config.toml lists them
        if not pool:
            continue
        free = [t for t in pool if t not in recent or (day - recent[t]).days >= floor]
        if not free:
            oldest = min(recent[t] for t in pool)
            free = [t for t in pool if recent[t] == oldest]
        return random.Random(f"{where}:{day.isoformat()}").choice(free), where
    raise ValueError("[daily] has no themes: add themes = [\"...\"] to config.toml")


def pick_props(d: dict, day: date) -> list[str]:
    """props_per_day items for a day, each from a different [daily.props] category (the date decides)."""
    cats = sorted(c for c, items in d["props"].items() if items)
    n = min(max(0, int(d["props_per_day"])), len(cats))
    rng = random.Random(f"props:{day.isoformat()}")
    return [rng.choice(sorted(d["props"][c])) for c in rng.sample(cats, n)]


def with_props(theme: str, props: list[str]) -> str:
    if not props:
        return theme
    items = props[0] if len(props) == 1 else ", ".join(props[:-1]) + " and " + props[-1]
    return f"{theme.rstrip().rstrip(',')}, with {items} in the scene"


def compose(d: dict, theme: str) -> tuple[str, dict]:
    """The prompt (cast descriptions, theme, a literal style; joined like a project pass) and {character: version}.
    Named styles are left to generate() (plan's gen_styles), so their negative_extra applies too."""
    import characters
    import projects
    used, cast_text = {}, []
    for name in _pool(d["cast"]):
        ch = characters.load(name)  # FileNotFoundError names the known characters
        desc = characters.state_at(ch)["description"]
        if not desc.strip():
            raise ValueError(f"character '{name}' has no description yet: gen char set {name} --desc '...'")
        used[name] = ch["version"]
        cast_text.append(desc)
    literal = "" if d["styles"] else d["style"]
    return projects.full_prompt({"prompt": theme, "snippets": {}}, literal, cast_text), used


def ref_prompt(d: dict, theme: str, used: dict) -> str:
    """The prompt when the ref image shows the character: the scene plus [daily] ref_prompt, without the
    cast's text descriptions (written for text-only models, they say things like "plain white background,
    standing", and with a ref the model then copies the ref's pose and white background).
    ref_prompt may use {scene} and {name}; without {scene}, the scene goes first."""
    scene = ", ".join(t.strip().strip(",") for t in (theme, d["style"]) if t and t.strip())
    import characters
    name = characters.load(next(iter(used))).get("title") or next(iter(used)) if used else "the character"
    tmpl = (d["ref_prompt"] or "").strip()
    if not tmpl:
        return scene
    if "{scene}" not in tmpl:
        tmpl = "{scene}. " + tmpl
    try:
        return tmpl.format(scene=scene, name=name)
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"[daily] ref_prompt: only {{scene}} and {{name}} can be filled in ({e})") from None


STAND_IN = "a small cute cartoon creature"
PLACES = ("on the left", "on the right", "in the middle", "in the foreground", "in the background")
SCENE_PROMPT = ("A wide, finished wallpaper image that fills the whole frame: {stand_ins}, {scene}. The only text "
                "in the image is the sign text named in the scene, spelled exactly as given; no other signs, labels "
                "or writing. Detailed background, scenery and lighting everywhere, no white or empty background, "
                "no border")  # no full stop: generate() adds the preset's suffix and the style after it
CAST_PROMPT = ("{replace} Each character takes over the place, pose, size and action of the one it replaces and is "
               "lit like the rest of the scene, with exactly the same design, colours and proportions as in its "
               "reference image. Keep everything else in image 1 exactly the same: layout, background, lighting, "
               "colours and text.")


def _name(name: str | None) -> str:
    import characters
    return (characters.load(name).get("title") or name) if name else "the character"


def cast_prompts(d: dict, theme: str, names: list[str | None]) -> tuple[str, str]:
    """(scene prompt, cast prompt) for a cast preset: the scene pass draws a stand-in per character (unless
    stand_in is ""), the cast pass replaces each with its character from image 2, 3, ...; {scene} = theme plus
    a literal style (named styles come from generate())."""
    scene = ", ".join(t.strip().strip(",") for t in (theme, "" if d["styles"] else d["style"]) if t and t.strip())
    stand_in = str(d["stand_in"] or "").strip()
    n = len(names)
    places = [""] if n == 1 else [f" {PLACES[k]}" if k < len(PLACES) else f" number {k + 1}" for k in range(n)]
    if stand_in:
        stand_ins = f"{stand_in}{places[0]}" + "".join(f", another one{pl}" for pl in places[1:])
        bare = re.sub(r"^(an?|the)\s+", "", stand_in, flags=re.IGNORECASE)
        replace = " ".join(f"Replace the {bare}{places[k]} in image 1 "
                           f"with {_name(nm)}, the character from image {k + 2}." for k, nm in enumerate(names))
    else:
        stand_ins = "nobody in it yet"
        replace = " ".join(f"Add {_name(nm)}, the character from image {k + 2}, into the scene from image 1, "
                           f"busy with what happens there." for k, nm in enumerate(names))
    out = []
    for key, default, fills in (("scene_prompt", SCENE_PROMPT, {"scene": scene, "stand_ins": stand_ins}),
                                ("cast_prompt", CAST_PROMPT, {"replace": replace, "scene": scene})):
        tmpl = str(d[key] or "").strip() or default
        try:
            out.append(tmpl.format(**fills))
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError(f"[daily] {key}: only {', '.join('{' + k + '}' for k in fills)} can be filled in "
                             f"({e})") from None
    return out[0], out[1]


def plan(day: date | None = None, conf: dict | None = None) -> dict:
    day = day or date.today()
    d = settings(conf)
    conf = conf or imagegen.load_config()
    theme, where = pick_theme(d, day, recent_themes(conf, day, max(1, int(d["no_repeat_days"])) - 1))
    props = pick_props(d, day)
    scene = with_props(theme, props)
    prompt, used = compose(d, scene)
    ref, refs, cast_prompt = None, [], None
    if d["uses_ref"]:
        ref = str(find_ref(d))
        prompt = ref_prompt(d, scene, used)
    elif d["casting"]:
        found = find_refs(d)
        for name, _ in found:  # a CHAR:REF entry for a character outside the cast still shows it
            if name and name not in used:
                import characters
                used[name] = characters.load(name)["version"]
        refs = [str(path) for _, path in found]
        prompt, cast_prompt = cast_prompts(d, scene, [name for name, _ in found])
    return {"date": day.isoformat(), "theme": theme, "from": where, "props": props, "prompt": prompt, "cast": used,
            "styles": d["styles"], "gen_styles": [] if ref else d["styles"],  # with a ref: in the prompt
            "post": imagegen.style_post(d["styles"], conf) if ref else [],  # ...so their post steps come from here
            "preset": d["preset"], "count": max(1, int(d["count"])), "ref": ref,
            "refs": refs, "cast_prompt": cast_prompt}


def log_file(conf: dict | None = None) -> Path:
    conf = conf or imagegen.load_config()
    return Path(conf["output_dir"]).expanduser() / "daily.log"


def _log(line: str, conf: dict | None) -> None:
    """stderr, and a line in daily.log. Never raises: it also reports a broken config.toml."""
    imagegen.log(line)
    try:
        f = log_file(conf) if conf else Path(os.environ.get("IMAGEGEN_OUTPUT_DIR")
                                             or tomllib.loads((imagegen.ROOT / "config.toml").read_text())
                                             ["output_dir"]).expanduser() / "daily.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except Exception as e:  # a bad config.toml, or a missing output folder drive
        imagegen.log(f"warning: could not write the daily log: {e}")


def run(day: date | None = None, wait: float = DEFAULT_WAIT, set_wallpaper: bool = True) -> dict:
    """Make today's wallpaper and set it. Raises (wallpaper untouched) on any failure; logs either way."""
    conf = None
    try:
        conf = imagegen.load_config()
        p = plan(day, conf)
        imagegen.Comfy(conf["comfy_url"]).wait_ready(timeout=wait)  # generate() then finds it up
        run = imagegen.generate(p["prompt"], p["preset"], p["count"], label=f"daily-{imagegen.slug(p['theme'], 30)}",
                                source_image=p["ref"], edit=p["ref"] is not None,
                                styles=p["gen_styles"], post=p["post"], refs=p["refs"] or None,
                                cast_prompt=p["cast_prompt"])
        if not run["images"]:
            raise RuntimeError(f"the run made no images ({run['run_dir']})")
        rf = Path(run["run_dir"]) / "run.json"
        meta = json.loads(rf.read_text())
        meta["daily"] = run["daily"] = {k: p[k] for k in ("date", "theme", "from", "props", "cast", "ref", "refs",
                                                          "styles")}
        rf.write_text(json.dumps(meta, indent=2))
        good = [im for im in run["images"] if not im.get("blank")]
        if not good:  # a black wallpaper is worse than yesterday's
            raise RuntimeError(f"every image came out blank (black) ({run['run_dir']})")
        image = good[0]["path"]
        run["wallpaper"] = imagegen.set_wallpaper(image) if set_wallpaper else None
    except Exception as e:
        _log(f"FAILED, wallpaper unchanged: {type(e).__name__}: {e}", conf)
        raise
    _log(f"ok: {p['theme']!r} -> {run['wallpaper'] or image}" + ("" if set_wallpaper else " (not set)"), conf)
    return run


USAGE_EPILOG = """Settings: [daily] in config.toml. Log: <output_dir>/daily.log.
Schedule it: windows/daily-task.sh on [--at 06:00] (off / status / run)."""


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="gen daily", epilog=USAGE_EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="Make today's wallpaper (theme and cast from [daily]) and set it.")
    ap.add_argument("--show", action="store_true", help="only print the day's theme and prompt")
    ap.add_argument("--date", type=date.fromisoformat, help="pretend it is this day (YYYY-MM-DD)")
    ap.add_argument("--no-set", action="store_true", help="generate, but leave the wallpaper as it is")
    ap.add_argument("--wait", type=float, default=DEFAULT_WAIT,
                    help=f"seconds to wait for ComfyUI to come up (default {DEFAULT_WAIT})")
    ap.add_argument("--open", action="store_true", help="open the contact sheet in the Windows browser")
    a = ap.parse_args(argv)
    if a.show:
        p = plan(a.date)
        print(f"{p['date']}: {p['theme']}  (from {p['from']})")
        print(f"preset:  {p['preset']} x{p['count']}" + (f", style {', '.join(p['styles'])}" if p["styles"] else ""))
        if p["props"]:
            print("props:   " + "; ".join(p["props"]))
        if p["ref"]:
            print(f"ref:     {p['ref']}")
        for r in p["refs"]:
            print(f"ref:     {r}")
        if p["cast"]:
            print("cast:    " + ", ".join(f"{c} v{v}" for c, v in p["cast"].items()))
        if p["cast_prompt"]:
            print(f"scene:   {p['prompt']}")
            print(f"then:    {p['cast_prompt']}")
        else:
            print(f"prompt:  {p['prompt']}")
        return 0
    try:
        r = run(a.date, a.wait, not a.no_set)
    except Exception:
        return 1  # already logged with the reason
    imagegen._print_run(r, a.open)
    if r["wallpaper"]:
        print("wallpaper set:", r["wallpaper"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
