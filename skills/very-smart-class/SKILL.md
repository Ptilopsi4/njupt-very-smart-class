---
name: very-smart-class
description: Use the vsc CLI to download lecture recordings from NJUPT Smart Classroom (智慧课堂, njupt.smartclass.cn) and turn them into class notes with timestamps (Markdown). Use when the user asks to "summarize a lecture recording / find the replay of a course / download recordings / batch-process a course this semester / check the status, cost or duration, or why a note came out incomplete". Covers login, filtering by semester or date, download, transcription + slide description + whole-video understanding, merged notes and resumable runs; every agent-facing call is non-interactive and --json output parses directly. Not for recordings from other platforms, and not for attendance or homework.
---

# very-smart-class (vsc)

Turns NJUPT Smart Classroom recordings into class notes with timestamps. Two modes run in parallel: the **transcription mode** (speech to text + slide descriptions → notes) and the **video mode** (a multimodal model watches the whole recording with sound); the results are merged into one `summary.md`. The notes, the logs and the CLI help are written in Chinese, like the lectures.

## Prerequisites (check these first)

- Repository: <https://github.com/Ptilopsi4/njupt-very-smart-class>. Run every command from the **repository root** (the directory containing `pyproject.toml`) as `uv run vsc …`; the commands below omit the `uv run` prefix. If you do not know where the repository is, ask the user instead of guessing a directory.
- Environment: Python 3.12+, uv and ffmpeg; `uv sync` installs the dependencies; the `.env` in the repository root must contain `DASHSCOPE_API_KEY` (Alibaba Cloud Bailian). `vsc --json doctor` checks each item and returns a fix for each (`checks[].fix`).
- Network: `i.njupt.edu.cn` and `njupt.smartclass.cn` must be reachable, usually from the campus network or a VPN.
- Session: the login session expires after about an hour. `doctor`, `list`, `semesters`, `fetch` and `run` (except `--local`) need the network; `prepare`, `status`, `summary` and `run --local` only read local files.
- Duration: a full run takes about 10 minutes per lecture plus about 170 MB of download. Give the execution tool a timeout of 20 minutes or more, or start the command in the background (nohup, a background task, …) and poll `status`. Killing the process loses nothing: downloads resume, and every stage already written to disk is skipped when the same command is rerun.

## Flow (fixed order, every step has JSON output)

```
doctor ──→ list --json ──→ run --json ──→ status --json ──→ summary --print
 checks     pick scope      process       progress, errors     read result
```

1. `vsc --json doctor`: includes the session check. Exit code 3 means the session expired; have the user log in, then continue from here. Fix anything else that fails as `fix` says.
2. `vsc --json list …`: pick the recordings to process; the filters are in the reference table. If several match and the user wants one, show `items[]` and let them choose.
3. `vsc --json run <new_id>`; for a batch, `vsc --json run --semester current --title <course> --limit N`.
4. `vsc --json status <new_id>`: read `verdict`, `notes` and `errors`.
5. `vsc --json summary <new_id> --print`: `markdown` is the note itself, `summary` is the file path.

## Output contract

- With `--json` (before or after the command) **stdout holds exactly one JSON document** and all progress logs go to stderr. Without `--json`, stdout is a table for humans.
- Success: `"ok": true` plus the command's own fields. Failure: `"ok": false` plus `exit_code`, and for most commands `error`; when `list` or a batch matches nothing there is no `error`, only `count: 0`. Usage errors (exit code 2) print no JSON; the reason is on stderr.
- Exit codes: `0` success · `1` environment or local processing error (follow `fix` or `error`; do not simply rerun) · `2` usage error · `3` not logged in or session expired (a batch that loses its session midway also returns 3; the remaining lessons are not recorded as failed) · `4` upstream error: the network, the Smart Classroom API or the model service failed at the command level, for example a failed download or a list that cannot be fetched; a model call that fails midway inside a stage is not this case, it is 5. Show `error` to the user; for network errors one retry after a short wait is fine · `5` partially done: a stage failed, everything else is on disk, **rerun the same command to resume**; it returns to 0 once it succeeds. A run with a `--stages` subset only settles the stages it names: stages it leaves out keep their earlier errors and notes, so exit code 5 stays until a run includes the failed stage · `6` no match: the filter returned nothing, the lesson does not exist, or `summary` was called before the note existed.

## Command reference

