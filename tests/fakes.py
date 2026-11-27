"""测试用假模型：实现 models.py 四个 Protocol，不碰网络。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from pathlib import Path

from njupt_very_smart_class.models import AsrOutput, ModelOutput, ModelSet, NullVideoModel

RAW = {
    "transcripts": [
        {
            "channel_id": 0,
            "content_duration_in_milliseconds": 9000,
            "text": "第一句。第二句。",
            "sentences": [
                {"begin_time": 0, "end_time": 4500, "text": "第一句。"},
                {"begin_time": 4500, "end_time": 9000, "text": "第二句。"},
            ],
        }
    ],
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

SLIDE_OBJ = {
    "kind": "slide",
    "is_courseware": True,
    "title": "T",
    "summary": "S",
    "keywords": ["k"],
    "formulas_or_code": [],
    "readable": True,
}
MAP_OBJ = {
    "topics": ["t"],
    "key_points": ["p"],
    "examples_or_demo": [],
    "questions": [],
    "homework_or_notices": [],
    "uncertain": [],
}
REDUCE_OBJ = {
    "overview": "o",
    "chapters": [{"start": "00:00", "end": "00:09", "title": "C", "points": ["p"]}],
    "knowledge_points": ["k"],
    "qa": [],
    "homework_or_notices": [],
    "terms": [],
    "uncertain": [],
    "courseware": ["T"],
}
FUSE_OBJ = {"overview": "o", "chapters": [], "demos": [], "asr_corrections": [], "uncertain": [], "non_teaching": []}


def _usage():
    return {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


class _Fake:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self.spent: dict[str, int] = {}
        self.spent_detail: dict[str, dict[str, int]] = {}

    def _tick(self, call: str) -> None:
        self.calls.append(call)
        self.spent[self.name] = self.spent.get(self.name, 0) + 150
        detail = self.spent_detail.setdefault(self.name, {"input": 0, "output": 0})
        detail["input"] += 100
        detail["output"] += 50


class FakeAsrModel(_Fake):
    def __init__(self, name: str = "fake-asr", *, no_words: bool = False, speech_ms: int | None = None) -> None:
        super().__init__(name)
        self.no_words = no_words
        self.speech_ms = speech_ms  # None = 与样本等长（9000 ms）

    def transcribe(self, audio: Path) -> AsrOutput:
        self._tick(f"transcribe:{audio.name}")
        if self.no_words:
            return AsrOutput([], "", 0, {}, self.name, no_speech=True, reason="asr_no_words")
        track = RAW["transcripts"][0]
        segments = [{"begin": s["begin_time"], "end": s["end_time"], "text": s["text"]} for s in track["sentences"]]
        duration = self.speech_ms if self.speech_ms is not None else 9000
        return AsrOutput(segments, track["text"], duration, RAW["usage"], self.name, raw=RAW)


class FakeVisionModel(_Fake):
    def __init__(self, name: str = "fake-vision", screen_kind: str = "slide") -> None:
        super().__init__(name)
        self.screen_kind = screen_kind  # VGA 判定时返回的画面类型

    def images_json(self, prompt, images, *, thinking=False, max_tokens=None) -> ModelOutput:
        if "只判断画面类型" in prompt:
            teaching = self.screen_kind in {
                "slide",
                "document",
                "diagram",
                "demo",
                "video_playback",
                "projection",
                "blackboard",
            }
            self._tick(f"judge:{self.screen_kind}")
            return ModelOutput({"kind": self.screen_kind, "is_teaching_content": teaching}, _usage(), self.name)
        camera = "机位" in prompt
        self._tick(f"{'camera' if camera else 'vision'}:{len(images)}")
        return ModelOutput(dict(SLIDE_OBJ), _usage(), self.name)


class FakeTextModel(_Fake):
    """按 prompt 内容区分 map / reduce / fuse 三种调用。"""

    def __init__(self, name: str = "fake-text") -> None:
        super().__init__(name)

    def complete_json(self, prompt, *, thinking=True, max_tokens=None, timeout=None) -> ModelOutput:
        if "两份机器笔记" in prompt:  # 融合 prompt 的开头
            self._tick(f"fuse:{thinking}")
            return ModelOutput(dict(FUSE_OBJ), _usage(), self.name)
        if "助教" in prompt:
            self._tick(f"map:{thinking}")
            return ModelOutput(dict(MAP_OBJ), _usage(), self.name)
        self._tick(f"reduce:{thinking}")
        return ModelOutput(dict(REDUCE_OBJ), _usage(), self.name)


def fake_model_set() -> ModelSet:
    return ModelSet(asr=FakeAsrModel(), vision=FakeVisionModel(), text=FakeTextModel(), video=NullVideoModel())


def total_spent(models: ModelSet) -> int:
    return sum(models.spent().values())


class FailingVideoModel:
    """模拟视频模式调用失败。"""

    name = "failing-video"
    spent: dict[str, int] = {}

    def __init__(self, message: str = "模拟的视频模式故障") -> None:
        self.message = message

    def video_json(self, video, prompt, *, fps, timeout=None):
        raise RuntimeError(self.message)


VIDEO_OBJ = {
    "overview": "a",
    "chapters": [{"start": "00:00", "end": "00:09", "title": "A 章", "points": ["p"]}],
    "screen_events": [],
    "demos": [],
    "homework_or_notices": [],
    "asr_corrections": [],
    "non_teaching": [],
    "uncertain": [],
}


class FakeVideoModel(_Fake):
    """视频模式假模型：整段视频理解。"""

    def __init__(self, name: str = "fake-video") -> None:
        super().__init__(name)

    def video_json(self, video, prompt, *, fps, timeout=None) -> ModelOutput:
        self._tick(f"video:{video.name}:{fps}")
        return ModelOutput(json.loads(json.dumps(VIDEO_OBJ)), _usage(), self.name)


class FailingAsrModel:
    """模拟 ASR 调用失败。"""

    name = "failing-asr"
    spent: dict[str, int] = {}

    def __init__(self, message: str = "模拟的 ASR 故障") -> None:
        self.message = message

    def transcribe(self, audio: Path) -> AsrOutput:
        raise RuntimeError(self.message)
