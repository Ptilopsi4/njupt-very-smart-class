# Contributing

## Setup

Python 3.12+, [uv](https://docs.astral.sh/uv/) and `ffmpeg` (with `ffprobe`).

```bash
git clone https://github.com/Ptilopsi4/njupt-very-smart-class
cd njupt-very-smart-class
uv sync
cp .env.example .env      # DASHSCOPE_API_KEY is only needed for real runs, not for the tests
```

## Tests and checks

```bash
uv run pytest -q                # offline: synthetic video + fake models, about a minute
uv run ruff check .
uv run ruff format --check .
```

CI runs exactly these three. The tests need no network and do not depend on a local `.env`: the models are the fakes in `tests/fakes.py`, and `tests/conftest.py` builds the videos with ffmpeg on the fly. When fixing a bug, start with a test that reproduces it.

## Code conventions

- The lint rules and the line length (120) live in `pyproject.toml`. Prompts are written one paragraph per line; their files opt out of the line-length check individually.
- New files carry an `SPDX-License-Identifier: AGPL-3.0-or-later` header like the existing ones. Code comments are written in Chinese, like the rest of the code; explain why and the constraints, not what the code already says.
- Model calls exist only behind the four interfaces in `models.py` (`AsrModel` / `VisionModel` / `TextModel` / `VideoModel`). The default implementation is `bailian.py`, assembled in `dashscope_model_set()`; to support another provider, implement the four interfaces and replace that assembly point.
- Every stage's output is a cache, and the cache key must reflect the model plus the actual input. When adding a stage, update `STAGES`, `needs_media`, the stage loop and the done-marking in `pipeline.py` as well as the `--stages` help, and add a test that an upstream change recomputes the new stage.
- The files under `src/njupt_very_smart_class/vendor/` come from [ArcticLampyrid/njupt_smartclass_downloader](https://github.com/ArcticLampyrid/njupt_smartclass_downloader): keep the original license header, note local changes at the top of the file, and leave the directory out of ruff, as it already is.

## Commits and pull requests

- One commit does one thing. Follow [Conventional Commits](https://www.conventionalcommits.org/), for example `fix(pipeline): …` or `docs(readme): …`.
- A pull request says what changed, why, and how it was verified. No debugging narrative.
- Never commit `data/`, `.env`, recordings or generated notes. Keep audio, video and descriptions of lecturers and students out of issues and pull requests as well, together with API keys, cookies and student IDs.

When reporting a problem, attach the output of `vsc --json …` and of `vsc --json status <id>`, and say which command you ran and what you expected.

Contributions are licensed under AGPL-3.0-or-later, like the project.
