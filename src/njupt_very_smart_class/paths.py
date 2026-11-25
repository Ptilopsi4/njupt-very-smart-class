# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def data_dir() -> Path:
    root = Path(os.environ.get("VSC_DATA_DIR") or PROJECT_ROOT / "data")
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_file() -> Path:
    return data_dir() / ".session.json"


def sanitize_filename(name: str, max_len: int = 80) -> str:
    cleaned = _INVALID.sub("_", name).strip(" .")
    return cleaned[:max_len] or "untitled"


def lesson_dir(course_name: str, new_id: str) -> Path:
    path = data_dir() / sanitize_filename(course_name) / new_id
    path.mkdir(parents=True, exist_ok=True)
    return path
