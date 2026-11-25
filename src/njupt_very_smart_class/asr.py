"""转写模式第一步：audio.wav → 带毫秒时间戳的 transcript.json（幂等）。

本模块只负责本地的事：静音检测、缓存、分块。网络与厂商协议在 AsrModel 的实现里（见 bailian.DashScopeAsrModel）。


SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .media import SUBPROCESS_FLAGS, duration_seconds, probe
from .models import AsrModel


class AsrError(RuntimeError):
    pass


@dataclass
class Transcript:
    path: Path
    model: str
    duration_ms: int
    segments: list[dict]  # {"begin": ms, "end": ms, "text": str}
    text: str
    usage: dict = field(default_factory=dict)
    no_speech: bool = False
    reason: str | None = None
    low_speech: bool = False  # 有效语音占比过低（噪音/音乐被当成话、或大部分时间没人说话）
    speech_ratio: float | None = None

    @property
    def chars(self) -> int:
        return sum(len(s["text"]) for s in self.segments)


def load_transcript(path: Path) -> Transcript | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Transcript(
        path=path,
        model=data.get("model", ""),
        duration_ms=int(data.get("duration_ms") or 0),
        segments=data.get("segments", []),
        text=data.get("text", ""),
        usage=data.get("usage") or {},
        no_speech=bool(data.get("no_speech")),
        reason=data.get("reason"),
        low_speech=bool(data.get("low_speech")),
        speech_ratio=data.get("speech_ratio"),
    )


def audio_mean_volume_db(wav: Path) -> float:
    """ffmpeg volumedetect 的 mean_volume；整段解码，47 min 约 10 s。"""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(wav), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        creationflags=SUBPROCESS_FLAGS,
    )
    match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    if not match:
        raise AsrError(f"volumedetect 没有输出 mean_volume：{proc.stderr[-300:]}")
    return float(match.group(1))


def write_transcript(out: Path, data: dict) -> Transcript:
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return Transcript(
        path=out,
        model=data.get("model", ""),
        duration_ms=int(data.get("duration_ms") or 0),
        segments=data.get("segments", []),
        text=data.get("text", ""),
        usage=data.get("usage") or {},
        no_speech=bool(data.get("no_speech")),
        reason=data.get("reason"),
        low_speech=bool(data.get("low_speech")),
        speech_ratio=data.get("speech_ratio"),
    )


def write_empty_transcript(out: Path, *, model: str, reason: str, duration_ms: int = 0) -> Transcript:
    return write_transcript(
        out,
        {
            "model": model,
            "duration_ms": duration_ms,
            "segments": [],
            "text": "",
            "usage": {},
            "no_speech": True,
            "reason": reason,
        },
    )


def transcribe(asr_model: AsrModel, wav: Path, *, silence_db: float = -50.0) -> Transcript:
    """无人声（本地测静音，或服务端判定无词）时写空转写并标记 no_speech，而不是让流水线中断。"""
    out = wav.with_name("transcript.json")
    cached = load_transcript(out)
    if cached is not None and cached.model == asr_model.name:
        return cached
    mean_db = audio_mean_volume_db(wav)
    if mean_db < silence_db:
        return write_empty_transcript(out, model=asr_model.name, reason=f"audio_silent({mean_db:.1f} dB)")
    result = asr_model.transcribe(wav)
    if result.raw:
        wav.with_name("transcription_raw.json").write_text(
            json.dumps(result.raw, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    if result.no_speech:
        return write_empty_transcript(out, model=result.model, reason=result.reason or "asr_no_words")
    data = {
        "model": result.model,
        "duration_ms": result.duration_ms,
        "segments": result.segments,
        "text": result.text,
        "usage": result.usage,
    }
    wav_ms = duration_seconds(probe(wav)) * 1000
    if wav_ms > 0:
        ratio = min(result.duration_ms / wav_ms, 1.0)
        data["speech_ratio"] = round(ratio, 3)
        if ratio < 0.25:
            data["low_speech"] = True  # 45 分钟只有几分钟被识别为语音：要么噪音幻听，要么大半时间没人说话
    return write_transcript(out, data)


@dataclass(frozen=True)
class Chunk:
    index: int
    start_ms: int
    end_ms: int
    text: str


def chunk_segments(segments: list[dict], *, max_chars: int = 3500, max_gap_ms: int = 90_000) -> list[Chunk]:
    """按句子聚合到约 max_chars；句间停顿超过 max_gap_ms 时提前断开，让分块贴着话题边界。"""
    chunks: list[Chunk] = []
    current: list[dict] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            chunks.append(
                Chunk(
                    index=len(chunks) + 1,
                    start_ms=current[0]["begin"],
                    end_ms=current[-1]["end"],
                    text="".join(s["text"] for s in current),
                )
            )
        current, size = [], 0

    for seg in segments:
        if current and (size + len(seg["text"]) > max_chars or seg["begin"] - current[-1]["end"] > max_gap_ms):
            flush()
        current.append(seg)
        size += len(seg["text"])
    flush()
    return chunks


def segments_between(segments: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    return [s for s in segments if s["end"] >= start_ms and s["begin"] <= end_ms]
