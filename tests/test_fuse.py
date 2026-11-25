# SPDX-License-Identifier: AGPL-3.0-or-later
import json

import pytest

from njupt_very_smart_class import fuse
from tests.fakes import FakeTextModel


def test_a_view_fixes_typo_key():
    view = fuse._a_view({"result": {"homework_or_noties": ["x"], "chapters": []}})
    assert view["homework_or_notices"] == ["x"] and "homework_or_noties" not in view


def test_c_view_collects_chunk_extras():
    view = fuse._c_view(
        {"result": {"overview": "o"}, "chunks": [{"examples_or_demo": ["e1"], "homework_or_notices": ["h1"]}]}
    )
    assert view["chunk_examples"] == ["e1"] and view["chunk_homework"] == ["h1"]


def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_fuse_without_a_returns_none_and_clears_stale(tmp_path):
    _write(tmp_path / "c_result.json", {"result": {"overview": "o"}, "chunks": [], "course": "c", "model": "m"})
    _write(tmp_path / "final.json", {"stale": True})
    text = FakeTextModel("m")
    assert fuse.fuse(text, tmp_path, course_info="ci") is None
    assert text.calls == [] and not (tmp_path / "final.json").exists()


def test_fuse_requires_c(tmp_path):
    with pytest.raises(RuntimeError, match="c_result"):
        fuse.fuse(FakeTextModel("m"), tmp_path, course_info="ci")


def test_fuse_writes_and_caches(tmp_path):
    _write(
        tmp_path / "c_result.json",
        {"result": {"overview": "o"}, "chunks": [], "course": "c", "model": "cm", "segments": 3, "transcript_chars": 9},
    )
    _write(tmp_path / "a_result.json", {"result": {"homework_or_noties": ["x"]}, "model": "am", "fps": 0.2})
    text = FakeTextModel("m")
    final = fuse.fuse(text, tmp_path, course_info="ci")
    assert final["sources"] == {"c_model": "cm", "a": {"VGA": {"model": "am", "fps": 0.2}}}  # 旧 a_result.json 视为 VGA
    assert final["course"] == "c" and final["segments"] == 3
    assert (tmp_path / "final.json").exists()
    again = fuse.fuse(text, tmp_path, course_info="ci")
    assert again["sources"]["a"]["VGA"]["model"] == "am" and len(text.calls) == 1  # 命中缓存
    fuse.fuse(text, tmp_path, course_info="ci", force=True)
    assert len(text.calls) == 2
    fuse.fuse(FakeTextModel("other"), tmp_path, course_info="ci")
    assert len(FakeTextModel("x").calls) == 0  # 模型变了不命中缓存
    assert json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))["model"] == "other"


def test_load_a_results_multiple(tmp_path):

    for name, obj in (("a_result_VGA.json", {"source": "VGA"}), ("a_result_Video1.json", {"source": "Video1"})):
        _write(tmp_path / name, obj)
    assert set(fuse.load_a_results(tmp_path)) == {"VGA", "Video1"}
    (tmp_path / "a_result_VGA.json").unlink()
    assert set(fuse.load_a_results(tmp_path)) == {"Video1"}  # 旧 a_result.json 不存在时不凭空补 VGA
