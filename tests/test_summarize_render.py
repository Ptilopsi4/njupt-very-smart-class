# SPDX-License-Identifier: AGPL-3.0-or-later
import json

import pytest

from njupt_very_smart_class import render, summarize
from njupt_very_smart_class.asr import chunk_segments
from njupt_very_smart_class.bailian import BailianError, normalize_filetrans
from njupt_very_smart_class.vision import courseware_frames

RAW = {
    "transcripts": [
        {
            "channel_id": 0,
            "content_duration_in_milliseconds": 31560,
            "text": "第一句。第二句。",
            "sentences": [
                {"begin_time": 1040, "end_time": 13280, "text": " 第一句。 "},
                {"begin_time": 14000, "end_time": 20000, "text": "第二句。"},
                {"begin_time": 21000, "end_time": 21500, "text": "   "},
            ],
        }
    ],
    "usage": {"input_tokens": 437, "output_tokens": 92},
}


def test_normalize_flattens_first_channel():
    segments, text, duration = normalize_filetrans(RAW)
    assert segments == [
        {"begin": 1040, "end": 13280, "text": "第一句。"},
        {"begin": 14000, "end": 20000, "text": "第二句。"},
    ]
    assert duration == 31560 and text == "第一句。第二句。"


def test_normalize_rejects_empty():
    with pytest.raises(BailianError):
        normalize_filetrans({"transcripts": [{"sentences": []}]})
    with pytest.raises(BailianError):
        normalize_filetrans({})


def _seg(begin, text):
    return {"begin": begin, "end": begin + 1000, "text": text}


def test_chunk_segments_by_chars_and_gap():
    segs = [_seg(i * 2000, "x" * 50) for i in range(10)]  # 每句 50 字，间隔 1 s
    chunks = chunk_segments(segs, max_chars=120, max_gap_ms=90_000)
    assert [len(c.text) for c in chunks] == [100, 100, 100, 100, 100]
    assert all(c.index == i + 1 for i, c in enumerate(chunks))
    # 第 7 句前有 5 分钟停顿 → 提前断开
    segs[6]["begin"] = segs[5]["end"] + 300_000
    segs[6]["end"] = segs[6]["begin"] + 1000
    for i in range(7, 10):
        segs[i]["begin"] = segs[i - 1]["end"] + 2000
        segs[i]["end"] = segs[i]["begin"] + 1000
    chunks = chunk_segments(segs, max_chars=1000, max_gap_ms=90_000)
    assert len(chunks) == 2 and chunks[0].end_ms == segs[5]["end"]


FRAMES = [
    {
        "index": 1,
        "time": 0.0,
        "audio_time": 0.0,
        "file": "a.jpg",
        "kind": "desktop",
        "is_courseware": False,
        "title": "",
    },
    {
        "index": 2,
        "time": 60.0,
        "audio_time": 60.0,
        "file": "b.jpg",
        "kind": "slide",
        "is_courseware": True,
        "title": "变量",
        "summary": "讲变量",
        "keywords": ["export", "PATH"],
        "formulas_or_code": ["export PATH=$PATH:/x"],
    },
    {
        "index": 3,
        "time": 300.0,
        "audio_time": 300.0,
        "file": "c.jpg",
        "kind": "slide",
        "is_courseware": True,
        "title": "变量",
        "summary": "再讲",
        "keywords": ["export"],
        "formulas_or_code": [],
    },
]


def test_courseware_glossary_and_block():
    cw = courseware_frames(FRAMES)
    assert [f["index"] for f in cw] == [2, 3]
    assert summarize.glossary(FRAMES) == ["变量", "export", "PATH", "export PATH=$PATH:/x"]
    block = summarize.slides_block(FRAMES, 0, 120_000)
    assert "01:00 《变量》讲变量" in block and "export PATH=$PATH:/x" in block and "05:00" not in block
    assert "没有检测到课件" in summarize.slides_block(FRAMES, 130_000, 200_000)


def test_map_prompt_mentions_glossary():
    assert "全课术语表" in summarize.MAP_PROMPT and "{glossary}" in summarize.MAP_PROMPT


