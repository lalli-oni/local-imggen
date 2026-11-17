# Character stages (#68, first iteration)

How to draft a character with imagegen, for Þór and for Claude. When Þór says "let's work on tulu",
follow this. It is version 1: after trying it, write what to change in issue #68.

## Why stages

Two tulu passes showed the two extremes. Text only gave variety but went off-model (red eyes, a cape,
big bat wings). img2img from the ref at low strength gave near copies, with nothing to choose between.
So each stage generates its own way, and the board has one button per stage.

## The stages

| Stage | Goal | What its Generate runs (`[stages.*]` in config.toml) | Triage |
|---|---|---|---|
| **explore** | a rough direction | text only: the character description + project prompt, `draft` preset, 8 images | quick picks, notes on parts ("these wings") |
| **converge** | on-model variety | klein edits of one image, a different idea per image ("sitting down", "seen from the side"), 4 images | promote the best to the character ref |
| **polish** | fix details | a klein edit with one fix you type ("remove the bump on the right elbow"), 2 images | compare with the ref, promote or drop |

The stage belongs to the character (`gen char status tulu` shows it). A project works in the stage of
its first cast character. Changing the stage makes a character version but stales nothing.

Converge and polish edit the **selected image** on the board (click it), else the character's first ref.

## Working on a character

1. Open the board, served so the buttons work:
   `gen board serve tulu --open`
2. Pick the stage in the board's **stage** menu, or `gen char set tulu --stage explore`.
3. Press the stage's button (the current stage's button is highlighted). The text field:
   - explore: extra prompt text for this pass (optional)
   - converge: one idea for all images (optional; empty = the next ideas from the list)
   - polish: the fix (required)
4. Rate with 1 / 2 / 3, note with n. Notes on parts ("keep these wings") steer the next prompt.
5. Move on:
   - explore -> converge when one image has the right direction. Press its **ref** button: it becomes the
     character's first ref (a new character version; the old ref stays in the history). Then set the stage to converge.
   - converge -> polish when a pick is on-model and you'd only fix small things. Make it the ref the same way.
   - in polish, each pass fixes one thing. Promote a fix that worked to the ref; drop the rest.

Without the board: `gen project gen tulu --stage` (the current stage), `--stage converge --from 3:2`,
`--stage polish --text "remove the bump on the right elbow"`.

## For Claude

- `project_get` returns `stage` (character, stage, and what each stage's generate runs).
- Generate with `project_generate(slug, stage="current")`, or a named stage, with `source="P:N"` for
  converge/polish and `text` for the polish fix.
- Change the stage or the ref only when Þór asks: `character_update(name, note, stage=...)` or `refs={...}`.
- Base the next pass on their picks and notes, and suggest moving on (or back to explore) when the board shows it.

## Not built yet (see #68)

- Poses as extra character refs (converge and polish only) and variants (#47) with their own stages.
- Recording which base version a pose or variant came from, and "update to base vN" when the base changes.
- A mask brush for polish, and the detailer pass (#56).
