"""gen daily (#3): make one new wallpaper and set it as the Windows desktop background.

Settings live in config.toml under [daily]:
  preset   the wallpaper preset (wallpaper / wallpaper_ultrawide)
  count    images to make; the first becomes the wallpaper (the rest stay in the run folder)
  cast     characters (gen char) in every daily; their descriptions lead the prompt, like a project pass
  style    text added after the theme (optional)
  themes   the scene for the day. Each theme comes up once per round, in a shuffled order that the
           date decides, so a re-run on the same day gets the same theme (with a new seed).
  [daily.months]   jan..dec = a theme or a list: replaces `themes` for that month
  [daily.weekdays] mon..sun = the same for a weekday (a month entry wins over a weekday one)

Runs land in the normal output folder (<date>/<time>-<preset>-daily-<theme>/), so `gen recent` lists them;
run.json gets a "daily" entry (date, theme, character versions). Every attempt adds one line to
<output_dir>/daily.log. On any failure (ComfyUI down, a cast character missing, ...) the wallpaper is left
alone, the reason goes to the log and stderr, and the exit code is 1.

Scheduling (Windows Task Scheduler, starts WSL if needed): windows/daily-task.sh on [--at HH:MM].
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime
from pathlib import Path

import imagegen

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DEFAULT_WAIT = 600  # seconds for ComfyUI to come up: a scheduled run may have just started WSL


def settings(conf: dict | None = None) -> dict:
    conf = conf or imagegen.load_config()
    d = {"preset": "wallpaper", "count": 1, "cast": [], "style": "", "themes": [], "months": {}, "weekdays": {},
         **conf.get("daily", {})}
    for key, names in (("months", MONTHS), ("weekdays", DAYS)):
        bad = [k for k in d[key] if k not in names]
        if bad:
            raise ValueError(f"[daily.{key}]: unknown {', '.join(bad)} (use {', '.join(names)})")
    if d["preset"] not in conf["presets"]:
        raise ValueError(f"[daily] preset '{d['preset']}' is not a preset ({', '.join(conf['presets'])})")
    return d


def _pool(value) -> list[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    return [t.strip() for t in items if isinstance(t, str) and t.strip()]


def pick_theme(d: dict, day: date) -> tuple[str, str]:
    """(theme, where it came from) for a day: the month's list, else the weekday's, else `themes`."""
    for where, pool in ((f"months.{MONTHS[day.month - 1]}", d["months"].get(MONTHS[day.month - 1])),
                        (f"weekdays.{DAYS[day.weekday()]}", d["weekdays"].get(DAYS[day.weekday()])),
                        ("themes", d["themes"])):
        pool = _pool(pool)
        if pool:
            n, k = len(pool), day.toordinal()
            order = sorted(pool)  # only the date decides the order, not how config.toml lists them
            random.Random(f"{where}:{k // n}").shuffle(order)
            return order[k % n], where
    raise ValueError("[daily] has no themes: add themes = [\"...\"] to config.toml")


def compose(d: dict, theme: str) -> tuple[str, dict]:
    """The prompt (cast descriptions, theme, style) and {character: version} it used."""
    import characters
    used, parts = {}, []
    for name in d["cast"]:
        ch = characters.load(name)  # FileNotFoundError names the known characters
        desc = characters.state_at(ch)["description"]
        if not desc.strip():
            raise ValueError(f"character '{name}' has no description yet: gen char set {name} --desc '...'")
        used[name] = ch["version"]
        parts.append(desc)
    parts += [theme, d["style"]]
    return ", ".join(p.strip().strip(",") for p in parts if p and p.strip()), used


def plan(day: date | None = None) -> dict:
    day = day or date.today()
    d = settings()
    theme, where = pick_theme(d, day)
    prompt, used = compose(d, theme)
    return {"date": day.isoformat(), "theme": theme, "from": where, "prompt": prompt, "cast": used,
            "preset": d["preset"], "count": max(1, int(d["count"]))}


def log_file() -> Path:
    return Path(imagegen.load_config()["output_dir"]).expanduser() / "daily.log"


def _log(line: str) -> None:
    imagegen.log(line)
    try:
        f = log_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError as e:
        imagegen.log(f"warning: could not write the daily log: {e}")


def run(day: date | None = None, wait: float = DEFAULT_WAIT, set_wallpaper: bool = True) -> dict:
    """Make today's wallpaper and set it. Raises (wallpaper untouched) on any failure; logs either way."""
    try:
        p = plan(day)
        conf = imagegen.load_config()
        imagegen.Comfy(conf["comfy_url"]).wait_ready(timeout=wait)
        run = imagegen.generate(p["prompt"], p["preset"], p["count"], label=f"daily-{imagegen.slug(p['theme'], 30)}")
        if not run["images"]:
            raise RuntimeError(f"the run made no images ({run['run_dir']})")
        rf = Path(run["run_dir"]) / "run.json"
        meta = json.loads(rf.read_text())
        meta["daily"] = run["daily"] = {k: p[k] for k in ("date", "theme", "from", "cast")}
        rf.write_text(json.dumps(meta, indent=2))
        image = run["images"][0]["path"]
        run["wallpaper"] = imagegen.set_wallpaper(image) if set_wallpaper else None
    except Exception as e:
        _log(f"FAILED, wallpaper unchanged: {type(e).__name__}: {e}")
        raise
    _log(f"ok: {p['theme']!r} -> {run['wallpaper'] or image}" + ("" if set_wallpaper else " (not set)"))
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
