# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re
import shutil
import subprocess
import types
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from njupt_very_smart_class import lesson, media
from njupt_very_smart_class.cli import app

runner = CliRunner()


def _make_lesson(data_root: Path, new_id: str, video: Path, *, vga_audio: bool = True) -> Path:
    d = data_root / "高数(上)" / new_id
    d.mkdir(parents=True)
    shutil.copy(video, d / "Video1.mp4")
    if vga_audio:
        shutil.copy(video, d / "VGA.mp4")
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-an",
                "-c",
                "copy",
                str(d / "VGA.mp4"),
            ],
            check=True,
            timeout=60,
        )
    probes = {name: media.probe(d / name) for name in ("VGA.mp4", "Video1.mp4")}
    (d / "probe.json").write_text(json.dumps(probes), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"NewID": new_id, "CourseName": "高数(上)"}), encoding="utf-8")
    return d


def test_find_lesson_dir(data_root):
    (data_root / "c1" / "ABCDEF12").mkdir(parents=True)
    (data_root / "c2" / "ABCDEF99").mkdir(parents=True)
    assert lesson.find_lesson_dir("ABCDEF12") == data_root / "c1" / "ABCDEF12"
    with pytest.raises(ValueError, match="2 个"):
        lesson.find_lesson_dir("ABCDEF")
    with pytest.raises(ValueError, match="0 个"):
        lesson.find_lesson_dir("ZZZ")


def test_prepare_prefers_vga_audio(data_root, synthetic_video):
    d = _make_lesson(data_root, "ID1", synthetic_video, vga_audio=True)
    assert lesson.pick_audio_source(d).name == "VGA.mp4"
    results = lesson.prepare_lesson(d)
    assert len(results) == 1
    r = results[0]
    assert r.audio == d / "audio.wav" and r.audio.exists()
    assert r.audio_source == "VGA.mp4"
    assert abs(r.audio_seconds - 9.0) < 0.3
    assert len(r.slides) == 3
    assert (d / "slides" / "slides.json").exists()
    assert lesson.prepare_lesson(d) == results  # 幂等


def test_prepare_falls_back_to_video1_when_vga_silent(data_root, synthetic_video):
    d = _make_lesson(data_root, "ID2", synthetic_video, vga_audio=False)
    assert lesson.pick_audio_source(d).name == "Video1.mp4"
    results = lesson.prepare_lesson(d)
    assert results[0].audio_source == "Video1.mp4"


def test_prepare_without_any_audio_raises(data_root, synthetic_video, tmp_path):
    d = tmp_path / "silent" / "ID3"
    d.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(synthetic_video),
            "-an",
            "-c",
            "copy",
            str(d / "VGA.mp4"),
        ],
        check=True,
        timeout=60,
    )
    with pytest.raises(RuntimeError, match="没有含音轨"):
        lesson.prepare_lesson(d)


def test_segment_dirs(data_root):
    single = data_root / "s" / "ID"
    single.mkdir(parents=True)
    assert lesson.segment_dirs(single) == [single]
    for i in (1, 2):
        (single / f"Seg{i}").mkdir()
    assert [p.name for p in lesson.segment_dirs(single)] == ["Seg1", "Seg2"]


def test_cli_prepare(data_root, synthetic_video):
    _make_lesson(data_root, "IDCLI", synthetic_video)
    result = runner.invoke(app, ["prepare", "IDCLI", "--max-slides", "10"])
    assert result.exit_code == 0, result.output
    assert "slides 3" in result.output
    assert re.search(r"audio 0\.\d min ← VGA\.mp4", result.output)  # 合成视频 9 s，不同 ffmpeg 构建四舍五入到 0.1/0.2


def test_cli_fetch_rejects_bad_types():
    result = runner.invoke(app, ["fetch", "X", "--types", "VGA,Bad"])
    assert result.exit_code != 0
    assert "非法" in result.output


def test_classify_lesson(data_root):
    d = data_root / "c" / "ID"
    d.mkdir(parents=True)
    (d / "c_result.json").write_text(json.dumps({"no_speech": True, "result": {}}), encoding="utf-8")
    from njupt_very_smart_class import pipeline

    assert pipeline.classify_lesson(d) == "empty+no_speech"
    (d / "c_result.json").write_text(json.dumps({"result": {"chapters": [{}]}}), encoding="utf-8")
    assert pipeline.classify_lesson(d) == "c_only"
    (d / "final.json").write_text(json.dumps({"result": {"chapters": [{}], "key_points": ["k"]}}), encoding="utf-8")
    assert pipeline.classify_lesson(d) == "ok"


def test_fetch_lesson_minimal_skips_video1_when_vga_has_audio(range_server, tmp_path, synthetic_video):
    from datetime import datetime

    from njupt_very_smart_class.vendor.njupt_smartclass import (
        NjuptSmartclassVideoInfo,
        NjuptSmartclassVideoSegmentInfo,
    )

    pub = tmp_path / "pub"
    pub.mkdir()
    (pub / "index.xml").write_bytes(b'<Info><VGA Src="VGA.mp4"/><Video1 Src="Video1.mp4"/></Info>')
    shutil.copy(synthetic_video, pub / "VGA.mp4")
    shutil.copy(synthetic_video, pub / "Video1.mp4")
    info = NjuptSmartclassVideoInfo(
        id="M1",
        title="t",
        start_time=datetime(2026, 1, 1),
        stop_time=datetime(2026, 1, 1),
        course_name="c",
        segments=[NjuptSmartclassVideoSegmentInfo(index_file_uri=f"{range_server}/pub/index.xml")],
    )
    sc = types.SimpleNamespace(session=requests.Session())
    lesson_dir = tmp_path / "lesson"
    import njupt_very_smart_class.lesson as L

    # fetch_lesson 内部调用 resolve_new_id / paths.lesson_dir，这里直接替换
    orig_resolve, orig_dir = L.resolve_new_id, L.paths.lesson_dir
    L.resolve_new_id = lambda sc, new_id: info
    L.paths.lesson_dir = lambda course, new_id: lesson_dir.mkdir(parents=True, exist_ok=True) or lesson_dir
    try:
        _, out_dir, segments = L.fetch_lesson(sc, "M1", types=("VGA", "Video1"), minimal=True)
    finally:
        L.resolve_new_id, L.paths.lesson_dir = orig_resolve, orig_dir
    assert [s.kind for s in segments[0].sources] == ["VGA"]
    assert (out_dir / "VGA.mp4").exists() and not (out_dir / "Video1.mp4").exists()
