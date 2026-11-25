"""鲁棒性：阶段隔离、处理提示、低语音比、坏文件清理、prune、时间格式。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import subprocess

import pytest
import requests

from njupt_very_smart_class import lesson, media, pipeline, render, slides
from tests.fakes import FailingVideoModel, FakeAsrModel, FakeVisionModel, fake_model_set
from tests.test_pipeline import lesson_dir  # noqa: F401  夹具复用


def test_stage_failure_is_isolated_and_reported(lesson_dir, synthetic_video):  # noqa: F811
    models = fake_model_set()
    models.video = FailingVideoModel("磁盘不够")
    logs: list[str] = []
    stages = ("prepare", "transcribe", "vision", "summarize", "omni", "fuse", "render")
    report = pipeline.run_lesson(None, "ID42", stages=stages, models=models, log=logs.append)
    state = report["state"]
    assert "omni" in state["errors"] and "磁盘不够" in state["errors"]["omni"]
    assert "omni" not in state["stages"] and "render" in state["stages"]  # 失败的阶段不标完成，其余照常
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert "## 处理提示" in text and "阶段 omni 失败：磁盘不够" in text
    assert "仅转写模式" in text  # 融合没有视频模式的结果，退回转写模式
    assert any("[omni] 失败：磁盘不够" in line for line in logs)


def test_low_speech_flag_and_note(lesson_dir, synthetic_video):  # noqa: F811
    models = fake_model_set()
    models.asr = FakeAsrModel(speech_ms=1000)  # 9 s 音频只识别出 1 s 语音
    logs: list[str] = []
    stages = ("prepare", "transcribe", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=models, log=logs.append)
    t = json.loads((lesson_dir / "transcript.json").read_text(encoding="utf-8"))
    assert t["low_speech"] is True and t["speech_ratio"] == pytest.approx(0.111, abs=0.01)
    assert any("低语音比" in line for line in logs), logs
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert "有效语音只占" in text
    c = json.loads((lesson_dir / "c_result.json").read_text(encoding="utf-8"))
    assert any("有效语音只占" in n for n in c["notes"])  # 处理提示也进了 reduce 的输入


def test_unreadable_slides_note(lesson_dir, synthetic_video):  # noqa: F811
    class Unreadable(FakeVisionModel):
        def images_json(self, prompt, images, *, thinking=False, max_tokens=None):
            out = super().images_json(prompt, images, thinking=thinking, max_tokens=max_tokens)
            if "只判断画面类型" not in prompt:
                out.data["readable"] = False
            return out

    models = fake_model_set()
    models.vision = Unreadable()
    stages = ("prepare", "vision", "transcribe", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=models, log=lambda *_: None)
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert "张课件文字不清晰" in text


def test_prune_video_removes_only_originals(lesson_dir, synthetic_video):  # noqa: F811
    (lesson_dir / "VGA_640.mp4").write_bytes(b"x")
    opts = pipeline.RunOptions(prune_video=True)
    stages = ("prepare", "transcribe", "vision", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, opts=opts, models=fake_model_set(), log=lambda *_: None)
    assert not (lesson_dir / "VGA.mp4").exists()
    assert (lesson_dir / "VGA_640.mp4").exists() and (lesson_dir / "audio.wav").exists()
    assert (lesson_dir / "slides" / "slides.json").exists()


def test_corrupt_download_is_removed(range_server, tmp_path):
    from datetime import datetime

    import njupt_very_smart_class.lesson as L
    from njupt_very_smart_class.vendor.njupt_smartclass import (
        NjuptSmartclassVideoInfo,
        NjuptSmartclassVideoSegmentInfo,
    )

    pub = tmp_path / "pub"
    pub.mkdir()
    (pub / "bad.mp4").write_bytes(b"not a video at all")
    (pub / "index.xml").write_bytes(b'<Info><VGA Src="bad.mp4"/></Info>')
    info = NjuptSmartclassVideoInfo(
        id="B1",
        title="t",
        start_time=datetime(2026, 1, 1),
        stop_time=datetime(2026, 1, 1),
        course_name="c",
        segments=[NjuptSmartclassVideoSegmentInfo(index_file_uri=f"{range_server}/pub/index.xml")],
    )
    out = tmp_path / "lesson"
    out.mkdir()
    orig_resolve, orig_dir = L.resolve_new_id, L.paths.lesson_dir
    L.resolve_new_id = lambda sc, new_id: info
    L.paths.lesson_dir = lambda course, new_id: out
    try:
        sc = type("S", (), {"session": requests.Session()})()
        with pytest.raises(RuntimeError, match="无法探测"):
            L.fetch_lesson(sc, "B1", types=("VGA",), minimal=False)
    finally:
        L.resolve_new_id, L.paths.lesson_dir = orig_resolve, orig_dir
    assert not (out / "VGA.mp4").exists()  # 坏文件不能留着


def test_slides_failure_is_only_a_note(lesson_dir, synthetic_video, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise media.MediaError("scene 检测炸了")

    monkeypatch.setattr(slides, "extract_slides", boom)
    results = lesson.prepare_lesson(lesson_dir)
    assert results[0].slides == [] and any("切幻灯片失败" in n for n in results[0].notes)


def test_stream_duration_mismatch_note(lesson_dir, synthetic_video):  # noqa: F811
    # Video1 拼成 2 倍时长，和 VGA 音频差 9 s → 提示
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-stream_loop",
            "1",  # 把 9 s 的视频循环两遍，得到 18 s 的 Video1
            "-i",
            str(synthetic_video),
            "-c",
            "copy",
            str(lesson_dir / "Video1.mp4"),
        ],
        check=True,
        timeout=120,
    )
    streams = [{"file": "VGA.mp4", "start_time": 0}, {"file": "Video1.mp4", "start_time": 0}]
    (lesson_dir / "streams.json").write_text(json.dumps(streams), encoding="utf-8")
    results = lesson.prepare_lesson(lesson_dir)
    assert any("时间轴可能有偏移" in n for n in results[0].notes)


def test_ts_normalization_and_tolerant_lists():
    assert render._ts("02:45") == "02:45"
    assert render._ts("2:5") == "02:05"
    assert render._ts("75:30") == "1:15:30"
    assert render._ts("01:15:30") == "1:15:30"
    assert render._ts(165000) == "02:45"
    assert render._ts("") == "00:00" and render._ts(None) == "00:00" and render._ts(True) == "00:00"
    assert render._as_list("oops") == [] and render._as_list({"a": 1}) == [{"a": 1}]
    assert render._as_list([{"a": 1}, "junk", 3]) == [{"a": 1}]
    assert render._bullets({"x": None, "y": None}) == ["- x", "- y"]
    assert render._bullets(None) == [] and render._bullets("单条") == ["- 单条"]


def test_render_survives_malformed_result(tmp_path):
    from tests.test_summarize_render import FRAMES, META

    broken = {
        "model": "m",
        "course": "c",
        "segments": 1,
        "transcript_chars": 1,
        "result": {
            "overview": "o",
            "chapters": "应该是列表",
            "qa": {"time": "1:00", "q": "q", "a": "a"},
            "terms": None,
            "homework_or_notices": "作业",
            "uncertain": None,
        },
    }
    out = render.render_summary(broken, META, FRAMES, tmp_path / "s.md", notes=["提示一"], errors={"omni": "挂了"})
    text = out.read_text(encoding="utf-8")
    assert "## 处理提示" in text and "- 提示一" in text and "阶段 omni 失败：挂了" in text
    assert "- 作业" in text and "**1:00** 问：q" in text


def test_same_duration_rejects_half_written_products(lesson_dir, synthetic_video):  # noqa: F811
    import shutil

    src = lesson_dir / "VGA.mp4"
    wav = lesson_dir / "audio.wav"
    media.extract_audio(src, wav)
    assert media.same_duration(wav, src)
    data = wav.read_bytes()
    wav.write_bytes(data[: len(data) // 2])  # 模拟进程被硬杀留下的半截 wav
    assert not media.same_duration(wav, src)
    media.extract_audio(src, wav)  # 存在但半截 → 重做
    assert media.same_duration(wav, src)
    shutil.rmtree(lesson_dir / "slides", ignore_errors=True)


def test_batch_tokens_are_per_lesson(lesson_dir, monkeypatch):  # noqa: F811
    """批量里 ModelSet 跨课复用，每课 state.tokens 只能记自己的用量。"""
    import shutil

    from typer.testing import CliRunner

    from njupt_very_smart_class.cli import app

    second = lesson_dir.parent / "ID43"
    shutil.copytree(lesson_dir, second)
    meta = json.loads((second / "meta.json").read_text(encoding="utf-8"))
    meta["NewID"] = "ID43"
    (second / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    result = CliRunner().invoke(
        app,
        ["--json", "run", "--local", str(lesson_dir.parent), "--stages", "prepare,transcribe,vision,summarize,render"],
    )
    assert result.exit_code == 0, result.output[-800:]
    first = pipeline.load_state(lesson_dir)["tokens"]
    second_tokens = pipeline.load_state(second)["tokens"]
    assert first == second_tokens, (first, second_tokens)
    assert json.loads(result.stdout)["tokens"] == {k: v * 2 for k, v in first.items()}


def test_old_frames_without_audio_time_are_backfilled(lesson_dir):  # noqa: F811
    from njupt_very_smart_class import vision

    frames_dir = lesson_dir / "slides"
    frames_dir.mkdir(exist_ok=True)
    (frames_dir / "frames.json").write_text(
        json.dumps([{"index": 1, "time": 12.5, "file": "slide_001.jpg", "kind": "slide"}]), encoding="utf-8"
    )
    frames = vision.load_frames(frames_dir / "frames.json")
    assert vision._backfill(frames, offset_s=3.0) is True
    assert frames[1]["audio_time"] == 15.5 and frames[1]["source"] == "screen"
    assert vision._backfill(frames, offset_s=3.0) is False