C_RESULT = {
    "model": "m",
    "course": "Linux编程",
    "course_info": "Linux编程 · x",
    "segments": 3,
    "transcript_chars": 100,
    "chunks": [
        {"index": 1, "start_ms": 0, "end_ms": 60000, "uncertain": ["chunk-level item"]},
        {"index": 2, "start_ms": 60000, "end_ms": 120000, "uncertain": []},
    ],
    "result": {
        "overview": "概览",
        "chapters": [
            {"start": "00:00", "end": "01:00", "title": "A", "points": ["p1", "p2"]},
            {"start": 60000, "end": 120000, "title": "B", "points": []},
        ],
        "knowledge_points": ["k1"],
        "qa": [],
        "homework_or_notices": ["作业 1"],
        "terms": [{"term": "t", "explain": "e"}],
        "uncertain": ["reduce-level item"],
    },
}
META = {
    "Title": "课程 老师 教室 2026-11-02 08:00:00-08:45:00",
    "NewID": "ID",
    "StartTime": "2026-11-02T08:00:00+08:00",
    "StopTime": "2026-11-02T08:45:00+08:00",
}


def test_ts_and_pick():
    assert render._ts("02:45") == "02:45" and render._ts(165000) == "02:45" and render._ts(None) == "00:00"
    assert [f["index"] for f in render._pick_key_slides(FRAMES)] == [2, 3]


def test_render_summary_sections(tmp_path):
    out = render.render_summary(C_RESULT, META, FRAMES, tmp_path / "summary.md")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Linux编程 2026-11-02 08:00–08:45")
    assert "> 课程 老师 教室" in text
    for h in (
        "## 一句话概览",
        "## 章节时间轴",
        "## 核心知识点",
        "## 关键幻灯片",
        "## 问答与互动",
        "## 作业 / 通知 / 考试提示",
        "## 术语表",
        "## 待人工复核",
    ):
        assert h in text, h
    assert "### 00:00–01:00 A" in text and "### 01:00–02:00 B" in text  # 字符串与毫秒两种时间都能渲染
    assert "![变量](slides/b.jpg)" in text
    assert "reduce-level item" in text and "chunk-level item" not in text  # reduce 已合并时不重复
    c2 = json.loads(json.dumps(C_RESULT))
    c2["result"]["uncertain"] = []
    text2 = render.render_summary(c2, META, FRAMES, tmp_path / "s2.md", chunks=c2["chunks"]).read_text(encoding="utf-8")
    assert "chunk-level item" in text2  # reduce 没给 uncertain 时回退到分段条目


FINAL = {
    "model": "m",
    "course": "Linux编程",
    "segments": 3,
    "transcript_chars": 100,
    "sources": {"c_model": "c", "a_model": "a", "a_fps": 0.2},
    "result": {
        "overview": "o",
        "non_teaching": [{"start": "00:00", "end": "02:33", "what": "课前准备"}],
        "chapters": [{"start": "02:33", "end": "05:08", "title": "A", "points": ["p"]}],
        "knowledge_points": ["k"],
        "demos": [{"time": "06:47", "code": "var1=12; (echo $var1)", "purpose": "子shell", "output": "12\n34"}],
        "qa": [],
        "homework_or_notices": ["作业"],
        "terms": [],
        "asr_corrections": [{"time": "06:10", "asr": "apple", "corrected": "echo"}],
        "uncertain": ["x"],
    },
}


def test_render_fused_summary(tmp_path):
    text = render.render_summary(FINAL, META, FRAMES, tmp_path / "s.md").read_text(encoding="utf-8")
    assert "转写模式 + 视频模式融合：转写 c，视频-VGA a @ 0.2 fps" in text  # 旧 sources 结构按 VGA 兼容
    assert "非教学时段：00:00–02:33 课前准备" in text
    assert "## 课堂演示" in text and "```\nvar1=12; (echo $var1)\n```" in text  # 屏幕内容不做语言标注
    assert "## ASR 勘误" in text and "| 06:10 | apple | echo |" in text
    assert "- x" in text and "chunk-level item" not in text


def test_render_warning_follows_persisted_reason(tmp_path):
    # 之后只重渲染时本次运行没有 errors：转写失败的事实记在 c_result.reason 里
    c = {
        "model": "m",
        "course": "c",
        "segments": 0,
        "transcript_chars": 0,
        "no_speech": True,
        "reason": "transcribe_failed",
        "result": {},
    }
    text = render.render_summary(c, META, [], tmp_path / "s.md").read_text(encoding="utf-8")
    assert "转写阶段失败" in text and "老师未用麦" not in text
    silent = dict(c, reason="audio_silent(-61.0 dB)")
    text = render.render_summary(silent, META, [], tmp_path / "s2.md").read_text(encoding="utf-8")
    assert "老师未用麦" in text and "转写阶段失败" not in text
    legacy = {k: v for k, v in c.items() if k != "reason"}  # 旧产物没有 reason：才回退看本次的 errors
    text = render.render_summary(legacy, META, [], tmp_path / "s3.md", errors={"transcribe": "x"}).read_text(
        encoding="utf-8"
    )
    assert "转写阶段失败" in text
