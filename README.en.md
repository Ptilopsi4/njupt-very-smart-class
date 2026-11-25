# njupt-very-smart-class

English | [简体中文](README.md)

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)
[![CI](https://github.com/Ptilopsi4/njupt-very-smart-class/actions/workflows/ci.yml/badge.svg)](https://github.com/Ptilopsi4/njupt-very-smart-class/actions/workflows/ci.yml)

Turns lecture recordings from NJUPT Smart Classroom ([njupt.smartclass.cn](https://njupt.smartclass.cn/)) into **class notes with timestamps**, automatically.
A 45-minute lecture takes about 10 minutes and yields one `summary.md`; the models cost about ¥0.6.

> For personal study only. You need your own university SSO account; follow the university's and the platform's rules, and do not redistribute recordings or generated notes that contain other people's voices or images.

The notes, the command output and the logs are in Chinese, like the lectures themselves.

## What you get

`vsc run <id>` processes one lecture and writes a `summary.md`:

- **Overview**: what the lecture was about.
- **Chapter timeline**: chapters in time order (usually a dozen or so for a 45-minute lecture), each with start and end times and a few points. Times count from the start of the recording, so you can scrub to the matching moment to check.
- **Key points**: ordered by importance.
- **In-class demos**: the commands the lecturer typed on screen and their output, in time order.
- **Q&A, homework / announcements / exam tips, glossary**.
- **Key slides**: up to 10 slide screenshots, embedded in the note.
- **ASR corrections**: terms the speech recognition misheard, corrected against what is on screen (for example `echo` heard as `apple`). The original and the correction are listed side by side so you can judge for yourself.
- **For manual review**: what the models were unsure about, mostly items where the two modes disagree, with time and reason.

The note comes from two modes that are generated separately and then merged. The **transcription mode** turns the lecturer's speech into timestamped text sentence by sentence, adds a vision model's description of each slide, and has a text model summarise; the **video mode** has a multimodal model watch the whole recording and record a timeline and what happened on screen. In the merge the chapter timeline follows the video mode, the lecturer's wording follows the transcription, and commands and outputs follow the screen; where the two modes disagree nothing is forced either way, and the item goes under “for manual review”.

- **Idempotent**: every step is saved as it completes; rerunning the same command after an interruption or a failed step picks up where it stopped.
- **Fault tolerant**: an empty room, silence, no screen recording, a lecturer who brought a laptop, a recording split into segments, truncated model output. Whichever of these applies, the processing notes at the end of the note say what may be missing; when there is no transcript (no microphone, or transcription failed) the note is based on the picture only.
- **Batch**: `--title` / `--semester` process a whole course in one go; finished lessons are skipped, and one failed lesson does not stop the others.
- **AI friendly**: `--json` prints structured results with stable exit codes, and an agent skill ships with the project: `skills/very-smart-class/SKILL.md`.

## Installation

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and `ffmpeg` (with `ffprobe`).

```bash
git clone https://github.com/Ptilopsi4/njupt-very-smart-class
cd njupt-very-smart-class
uv sync
cp .env.example .env      # put your Alibaba Cloud Bailian API key here
```

Get the API key from the [Alibaba Cloud Bailian console](https://bailian.console.aliyun.com/). The models are billed per token; see “Cost and time”.

## Usage

First time:

```bash
uv run vsc doctor                                   # check ffmpeg, API key, login, disk and model configuration
uv run vsc login                                    # log in with the university SSO; enter the password when prompted
uv run vsc list --semester current --limit 20       # recordings of the current semester, newest first
uv run vsc run 3f9a1c2e                             # process one lecture: download → notes → summary.md
uv run vsc summary 3f9a1c2e --print > notes.md      # print the notes and save them as a file
```

The argument of `run` is the recording id: the `NewID` in the first column of `vsc list` (the first 8 characters are enough). The login session expires after about an hour; lessons already on disk are continued with `vsc run --local data/<course>`, which needs no login and works wherever the directory lives.

### Commands

| Command | Purpose |
|---|---|
| `vsc doctor` | Checks ffmpeg, API key, login, disk (5 GB or more free) and model configuration, and prints a fix for anything that fails |
| `vsc login` | Log in. `--username` with `--password-stdin`, or the environment variables `NJUPT_USERNAME` / `NJUPT_PASSWORD`, avoid typing the password; `--status` only checks the session; `--cookie` imports a browser session |
| `vsc semesters` | Print the academic calendar; shows what `--semester` accepts (`current` or `2026-2027-1`) |
| `vsc list` | Recordings, newest first. `--semester`, `--title`, `--start`, `--end`, `--limit`, `--order` |
| `vsc fetch <id>` | Download only. The camera track is skipped when the screen track has sound; `--full-download` gets everything, `--types VGA,Video1` selects |
| `vsc prepare <id>` | Local processing only: extract the audio and cut the slides. `--threshold`, `--max-slides`, `--max-gap` tune the slide cutting; changed values recut |
| `vsc run` | Download and process; options below |
| `vsc status <id>` | Per-stage state, errors, processing notes, generated files and token usage. No network |
| `vsc summary <id>` | Where the note is; `--print` writes it to stdout as is |

Common `vsc run` options:

- A whole course: `--semester current --title 编译原理 --limit 5`. Run `vsc list` with the same filters first to see how many there are.
- Save cost: `--no-a-path` skips the video mode and relies on transcription and slides only, at about half the cost.
- Selected stages: `--stages transcribe,vision,summarize,render`.
- Disk: `--prune-video` deletes the original mp4 files once every stage has succeeded, and keeps them if one failed; notes and the video mode can still be rerun afterwards, cutting slides again needs a new `fetch`. `--min-free 2GB` is the free-space floor before downloading.
- Models: `--model-text`, `--model-vision`, `--model-asr`, `--model-omni`, or set them in `.env`. No cache to clear: a stage whose model or input changed is recomputed, and the stages after it follow; to have finished lessons in a batch recomputed, add `--no-skip-done`.
- Others: `--omni-input Video1` (the video mode watches the camera track), `--omni-sample-seconds` (one frame every N seconds, default 5), `--concurrency` (how many slides are described at once), `--force-vision` / `--force-omni` / `--force-fuse` (recompute one stage and the stages after it). All options: `vsc run --help`.

### For agents

With `--json` (before or after the command), stdout holds one JSON document and all logs go to stderr. Exit codes:

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | Other error: the pre-flight failed, local processing failed, configuration is missing |
| `2` | Command-line usage error. JSON is not guaranteed in this case; the reason is on stderr |
| `3` | Not logged in, or the session expired |
| `4` | Model service, Smart Classroom API or network error |
| `5` | Partially done: a step failed and the rest is on disk; rerun the same command to resume |
| `6` | No match: the filter returned nothing, or the lesson had no note yet when `summary` was called |

The rest is in `skills/very-smart-class/SKILL.md`; agents read that file directly.

## How it works

Processing a lecture takes eight steps. The first two make no model calls, each of the middle steps calls one kind of model, and each step's output is the next step's input:

```
vsc run <id>
  fetch       download: the screen track VGA.mp4 (with sound); the camera track Video1.mp4 only if needed
  prepare     local: extract the audio; cut the slides out of the picture (a page on each scene change, deduplicated, one more after a long still period)
  transcribe  a speech model turns the lecturer's words into text, sentence by sentence, with times
  vision      a vision model describes each slide: title, points, whether the text is legible
  summarize   a text model writes notes per time segment, then reduces them into the lecture's notes (transcription mode)
  omni        a multimodal model watches the downscaled recording (one frame every 5 seconds) and listens (video mode)
  fuse        a text model merges the two modes
  render      writes summary.md
```

- **When the lecturer brings a laptop**, the recorded classroom PC shows nothing useful. The program samples up to 8 frames and asks the vision model whether they contain teaching material; at least 2 must, otherwise it switches to the camera track, crops the area of the projected screen and recognises that instead.
- **Models come from Alibaba Cloud Bailian only**; other providers are not implemented, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Output files

```
data/<course>/<id>/
├── summary.md            the note; for a recording split into segments, the segments' notes concatenated
├── state.json            progress: which steps are done, errors, processing notes, token usage
├── meta.json etc.        recording metadata: stream locations, start times, which carry sound
└── Seg1/                 files of each recorded segment; absent when there is only one
    ├── VGA.mp4           screen recording; Video1.mp4 is the camera. --prune-video deletes them
    ├── audio.wav         extracted audio, 16 kHz mono
    ├── transcript.json   start and end time (ms) and text of every sentence
    ├── slides/           slide screenshots, and the vision model's description of each
    ├── c_result.json     the transcription mode's notes
    ├── a_result_*.json   the video mode's notes
    ├── final.json        both modes merged; summary.md is written from it
    └── summary.md        the note of this segment
```

The remaining files (`chunks.json`, `*_640.mp4`, `transcription_raw.json`, …) are intermediate results kept for reference. The data directory defaults to `data/` in the project and can be moved with `VSC_DATA_DIR`.

## Configuration

Everything can be set as environment variables or in a `.env` in the project root (existing environment variables win).

| Variable | Required | Description |
|---|---|---|
| `DASHSCOPE_API_KEY` | yes | Alibaba Cloud Bailian API key |
| `NJUPT_USERNAME` / `NJUPT_PASSWORD` | no | supply the account non-interactively |
| `VSC_MODEL_ASR`, `VSC_MODEL_VISION`, `VSC_MODEL_TEXT`, `VSC_MODEL_OMNI` | no | other models, for transcription, slide description, note writing and merging, and the whole-video pass. `--model-*` on the command line wins |
| `VSC_DATA_DIR` | no | data directory, default `./data` |

Default models (defined in `src/njupt_very_smart_class/config.py`): transcription `qwen-audio-3.1-asr-flash-filetrans`, slide description and note writing `qwen3.7-plus`, whole video `qwen3.8-omni-flash`.

## Cost and time (for reference)

A 45-minute lecture takes about 10 minutes and ¥0.64 with the full pipeline; with `--no-a-path` about 5 minutes and ¥0.37. The table is converted from the token usage of 5 measured lectures at Alibaba Cloud Bailian's list prices in China North 2 (looked up on 2026-11-25).

| Step | Mean | Range | Notes |
|---|---|---|---|
| Transcription | ¥0.047 | 0.044–0.049 | |
| Slide description | ¥0.11 | 0.06–0.14 | about ¥0.0037 per slide, 16–38 slides per lecture; more page flips cost more |
| Note writing (transcription mode) | ¥0.21 | 0.19–0.25 | model thinking enabled |
| Whole video (video mode) | ¥0.18 | 0.165–0.207 | about 166K video + 19.7K audio tokens per lecture |
| Merging both modes | ¥0.09 | 0.08–0.10 | |
| **Total** | **≈ ¥0.64** | 0.57–0.70 | `--no-a-path` ≈ ¥0.37 |

Downloads add about 170 MB (screen track only); the camera track adds about 1.1 GB more.

Two things change the cost:

- **The lecturer's own laptop**: after switching to the camera, slides are sampled every 30 seconds, 94 frames per lecture, ¥0.35 for this step and about ¥0.86 per lecture.
- **Silent or empty-room recordings**: silence is detected locally at no model cost, but the video mode still sends the whole recording to the model; with merging that is about ¥0.17. `--no-a-path` avoids it.

## Known limitations

- `i.njupt.edu.cn` and `njupt.smartclass.cn` must be reachable.
- The models are fallible. When revising, read “for manual review” and the processing notes at the end of the note first.
- Lessons recorded in several segments are processed segment by segment; timestamps are not aligned across segments.
- Blackboard lectures (nothing on the screen, chalk only): the camera track is used and the board area is cropped, but this path has not been verified on a real recording yet.
- Recordings longer than 2 hours are not split.
- The screen and camera tracks can differ in length; when they differ by more than 5 seconds it is stated in the processing notes and the timeline may be off. The program does not correct for the difference; both tracks share one origin.
- Old recordings whose media the platform has deleted (they are still listed) fail to download; pick a recent one.

## Privacy

- The password is used once in the login request and never written to disk; only the session cookie is stored locally (`data/.session.json`). That file is your login: delete it before handing `data/` or a lesson folder to anyone.
- Recordings and notes stay on your machine. Apart from logging in, what leaves it is only what the models need to analyse: audio, slide screenshots, the downscaled recording, and the text the models produce from them. These files contain other people's voices and images.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, tests and code conventions.

## License and acknowledgements

AGPL-3.0-or-later (see [LICENSE](LICENSE)). Two files under `src/njupt_very_smart_class/vendor/` come from
[ArcticLampyrid/njupt_smartclass_downloader](https://github.com/ArcticLampyrid/njupt_smartclass_downloader) (AGPL-3.0-or-later, commit `17d4779`) with their original license headers kept.

This project is not affiliated with Nanjing University of Posts and Telecommunications, Hanboer or Alibaba Cloud; the platform and its interfaces may change at any time.
