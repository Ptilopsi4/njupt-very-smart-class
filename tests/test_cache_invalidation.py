"""缓存按内容失效：换模型、上游重算后下游跟着重算；旧版产物（没有 model / cache_key）不触发整课重算。
transcribe 失败时仍渲染仅基于画面的摘要，修好后重跑补上。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json

from PIL import Image

from njupt_very_smart_class import asr, fuse, pipeline, summarize, vision
from njupt_very_smart_class.models import ModelSet
from njupt_very_smart_class.slides import Slide
from tests.fakes import FailingAsrModel, FakeTextModel, FakeVideoModel, FakeVisionModel, fake_model_set
from tests.test_pipeline import lesson_dir  # noqa: F401  夹具复用
from tests.test_summarize_render import FRAMES

ALL = ("prepare", "transcribe", "vision", "summarize", "omni", "fuse", "render")


def _a_set() -> ModelSet:
    ms = fake_model_set()
    ms.video = FakeVideoModel()
    return ms


def _calls(models: ModelSet, member: str, prefix: str) -> int:
    return sum(c.startswith(prefix) for c in getattr(models, member).calls)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _strip(path, *keys) -> None:
    """把新版的记账字段去掉，模拟旧版产物。"""
    data = _load(path)
    for item in data if isinstance(data, list) else [data]:
        for k in keys:
            item.pop(k, None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------- 课件描述 ----------


def _slide_dir(tmp_path) -> tuple:
    d = tmp_path / "slides"
    d.mkdir()
    slides = []
    for i in (1, 2):
        Image.new("RGB", (64, 64), (i * 40, 0, 0)).save(d / f"slide_{i:03d}.jpg")
        slides.append(Slide(index=i, time=float(i * 30), file=f"slide_{i:03d}.jpg"))
    return d, slides


def test_frames_follow_vision_model(tmp_path):
    d, slides = _slide_dir(tmp_path)
    first = vision.describe_slides(FakeVisionModel("v1"), d, slides)
    assert [f["model"] for f in first] == ["v1", "v1"]
    same = FakeVisionModel("v1")
    vision.describe_slides(same, d, slides)
    assert same.calls == []  # 同一模型命中缓存
    other = FakeVisionModel("v2")
    again = vision.describe_slides(other, d, slides)
    assert [f["model"] for f in again] == ["v2", "v2"] and len(other.calls) == 2


def test_legacy_frames_are_kept_and_stamped(tmp_path):
    d, slides = _slide_dir(tmp_path)
    (d / "frames.json").write_text(
        json.dumps([{"index": s.index, "time": s.time, "file": s.file, "kind": "slide"} for s in slides]),
        encoding="utf-8",
    )
    v = FakeVisionModel("v1")
    frames = vision.describe_slides(v, d, slides)
    assert v.calls == []  # 旧版 frames.json 没记模型：按当前模型补记，不重算
    assert [f["model"] for f in frames] == ["v1", "v1"] and frames[0]["source"] == "screen"
    assert all("model" in f for f in _load(d / "frames.json"))


# ---------- 摘要与融合 ----------


def _write_transcript(tmp_path) -> None:
    asr.write_transcript(
        tmp_path / "transcript.json",
        {
            "model": "asr",
            "duration_ms": 9000,
            "segments": [{"begin": 0, "end": 9000, "text": "讲变量。"}],
            "text": "讲变量。",
        },
    )


def _write_frames(tmp_path, title: str = "变量") -> None:
    (tmp_path / "slides").mkdir(exist_ok=True)
    frames = json.loads(json.dumps(FRAMES))
    frames[1]["title"] = title
    (tmp_path / "slides" / "frames.json").write_text(json.dumps(frames, ensure_ascii=False), encoding="utf-8")


class _Recording(FakeTextModel):
    def __init__(self) -> None:
        super().__init__("t")
        self.prompts: list[str] = []

    def complete_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return super().complete_json(prompt, **kw)


def test_summarize_caches_follow_inputs(tmp_path):
    _write_transcript(tmp_path)
    _write_frames(tmp_path)
    first = _Recording()
    summarize.summarize_segment(first, tmp_path, course_info="c", course_name="n")
    assert [c.split(":")[0] for c in first.calls] == ["map", "reduce"]
    assert all("cache_key" not in p and '"usage"' not in p for p in first.prompts)  # 记账字段不进 prompt

    same = FakeTextModel("t")
    summarize.summarize_segment(same, tmp_path, course_info="c", course_name="n")
    assert same.calls == []  # 输入没变：map、reduce 都不重跑

    _write_frames(tmp_path, title="变量与作用域")  # 课件描述变了，分段序号没变
    changed = FakeTextModel("t")
    summarize.summarize_segment(changed, tmp_path, course_info="c", course_name="n")
    assert [c.split(":")[0] for c in changed.calls] == ["map", "reduce"]

    noted = FakeTextModel("t")  # 处理提示是 reduce 的输入之一
    summarize.summarize_segment(
        noted, tmp_path, course_info="c", course_name="n", processing_notes=["只有 3/8 张是教学内容"]
    )
    assert [c.split(":")[0] for c in noted.calls] == ["reduce"]


def test_fuse_cache_follows_inputs(tmp_path):
    _write_transcript(tmp_path)
    _write_frames(tmp_path)
    summarize.summarize_segment(FakeTextModel("t"), tmp_path, course_info="c", course_name="n")
    (tmp_path / "a_result_VGA.json").write_text(
        json.dumps({"model": "am", "fps": 0.2, "source": "VGA", "result": {"chapters": []}}), encoding="utf-8"
    )
    text = FakeTextModel("t")
    fuse.fuse(text, tmp_path, course_info="c")
    cached = FakeTextModel("t")
    fuse.fuse(cached, tmp_path, course_info="c")
    assert cached.calls == []  # 输入没变：命中缓存
    assert _load(tmp_path / "final.json")["cache_key"]
    c = _load(tmp_path / "c_result.json")
    c["result"]["overview"] = "改过的概览"
    (tmp_path / "c_result.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    again = FakeTextModel("t")
    fuse.fuse(again, tmp_path, course_info="c")
    assert [k.split(":")[0] for k in again.calls] == ["fuse"]  # C 变了，融合重算


def test_legacy_summarize_and_fuse_keep_old_rule(tmp_path):
    _write_transcript(tmp_path)
    _write_frames(tmp_path)
    summarize.summarize_segment(FakeTextModel("t"), tmp_path, course_info="c", course_name="n")
    (tmp_path / "a_result_VGA.json").write_text(
        json.dumps({"model": "am", "fps": 0.2, "source": "VGA", "result": {"chapters": []}}), encoding="utf-8"
    )
    fuse.fuse(FakeTextModel("t"), tmp_path, course_info="c")
    for name in ("chunks.json", "c_result.json", "final.json"):
        _strip(tmp_path / name, "cache_key")
    text = FakeTextModel("t")
    summarize.summarize_segment(text, tmp_path, course_info="c", course_name="n")
    fuse.fuse(text, tmp_path, course_info="c")
    assert text.calls == []  # 旧版产物没有 cache_key：沿用旧规则，不为升级重算


# ---------- 整条流水线 ----------


def test_vision_model_change_propagates_to_summary(lesson_dir, synthetic_video):  # noqa: F811
    stages = ALL[1:]
    pipeline.run_lesson(None, "ID42", stages=stages, models=_a_set(), log=lambda *_: None)
    second = _a_set()
    second.vision = FakeVisionModel("v2")  # 只换视觉模型
    pipeline.run_lesson(None, "ID42", stages=stages, models=second, log=lambda *_: None)
    assert _calls(second, "vision", "judge:") == 8 and _calls(second, "vision", "vision:") == 3
    assert second.asr.calls == [] and second.video.calls == []  # 转写与视频模式命中缓存
    assert [_calls(second, "text", k) for k in ("map:", "reduce:", "fuse:")] == [1, 1, 1]  # 下游跟着重算
    third = _a_set()
    third.vision = FakeVisionModel("v2")
    pipeline.run_lesson(None, "ID42", stages=stages, models=third, log=lambda *_: None)
    assert third.vision.calls == [] and third.text.calls == [] and third.asr.calls == [] and third.video.calls == []


def test_force_vision_refreshes_downstream(lesson_dir, synthetic_video):  # noqa: F811
    stages = ALL[1:]
    pipeline.run_lesson(None, "ID42", stages=stages, models=_a_set(), log=lambda *_: None)
    again = _a_set()
    opts = pipeline.RunOptions(force_vision=True)
    pipeline.run_lesson(None, "ID42", stages=stages, models=again, opts=opts, log=lambda *_: None)
    assert _calls(again, "vision", "judge:") == 8  # 判定也重做
    assert _calls(again, "vision", "vision:") == 3 and again.asr.calls == [] and again.video.calls == []
    assert [_calls(again, "text", k) for k in ("map:", "reduce:", "fuse:")] == [1, 1, 1]


def test_legacy_lesson_is_not_recomputed_on_upgrade(lesson_dir, synthetic_video):  # noqa: F811
    stages = ALL[1:]
    pipeline.run_lesson(None, "ID42", stages=stages, models=_a_set(), log=lambda *_: None)
    for name in ("chunks.json", "c_result.json", "final.json"):
        _strip(lesson_dir / name, "cache_key")
    _strip(lesson_dir / "slides" / "frames.json", "model")
    state = pipeline.load_state(lesson_dir)
    state["vga_judge"].pop("model")
    (lesson_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    upgraded = _a_set()
    pipeline.run_lesson(None, "ID42", stages=stages, models=upgraded, log=lambda *_: None)
    assert upgraded.vision.calls == [] and upgraded.text.calls == [] and upgraded.asr.calls == []
    assert upgraded.video.calls == []

    forced = _a_set()  # 旧版下游产物：上游重算时照样跟着重算
    opts = pipeline.RunOptions(force_vision=True)
    pipeline.run_lesson(None, "ID42", stages=stages, models=forced, opts=opts, log=lambda *_: None)
    assert _calls(forced, "vision", "vision:") == 3
    assert [_calls(forced, "text", k) for k in ("map:", "reduce:", "fuse:")] == [1, 1, 1]


# ---------- transcribe 失败 ----------


def test_transcribe_failure_still_renders_and_recovers(lesson_dir, synthetic_video):  # noqa: F811
    stages = ALL[1:]
    models = _a_set()
    models.asr = FailingAsrModel("模拟的 ASR 故障")
    logs: list[str] = []
    report = pipeline.run_lesson(None, "ID42", stages=stages, models=models, log=logs.append)
    state = report["state"]
    assert state["errors"] == {"transcribe": "模拟的 ASR 故障"}
    assert "transcribe" not in state["stages"] and {"summarize", "omni", "fuse", "render"} <= set(state["stages"])
    assert not (lesson_dir / "transcript.json").exists()  # 失败不留半成品，下次重试
    c = _load(lesson_dir / "c_result.json")
    assert c["no_speech"] and c["reason"] == "transcribe_failed" and c["result"] == {}
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert "[!WARNING]" in text and "转写阶段失败" in text and "老师未用麦" not in text
    assert "## 处理提示" in text and "阶段 transcribe 失败：模拟的 ASR 故障" in text
    assert text.count("阶段 transcribe 失败") == 1  # notes 与 errors 里各有一条，渲染只留全文
    assert "没有转写结果，转写模式不写笔记" in text
    assert (lesson_dir / "final.json").exists()  # 有视频模式的结果时按它整理
    assert "asr_failed" in pipeline.classify_lesson(lesson_dir)
    assert not pipeline.lesson_done(lesson_dir, stages)  # 批量下次会重试
    assert any("[transcribe] 失败" in line for line in logs)

    fixed = _a_set()  # 修好转写后重跑：转写 → 摘要 → 融合一路补上，错误清空
    report = pipeline.run_lesson(None, "ID42", stages=stages, models=fixed, log=lambda *_: None)
    assert report["state"]["errors"] == {} and "transcribe" in report["state"]["stages"]
    assert len(fixed.asr.calls) == 1 and fixed.video.calls == []
    assert [_calls(fixed, "text", k) for k in ("map:", "reduce:", "fuse:")] == [1, 1, 1]
    c = _load(lesson_dir / "c_result.json")
    assert not c.get("no_speech") and c["segments"] == 2
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert "[!WARNING]" not in text and "转写阶段失败" not in text
    assert pipeline.lesson_done(lesson_dir, stages)


def test_classify_lesson_distinguishes_asr_failure(lesson_dir):  # noqa: F811
    (lesson_dir / "c_result.json").write_text(
        json.dumps({"no_speech": True, "reason": "transcribe_failed", "result": {}}), encoding="utf-8"
    )
    assert pipeline.classify_lesson(lesson_dir) == "empty+no_speech"
    (lesson_dir / "state.json").write_text(json.dumps({"errors": {"transcribe": "x"}}), encoding="utf-8")
    assert pipeline.classify_lesson(lesson_dir) == "asr_failed"
