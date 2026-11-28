# Judging generated images (#113)

A rubric so a person and the `image-judge` subagent (`.claude/agents/image-judge.md`) look at an image the same way.
Scores are **advisory**: nothing picks, rates or deletes an image from them. The point for now is to see how
closely the judge matches human assessments (see "Calibration" below). Only judge images you made or were pointed at:
other runs may be private.

## Input
One image (or a few), plus whatever applies:
- the **prompt** (text-to-image) or the **edit instruction** (edit, `gen edit`/`gen vary`);
- the **style** (`synthwave`, ...), if one was used;
- a **character ref** image, when a character should appear.

Look at the full-size file. A downscaled preview or a contact sheet hides small hands, text and seams, so the judge
sets `confidence` to `low` when that is all it had (a grid cell is a preview). To zoom, pass crops of the image as extra files.

## What is scored

### 1. Artifacts (a list, each with a severity)
Anything that looks wrong as a picture, apart from whether it matches the prompt.

| kind | examples |
|---|---|
| `anatomy` | wrong number of fingers, limbs, eyes; fused or bent joints; odd faces |
| `text` | garbled or misspelt lettering, signs that should read something and don't |
| `duplicate` | a second door, doubled window, cloned person or object |
| `melt` | objects that blend into each other or into the background |
| `seam` | tiling lines, tile boundaries, abrupt changes in light or detail |
| `noise` | grain, banding, speckle, smeared patches |
| `blank` | all black, all one colour, nothing drawn |
| `off_model` | the character differs from the ref (see character match) |
| `other` | anything else, with a note |

Severity: **1** only visible when zoomed in; **2** noticeable at normal size; **3** ruins the image.
Count what you can see, don't guess at what might be hidden. No artifacts = an empty list. Don't log vague "slightly odd geometry"
notes as artifacts, and an odd but cleanly drawn thing the prompt didn't ask for (a bed in an arcade) is `extra` in instruction following,
not an artifact.
One artifact per distinct problem; the same garbled sign on three cabinets is one `text` artifact, with `where` saying so.

### 2. Instruction following
`score` 1 to 5: how much of the prompt or instruction is in the picture. `missed` lists things asked for and absent
or wrong ("neon sign says GAME ON"); `extra` lists clear additions nobody asked for. Not applicable (no prompt) = `null`.

### 3. Character match
Only when a ref is given, else `null`. `score` 1 to 5 against the ref (silhouette, colours, distinguishing features such as
tentacle count); `notes` names the differences.

### 4. Style match
`score` 1 to 5 for how well the image looks like the named style or the prompt's stated look (for example synthwave: neon, magenta/cyan,
80s). No style and nothing implied = `null`.

### 5. Overall and confidence
`overall` is 1 to 5 in 0.5 steps, a judgement of the picture as a whole (a severity-3 artifact caps it at 2; garbled
incidental text is a smaller cost than a melted face). `confidence` is `high` (full-size view, clear call), `medium` or `low`
(preview only, or very busy scene).

Score scale for all 1-5 fields: 5 excellent, 4 good with small misses, 3 mixed, 2 poor, 1 fails.

## Output
Return JSON only, one object per image, as a list when several images were given:

```json
{
  "image": "docs/model-comparison/arcade-final.jpg",
  "artifacts": [
    {"kind": "text", "where": "cabinet marquees, left", "severity": 1, "note": "unreadable lettering"}
  ],
  "instruction_following": {"score": 4, "missed": ["a claw machine"], "extra": []},
  "character_match": null,
  "style_match": {"score": 4, "style": "synthwave"},
  "overall": 3.5,
  "confidence": "medium"
}
```

Fields: `image`, `artifacts[]` (`kind` from the table, `where`, `severity` 1-3, `note`), `instruction_following`
(`score`, `missed[]`, `extra[]`) or `null`, `character_match` (`score`, `notes`) or `null`, `style_match` (`score`, `style`) or `null`,
`overall`, `confidence`. `tests/test_judging.py` checks that this example matches the field list.

## Using it
Ask in a session: "judge these images" (give paths, the prompt, style and ref). In Claude Code the agent is picked up from
`.claude/agents/`, in Þór's local sessions too. Cloud threads can only judge images they can read (project folder, or via `gen tunnel`).
Next steps, not built: writing `judge.json` next to `run.json` and showing scores on the contact sheet.

## Calibration (#113 slice 1, 2026-11-28)
First run: the 36 cells of the three #102 grids (`docs/model-comparison/*-final.jpg`: klein @8, klein3 @24, qwen @25, four seeds), cut out of the
grids and doubled in size, so every call was made from a ~480 px preview (confidence `medium`/`low`). The originals are on tulu.

Þór has not recorded per-image counts yet, only the notes in `docs/model-comparison.md` marked (Þór), so there is no numeric agreement
to report. Against what can be checked:

| Þór's note | Judge |
|---|---|
| Cabin klein3 seed 1 has no second door at 24 steps (it does at 16 and 32) | No `duplicate` on that cell. Agrees (a negative only). |
| No huge difference between the models at first look | Mean overall (1-5): cabin klein 4.5, klein3 4.0, qwen 3.5; arcade 3.3, 3.5, 3.5; market 4.1, 4.0, 4.4. Mostly within half a point except the cabin, where qwen's lit-window misses and the porch melt cost it. Broadly agrees. |
| Qwen's cabins vary most seed to seed (farmhouse, red cabin, veranda) | Flags a "large white house" and the missing cozy glow in `missed`; it sees the variety but not as one finding. Partly agrees. |
| (Claude's read, not Þór) klein garbles secondary sign text; klein3's "GAME ON" is cleaner but cabinet text garbles | `text` artifacts on all four klein arcade cells (`GAME ONE`, `AINES`); klein3 cabinet marquees flagged on 2 of 4. Agrees. |

Limits seen: the first pass padded the list with severity 1 `other` notes on clean cabins and called odd extras (beds, a tub) artifacts, so the
rubric now says not to (this PR). Hands in crowds were flagged at severity 1 on nearly every market cell, which is
noise at preview size. Next step for a real number: Þór's per-image tallies (even a handful) for the arcade and market grids, then
re-run on the full-size images through `gen tunnel` and compare counts and severities.