| Command | Purpose | Key JSON fields |
|---|---|---|
| `doctor` | Pre-flight: ffmpeg, API key, session, disk (5 GB or more free), model configuration | `ok`, `checks[]{name, ok, detail, fix}` |
| `login --status` | Only check the session | `valid`, `visible_recordings` (recordings visible to this account), `session_file` |
| `login -u <student id> --password-stdin` / `--cookie "<Cookie header>"` | Non-interactive login. Copy the Cookie header from a logged-in browser request and treat it as a credential | `valid`, `session_file` |
| `semesters` | Academic calendar | `current{name, start, end}`, `terms[]{name, start, end}` |
| `list [--semester current] [--title X] [--start D] [--end D] [--limit N] [--order desc\|asc]` | Recordings, newest first. `--title` is a keyword; course name, teacher and classroom are all part of the title. `--start` / `--end` filter by recording date, `YYYY-MM-DD`, inclusive. Dates take precedence over `--semester`; `--semester` takes `current` or `2026-2027-1` | `count`, `items[]{new_id, title, course, teachers, classroom, start, stop, minutes}`; `start` / `stop` are ISO timestamps with a time zone |
| `fetch <id> [--types VGA,Video1] [--full-download]` | Download only (idempotent). A recording has two video tracks: VGA is the classroom PC's screen, Video1 the classroom camera; by default only the screen track is downloaded when it carries sound | `lesson_dir`, `streams[]{kind, file, bytes, start_time}` (the tracks on disk), `missing[]` (tracks the recording does not have), `skipped[]` (tracks that exist but were not downloaded because the screen track carries sound; `--full-download` gets them) |
| `prepare <id>` | Extract the wav and cut the slides (local, no model calls) | `segments[]{audio_source, slides, notes[]}` |
| `run <id> [options]` | Everything up to summary.md | `lesson_dir`, `summary`, `summary_exists`, `stages_done`, `errors`, `notes`, `tokens`, `verdict` |
| `run --semester current --title X --limit N` | Batch | `mode`, `count`, `results[]{new_id, status, verdict, summary, tokens, errors, error}`, `tokens`, `report` (path of the JSON report file) |
| `run --local <dir>` | Resume lessons that are already downloaded, **no login needed**. Pass the lesson directory or its course directory (`lesson_dir` or the directory above it), wherever it lives | same as batch |
| `status <id>` | State (no network) | `stages`, `errors`, `notes`, `tokens`, `verdict`, `summary`, `summary_exists`, `products` |
| `summary <id> [--print]` | Locate or print the note | `summary` (path), `fused` (whether both modes were merged), `bytes`, `markdown` (only with `--print`) |

Common `run` options (all groups in `vsc run --help`):

- Scope and stages: `--stages a,b,c` (default fetch,prepare,transcribe,vision,summarize,omni,fuse,render) · `--no-a-path` skips the video mode and relies on transcription and slides only: cost and time roughly halve, but the note has no in-class demos, ASR corrections or non-teaching periods, and chapter times are coarser
- Disk: `--prune-video` deletes the original mp4 files once every stage of the lesson has succeeded (a failed stage keeps them, so the rerun does not download again). Afterwards notes can still be regenerated locally and the video mode reuses the shrunken copy it kept; cutting slides again needs a new `fetch` · `--min-free 2GB` free-space floor before downloading, default 2GB
- Video mode: `--omni-input Video1` which track to watch, repeatable; values from `fetch`'s `streams[]` (VGA, Video1, Video2, Video3) · `--omni-sample-seconds 5` one frame every N seconds
- Models: `--model-asr / --model-vision / --model-text / --model-omni` override `.env`'s `VSC_MODEL_*`, which override the defaults (see “Stages and models”). A wrong model name fails at that stage's first call and shows up in `errors`
- Recomputation: `--force-vision / --force-omni / --force-fuse` discard that stage's result and recompute, together with every stage after it — **this is billed again**, so use it only when the user asks · `--no-skip-done` stops a batch from skipping finished lessons; that is how lessons are recomputed after a model change, and stages whose inputs did not change still hit the cache for free

## Stages and models

Which model each of the eight `run` stages uses, so you can tell what a model change recomputes:

| Stage | Does | Model |
|---|---|---|
| fetch, prepare | download, extract audio, cut slides | none |
| transcribe | speech to text with per-sentence times | `--model-asr`, default `qwen-audio-3.1-asr-flash-filetrans` |
| vision | describe every slide | `--model-vision`, default `qwen3.7-plus` |
| summarize | write notes per time segment, then reduce (transcription mode) | `--model-text`, default `qwen3.7-plus` |
| omni | watch the whole recording with sound (video mode) | `--model-omni`, default `qwen3.8-omni-flash` |
| fuse | merge the two modes | `--model-text` |
| render | write summary.md | none |

## Fields

