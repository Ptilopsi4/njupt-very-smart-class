# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import shutil

import pytest

from njupt_very_smart_class import asr, lesson, pipeline
from njupt_very_smart_class.models import NullVideoModel
from njupt_very_smart_class.vendor.njupt_smartclass import NjuptSmartclassVideoSummary
from tests.fakes import FakeAsrModel, fake_model_set, total_spent
from tests.util import plain


@pytest.fixture
def lesson_dir(data_root, synthetic_video):
    d = data_root / "Linux编程" / "ID42"
    d.mkdir(parents=True)
    shutil.copy(synthetic_video, d / "VGA.mp4")
    (d / "meta.json").write_text(
        json.dumps(
            {
                "NewID": "ID42",
                "Title": "Linux编程 老师 2026-11-02 08:00:00-08:45:00",
                "CourseName": "Linux编程",
                "StartTime": "2026-11-02T08:00:00+08:00",
                "StopTime": "2026-11-02T08:45:00+08:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (d / "streams.json").write_text(
        json.dumps([{"kind": "VGA", "file": "VGA.mp4", "start_time": 0.0, "width": 1, "height": 1}]), encoding="utf-8"
    )
    return d


def test_parse_stages():
    assert pipeline.parse_stages("render,summarize") == ("summarize", "render")
    with pytest.raises(ValueError):
        pipeline.parse_stages("fetch,nope")


def test_run_lesson_end_to_end_without_fetch(monkeypatch, lesson_dir):
    models = fake_model_set()
    logs: list[str] = []
    report = pipeline.run_lesson(
        None,
        "ID42",
        stages=("prepare", "transcribe", "vision", "summarize", "render"),
        models=models,
        log=logs.append,
    )
    assert report["lesson_dir"] == lesson_dir
    for name in (
        "audio.wav",
        "slides/slides.json",
        "slides/frames.json",
        "transcript.json",
        "transcription_raw.json",
        "chunks.json",
        "c_result.json",
        "summary.md",
        "state.json",
    ):
        assert (lesson_dir / name).exists(), name
    state = json.loads((lesson_dir / "state.json").read_text(encoding="utf-8"))
    assert set(state["stages"]) >= {"prepare", "transcribe", "vision", "summarize", "render"}
    # VGA 判定 8 + ASR 1 + 幻灯片 3 + map 1 + reduce 1，每次 150
    assert sum(state["tokens"].values()) == 2100
    assert any("[vision]" in line for line in logs)
    assert models.asr.calls[0] == "transcribe:audio.wav"


def test_run_lesson_is_idempotent(monkeypatch, lesson_dir):
    stages = ("transcribe", "vision", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=fake_model_set(), log=lambda *_: None)
    second = fake_model_set()
    pipeline.run_lesson(None, "ID42", stages=stages, models=second, log=lambda *_: None)
    assert total_spent(second) == 0  # 全部命中缓存，不再调用模型


def test_run_lesson_requires_fetch_client():
    with pytest.raises(ValueError, match="fetch"):
        pipeline.run_lesson(None, "ID42", stages=("fetch",), models=fake_model_set(), log=lambda *_: None)


def test_lesson_helpers(lesson_dir):
    assert lesson.stream_start_times(lesson_dir) == {"VGA.mp4": 0.0}
    assert lesson.stream_start_times(lesson_dir / "nope") == {}
    assert isinstance(pipeline.RunOptions().models, dict)
    assert isinstance(fake_model_set().video, NullVideoModel)


def _video(i: int, lesson_dir) -> NjuptSmartclassVideoSummary:
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 11, 2, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    return NjuptSmartclassVideoSummary(
        id=lesson_dir.name if i == 1 else f"NOPE{i}",
        title=f"课 {i}",
        start_time=start,
        stop_time=start + timedelta(minutes=45),
        course_name="Linux编程",
        teachers="t",
        classroom_name="c",
        cover_url="",
    )


def test_run_batch_continues_on_error_and_reports(monkeypatch, lesson_dir, data_root):
    videos = [_video(1, lesson_dir), _video(2, lesson_dir)]  # 第二节没有课时目录 → find_lesson_dir 抛错
    stages = ("transcribe", "vision", "summarize", "render")
    report = pipeline.run_batch(None, videos, stages=stages, models=fake_model_set(), log=lambda *_: None)
    statuses = [r["status"] for r in report["results"]]
    assert statuses == ["ok", "error"], statuses
    assert report["results"][0]["summary"].endswith("summary.md")
    assert sum(report["results"][0]["tokens"].values()) == 2100
    assert "未下载" in report["results"][1]["error"]
    assert sum(report["tokens"].values()) == 2100
    path = pipeline.write_batch_report(report, data_root / "_reports")
    assert path.exists() and path.name.startswith("batch-")
    again = pipeline.run_batch(None, videos[:1], stages=stages, models=fake_model_set(), log=lambda *_: None)
    assert again["results"][0]["status"] == "skipped"  # 已完成 → 跳过
    assert pipeline.lesson_done(lesson_dir, stages) and not pipeline.lesson_done(lesson_dir, ("fetch",))


def test_cli_run_batch_requires_filter():
    from typer.testing import CliRunner

    from njupt_very_smart_class.cli import app

    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code != 0 and "筛选条件" in result.output
    result = CliRunner().invoke(app, ["run", "X", "--omni-input", "Bad"])
    assert result.exit_code != 0 and "omni-input" in plain(result.output)


def test_silent_audio_short_circuits_asr(monkeypatch, tmp_path):
    import subprocess

    silent = tmp_path / "silent.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "3",
            str(silent),
        ],
        check=True,
        timeout=60,
    )
    assert asr.audio_mean_volume_db(silent) < -50
    models = fake_model_set()
    t = asr.transcribe(models.asr, silent)
    assert t.no_speech and t.segments == [] and t.reason.startswith("audio_silent")
    assert models.asr.calls == []  # 静音不调用模型
    assert asr.load_transcript(silent.with_name("transcript.json")).no_speech


def test_asr_no_words_code_writes_empty(monkeypatch, tmp_path):
    loud = tmp_path / "loud.wav"
    subprocess = __import__("subprocess")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(loud),
        ],
        check=True,
        timeout=60,
    )
    assert asr.audio_mean_volume_db(loud) > -50

    t = asr.transcribe(FakeAsrModel(no_words=True), loud)
    assert t.no_speech and t.reason == "asr_no_words"


def test_no_speech_lesson_still_renders(monkeypatch, lesson_dir):
    # 把 VGA 音轨换成静音，整条流水线应继续并给出警告
    import subprocess

    silent_vga = lesson_dir / "VGA_silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(lesson_dir / "VGA.mp4"),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(silent_vga),
        ],
        check=True,
        timeout=120,
    )
    silent_vga.replace(lesson_dir / "VGA.mp4")
    logs: list[str] = []
    stages = ("prepare", "transcribe", "vision", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=fake_model_set(), log=logs.append)
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert "[!WARNING]" in text and "仅基于画面" in text
    assert any("无有效语音" in line for line in logs) and any("转写模式跳过" in line for line in logs)
    c = json.loads((lesson_dir / "c_result.json").read_text(encoding="utf-8"))
    assert c["no_speech"] and c["result"] == {}
