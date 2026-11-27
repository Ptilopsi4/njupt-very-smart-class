"""缓存按内容失效：换模型、上游重算后下游跟着重算；旧版产物（没有 model / cache_key）不触发整课重算。
transcribe 失败时仍渲染仅基于画面的摘要，修好后重跑补上。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
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


def _set(path, key, value) -> None:
    data = _load(path)
    for item in data if isinstance(data, list) else [data]:
        item[key] = value
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
    # 手填常量时代写下的版本号 1：与没记版本一样按当前看待
    _set(lesson_dir / "slides" / "frames.json", "prompt_version", 1)
    state = pipeline.load_state(lesson_dir)
    state["vga_judge"].pop("model")
    state["vga_judge"]["prompt_version"] = 1
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


def test_prompt_change_invalidates_frames_and_vga_judge(lesson_dir, synthetic_video, monkeypatch):  # noqa: F811
    stages = ALL[1:]
    pipeline.run_lesson(None, "ID42", stages=stages, models=_a_set(), log=lambda *_: None)
    assert isinstance(vision.PROMPT_VERSION, str) and len(vision.PROMPT_VERSION) == 12
    monkeypatch.setattr(vision, "PROMPT_VERSION", "deadbeef1234")  # 模板改了 → 哈希变
    changed = _a_set()
    pipeline.run_lesson(None, "ID42", stages=stages, models=changed, log=lambda *_: None)
    assert _calls(changed, "vision", "judge:") == 8  # VGA 判定缓存同样看 prompt 版本
    assert _calls(changed, "vision", "vision:") == 3 and changed.asr.calls == []
    assert [_calls(changed, "text", k) for k in ("map:", "reduce:", "fuse:")] == [1, 1, 1]
    assert pipeline.load_state(lesson_dir)["vga_judge"]["prompt_version"] == "deadbeef1234"


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


def test_classify_lesson_reads_asr_failure_from_artifact(lesson_dir):  # noqa: F811
    (lesson_dir / "c_result.json").write_text(
        json.dumps({"no_speech": True, "reason": "transcribe_failed", "result": {}}), encoding="utf-8"
    )
    assert pipeline.classify_lesson(lesson_dir) == "asr_failed"  # 不看 state.errors：之后的局部重跑会把它冲掉
    (lesson_dir / "state.json").write_text(json.dumps({"errors": {"transcribe": "x"}}), encoding="utf-8")
    (lesson_dir / "c_result.json").unlink()
    assert pipeline.classify_lesson(lesson_dir) == "c_only"  # 没有材料就不下"空教室"的结论


# ---------- 输出被截断 / 布尔写成字符串 / 缓存键 ----------
class _Truncating(FakeTextModel):
    def complete_json(self, prompt, **kw):
        out = super().complete_json(prompt, **kw)
        return dataclasses.replace(out, truncated=True) if self.calls[-1].split(":")[0] in ("reduce", "fuse") else out


class _TruncVideo(FakeVideoModel):
    def video_json(self, video, prompt, *, fps, timeout=None):
        return dataclasses.replace(super().video_json(video, prompt, fps=fps, timeout=timeout), truncated=True)


def test_truncated_output_is_recorded_and_reported(lesson_dir, synthetic_video):  # noqa: F811
    ms = _a_set()
    ms.text, ms.video = _Truncating("fake-text"), _TruncVideo("fake-video")  # 与 _a_set() 同名，第二次才能命中缓存
    pipeline.run_lesson(None, "ID42", stages=ALL[1:], models=ms, log=lambda *_: None)
    assert _load(lesson_dir / "a_result_VGA.json")["result"]["_truncated"] is True
    assert _load(lesson_dir / "c_result.json")["result"]["_truncated"] is True
    assert _load(lesson_dir / "final.json")["result"]["_truncated"] is True
    tips = (lesson_dir / "summary.md").read_text(encoding="utf-8").split("## 处理提示")[1]
    assert tips.count("截断") == 3  # 转写模式汇总、视频模式、融合各一条

    again = _a_set()  # 全部命中缓存再渲染一遍：提示仍然在
    pipeline.run_lesson(None, "ID42", stages=ALL[1:], models=again, log=lambda *_: None)
    assert again.text.calls == [] and again.video.calls == []
    tips = (lesson_dir / "summary.md").read_text(encoding="utf-8").split("## 处理提示")[1]
    assert tips.count("截断") == 3


def test_as_bool_tolerates_string_booleans():
    assert vision.as_bool("false") is False and vision.as_bool("True") is True and vision.as_bool("是") is True
    assert vision.as_bool(True) is True and vision.as_bool(None) is None and vision.as_bool("maybe") is None
    assert vision.courseware_frames([{"is_courseware": "false", "kind": "desktop"}]) == []
    assert vision.courseware_frames([{"is_courseware": "true"}]) and vision.courseware_frames([{"kind": "slide"}])


class _StringBool(FakeVisionModel):
    def images_json(self, prompt, images, *, thinking=False, max_tokens=None):
        out = super().images_json(prompt, images, thinking=thinking, max_tokens=max_tokens)
        out.data["is_teaching_content"] = "false"
        return out


def test_judge_screen_treats_string_false_as_false(lesson_dir):  # noqa: F811
    from njupt_very_smart_class.slides import Slide

    (lesson_dir / "slides").mkdir(exist_ok=True)
    manifest = [{"index": i + 1, "time": float(i * 10), "file": f"slide_{i + 1:03d}.jpg"} for i in range(8)]
    for i in range(8):
        Image.new("RGB", (64, 64), (i * 10, 0, 0)).save(lesson_dir / "slides" / f"slide_{i + 1:03d}.jpg")
    model = _StringBool("slide")  # kind 写的是 slide，但明确说了不是教学内容
    v = vision.judge_screen(model, lesson_dir, [Slide(**d) for d in manifest], lesson_dir / "VGA.mp4")
    assert v["usable"] is False and v["teaching"] == 0


def test_frames_record_prompt_version_and_drop_orphans(tmp_path):
    d, slide_list = _slide_dir(tmp_path)
    frames = vision.describe_slides(FakeVisionModel("v1"), d, slide_list)
    assert [f["prompt_version"] for f in frames] == [vision.PROMPT_VERSION] * 2
    kept = vision.describe_slides(FakeVisionModel("v1"), d, slide_list[:1])  # 清单里只剩第 1 张
    assert [f["index"] for f in kept] == [1]
    assert [f["index"] for f in _load(d / "frames.json")] == [1]


def test_frame_audio_time_is_derived_each_run(tmp_path):
    d, slide_list = _slide_dir(tmp_path)
    vision.describe_slides(FakeVisionModel("v1"), d, slide_list, offset_s=-21.0)
    same = FakeVisionModel("v1")
    frames = vision.describe_slides(same, d, slide_list, offset_s=0.0)
    assert same.calls == []  # 只改时间标注，不重新描述
    assert [f["audio_time"] for f in frames] == [s.time for s in slide_list]
    assert [f["audio_time"] for f in _load(d / "frames.json")] == [s.time for s in slide_list]


def test_analyze_video_cache_key_covers_input(synthetic_video, tmp_path):
    from njupt_very_smart_class import video_llm

    out = tmp_path / "a.json"
    first = FakeVideoModel("v")
    a = video_llm.analyze_video(first, synthetic_video, course_info="课 A", sample_seconds=5.0, out=out)
    assert a["cache_key"] and len(first.calls) == 1
    same = FakeVideoModel("v")
    video_llm.analyze_video(same, synthetic_video, course_info="课 A", sample_seconds=5.0, out=out)
    assert same.calls == []
    other = FakeVideoModel("v")
    video_llm.analyze_video(other, synthetic_video, course_info="课 B", sample_seconds=5.0, out=out)
    assert len(other.calls) == 1  # prompt 变了
    finer = FakeVideoModel("v")
    video_llm.analyze_video(finer, synthetic_video, course_info="课 A", sample_seconds=5.5, out=out)
    assert len(finer.calls) == 1 and _load(out)["fps"] == 0.1818  # 取帧间隔变了，prompt 里的整数没变也要重算
    _strip(out, "cache_key")
    legacy = FakeVideoModel("v")
    video_llm.analyze_video(legacy, synthetic_video, course_info="课 A", sample_seconds=5.5, out=out)
    assert legacy.calls == []  # 旧产物沿用旧规则：模型、fps、来源一致就不重算
