"""模型层接口：按角色定义 Protocol，阶段模块只依赖这里；具体厂商实现在各适配器模块（如 bailian.py）。

协议现状分级：
- 文本 / 图像：OpenAI Chat Completions 是真标准，一个 OpenAICompatible 适配器 + 方言配置通吃。
- 视频帧：`video_url` 是事实约定但非 OpenAI 标准；上传方式与 fps 等参数各家不同，适配器内处理。
- 音频转写：无跨厂商协议，每家一个薄适配器，统一归一化为 AsrOutput。


SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class ModelUnavailable(RuntimeError):
    """该角色没有配置可用模型（例如 NullVideoModel）。"""


@dataclass(frozen=True)
class ModelOutput:
    """一次调用的结果：解析好的 JSON 对象 + 原样保留的厂商 usage。"""

    data: dict
    usage: dict
    model: str
    truncated: bool = False
    reasoning: str = ""


def cache_key(model: str, prompt: str) -> str:
    """LLM 结果的缓存键：模型名 + 实际送入的 prompt。

    prompt 里拼装的上游内容（转写、课件描述、处理提示、视频模式的结果）任何一处变了，键就变，下游缓存随之失效。
    """
    return hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class AsrOutput:
    """音频转写：句子级毫秒时间戳。无人声时 segments 为空且 no_speech=True。"""

    segments: list[dict]  # {"begin": ms, "end": ms, "text": str}
    text: str
    duration_ms: int
    usage: dict
    model: str
    raw: dict = field(default_factory=dict)  # 厂商原始结果，留档用
    no_speech: bool = False
    reason: str | None = None


@runtime_checkable
class AsrModel(Protocol):
    """音频 → 带时间戳的句子。"""

    name: str

    def transcribe(self, audio: Path) -> AsrOutput: ...


@runtime_checkable
class VisionModel(Protocol):
    """图像 + 文本 → JSON。"""

    name: str

    def images_json(
        self, prompt: str, images: list[bytes], *, thinking: bool = False, max_tokens: int | None = None
    ) -> ModelOutput: ...


@runtime_checkable
class TextModel(Protocol):
    """文本 → JSON（分段摘要、全课 reduce、融合）。"""

    name: str

    def complete_json(
        self,
        prompt: str,
        *,
        thinking: bool = True,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> ModelOutput: ...


@runtime_checkable
class VideoModel(Protocol):
    """视频（帧，omni 类模型同时用音轨）→ JSON。"""

    name: str

    def video_json(self, video: Path, prompt: str, *, fps: float, timeout: float | None = None) -> ModelOutput: ...


class NullVideoModel:
    """不跑视频模式时放进 ModelSet.video；pipeline 据此跳过 omni 阶段。"""

    name = "none"
    spent: dict[str, int] = {}

    def video_json(self, video: Path, prompt: str, *, fps: float, timeout: float | None = None) -> ModelOutput:
        raise ModelUnavailable("未配置视频模型（NullVideoModel）")


@dataclass
class ModelSet:
    """一组角色模型；pipeline / CLI 持有，spent() 汇总各成员上报的 token。"""

    asr: AsrModel
    vision: VisionModel
    text: TextModel
    video: VideoModel

    def _distinct(self, attr: str) -> list[dict]:
        """各成员名为 attr 的计数字典，按对象去重：多个角色共用一个客户端时同一份只算一次。"""
        seen: list[dict] = []
        for member in (self.asr, self.vision, self.text, self.video):
            stats = getattr(member, attr, None)
            if isinstance(stats, dict) and not any(stats is s for s in seen):
                seen.append(stats)
        return seen

    def spent(self) -> dict[str, int]:
        """汇总各成员上报的 token 总数。"""
        total: dict[str, int] = {}
        for stats in self._distinct("spent"):
            for model, n in stats.items():
                total[model] = total.get(model, 0) + n
        return total

    def spent_detail(self) -> dict[str, dict[str, int]]:
        """同 spent()，按输入 / 输出分项；没有分项计数的成员（NullVideoModel）不计。"""
        total: dict[str, dict[str, int]] = {}
        for stats in self._distinct("spent_detail"):
            for model, d in stats.items():
                item = total.setdefault(model, {"input": 0, "output": 0})
                item["input"] += int(d.get("input") or 0)
                item["output"] += int(d.get("output") or 0)
        return total