- `verdict`: `ok` normal · `c_only` only the transcription mode's result (always the case with `--no-a-path`, not a failure; otherwise the video mode's result is missing, see `errors.omni`) · `no_speech` no usable speech (no microphone, or a silent track) · `asr_failed` the transcription stage failed, which is not the same as silence: read `errors.transcribe` and rerun the same command · `empty` no teaching content on screen, typical of an empty room · values combine, e.g. `empty+no_speech`.
- `notes` (processing notes, also written at the end of summary.md): a missing track, the screen track had no teaching content so the camera was used, a low speech ratio, unreadable slides, truncated model output, audio tracks of different lengths, no transcript, a failed stage. Notes belong to the stage that produced them and survive later runs that do not include that stage. Whenever a note is “incomplete”, the reason is here; read it before answering the user.
- `errors`: stage name → message. Where there are `errors` there is exit code 5; rerun with that stage included to resume. Errors of stages a run did not include are kept from the earlier run.
- `tokens`: `{model: token count}`, input plus output. In `run`, `status` and batch `results[]` it is the lesson's **running total over all runs**; the batch's top-level `tokens` is what this batch actually used. It cannot be converted into money (input and output are not split), so quote the per-lecture estimates under “Cost and duration” and use `tokens` only to spot anomalies.
- Batch `results[].status`: `ok` done · `partial` a stage failed, same as exit code 5, rerun · `skipped` finished earlier, skipped this time · `error` the lesson failed as a whole, see the `error` field.
- `products`: what each segment has on disk (audio, slide count, transcript sentences, both modes' results, the merged result, mp4 files).
- For a lesson recorded in several segments, the lesson-level `summary.md` concatenates the segments' notes under headings that name the segment, and timestamps are relative to each segment; to cite a segment, use its own `Seg*/summary.md` listed in `products`.

## Idempotence and resuming

- Every stage skips work that is already on disk: mp4, wav, slides, transcript, slide descriptions, segment notes, video-mode result, merged result.
- Caches are keyed on model plus actual input: after a model change, or when an upstream stage is recomputed, the stages after it recompute on their own. To redo the transcription, for which there is no force flag, delete `transcript.json` from the lesson directory and rerun.
- A batch skips lessons whose `state.json` marks every stage as done and whose `summary.md` exists. **That check ignores the model**: after a model change, add `--no-skip-done` to have finished lessons recomputed. To continue with the next batch, raise `--limit` and rerun the same command; it takes the first N recordings in order and skips the finished ones. If the session expires midway, the batch ends with exit code 3; after the user logs in, rerun the same batch command.
- Lessons that are already downloaded are finished with `run --local`, no login needed; log in again only to download more.

## Cost and duration (one 45-minute lecture)

- Full run ≈ 10 min and ¥0.64; `--no-a-path` ≈ 5 min and ¥0.37; when the lecturer brought a laptop and the camera has to be used, ≈ ¥0.86. Quote a batch as ¥0.64–0.86 per lecture plus download time.
- Silent or empty-room recordings: silence is detected locally, so transcription costs nothing; the video mode still runs (≈ ¥0.17).
- Downloads are about 170 MB (screen track only), plus about 1.1 GB for the camera track.
- Recomputing one part (per 45-minute lecture): fuse ¥0.09 · video mode ¥0.18, ¥0.27 with fuse · slide descriptions ¥0.11, ¥0.41 with notes and fuse · transcription ¥0.05, ¥0.35 with notes and fuse. Longer lectures scale roughly in proportion to `items[].minutes`.

## Decisions

- The user wants “one lecture”: `run <id>` on that lesson, not a batch.
- The user wants “the whole course”: `list` first, report the count, the estimated cost and the duration, and run only after confirmation; default `--limit 5`; to continue, raise `--limit` and rerun the same command.
- Exit code 3: never guess the password and never run `vsc login` without `--password-stdin` yourself (it is interactive and fails outside a terminal); have the user run `vsc login` in a terminal or supply a Cookie.
- Exit code 5: read `errors`; it is usually the network, rate limiting or truncated output, so rerun the same command. After a 429 wait 1–5 minutes before retrying; after two or three failed retries tell the user. If the video mode keeps failing, `--no-a-path` produces a transcription-mode note first and a later rerun adds the rest. When `notes` mentions disk space, tell the user that `--prune-video` or a lower `--min-free` would help and **delete files only with their consent**.
- The recording may be an empty room (`empty+no_speech`): say so; do not dress “nothing here” up as notes.
- Images in the note are paths relative to the lesson directory and will not render if the text is pasted into a chat; hand over the `summary` file path instead.
- Never print API keys, passwords or cookies and never put them in an answer. Notes contain lecture content and descriptions of the people in the room; return them to the user only.
