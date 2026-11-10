"""gen daily (#3): make one new wallpaper and set it as the Windows desktop background.

Settings live in config.toml under [daily]:
  preset   the wallpaper preset (wallpaper / wallpaper_ultrawide)
  count    images to make; the first becomes the wallpaper (the rest stay in the run folder)
  cast     characters (gen char) in every daily; their descriptions lead the prompt, like a project pass
  style    text added after the theme (optional)
  ref, ref_prompt  for an edit-only preset (wallpaper_ref): the image that shows the cast (CHARACTER:REF or a
           path; default the first cast character's first ref), and text added to the prompt about it
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
    d = {"preset": "wallpaper_ref", "count": 1, "cast": [], "style": "", "no_repeat_days": 7, "themes": [],
         "dates": {}, "months": {}, "weekdays": {}, "ref": "", "ref_prompt": "", **conf.get("daily", {})}
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
    if (d["ref"] or "").strip() and "edit_workflow" not in p:
        raise ValueError(f"[daily] ref is set, but preset '{d['preset']}' can't use a ref (no edit_workflow)")
    # an edit-only preset always draws the cast from a ref image; one that does both does when ref is set
    d["uses_ref"] = "workflow" not in p or bool((d["ref"] or "").strip())
    return d


def find_ref(d: dict) -> Path:
    """The reference image for an edit preset: [daily] ref (CHARACTER:REF or a path), else the first cast
    character's first ref."""
    import characters
    ref = (d["ref"] or "").strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*:[A-Za-z0-9][A-Za-z0-9_-]*", ref):
        name, _, r = ref.partition(":")
    elif ref:
        path = imagegen.to_linux_path(ref)
        if not path.is_absolute():  # the scheduled task runs from anywhere: relative = under output_dir
            path = Path(imagegen.load_config()["output_dir"]).expanduser() / path
        if not path.is_file():
            raise FileNotFoundError(f"[daily] ref: no file {path}")
        return path
    else:
        cast = _pool(d["cast"])
        if not cast:
            raise ValueError(f"[daily] preset '{d['preset']}' needs a reference image: set cast or ref")
        name, r = cast[0], None
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


def compose(d: dict, theme: str) -> tuple[str, dict]:
    """The prompt (cast descriptions, theme, style; joined like a project pass) and {character: version}."""
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
    return projects.full_prompt({"prompt": theme, "snippets": {}}, d["style"], cast_text), used


def plan(day: date | None = None, conf: dict | None = None) -> dict:
    day = day or date.today()
    d = settings(conf)
    conf = conf or imagegen.load_config()
    theme, where = pick_theme(d, day, recent_themes(conf, day, max(1, int(d["no_repeat_days"])) - 1))
    prompt, used = compose(d, theme)
    ref = None
    if d["uses_ref"]:
        ref = str(find_ref(d))
        if d["ref_prompt"].strip():
            prompt = f"{prompt}. {d['ref_prompt'].strip()}"
    return {"date": day.isoformat(), "theme": theme, "from": where, "prompt": prompt, "cast": used,
            "preset": d["preset"], "count": max(1, int(d["count"])), "ref": ref}


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
                                source_image=p["ref"], edit=p["ref"] is not None)
        if not run["images"]:
            raise RuntimeError(f"the run made no images ({run['run_dir']})")
        rf = Path(run["run_dir"]) / "run.json"
        meta = json.loads(rf.read_text())
        meta["daily"] = run["daily"] = {k: p[k] for k in ("date", "theme", "from", "cast", "ref")}
        rf.write_text(json.dumps(meta, indent=2))
        image = run["images"][0]["path"]
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
        print(f"preset:  {p['preset']} x{p['count']}")
        if p["ref"]:
            print(f"ref:     {p['ref']}")
        if p["cast"]:
            print("cast:    " + ", ".join(f"{c} v{v}" for c, v in p["cast"].items()))
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
