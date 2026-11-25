"""视频模式：整段视频（屏幕流或机位流，均含音轨）→ qwen3.8-omni-flash → a_result_<kind>.json。

与转写模式的分工：转写模式以 ASR 转写为事实骨架、课件页为视觉上下文；视频模式把音频和画面一起交给全模态模型，
用来核对 ASR 的同音错误（echo/apple）、捕捉课件翻页之外的屏幕事件（终端演示、现场敲命令）。


SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .media import SUBPROCESS_FLAGS, MediaError, duration_seconds, probe, require_ffmpeg, same_duration
from .models import VideoModel

SOURCE_DESC = {
    "VGA": "画面是教室电脑屏幕（课件、终端、演示）",
    "Video1": "画面是教室机位摄像机（老师、黑板/板书、讲台），不是电脑屏幕；课件内容只能从投影或口述获取",
    "Video2": "画面是教室第二个机位（学生/全景）",
}

PROMPT = """这是一节 {duration} 分钟的大学课堂录播：{source}，音轨是老师讲课。
视频每 {sample} 秒采样一帧，音频完整。请整理成课堂笔记，只输出一个 JSON 对象：
{{"chapters": [{{"start": "MM:SS", "end": "MM:SS", "title": "章节标题", "points": ["要点，含具体命令/公式/结论"]}}],
 "screen_events": [{{"time": "MM:SS", "what": "屏幕上发生了什么：翻到哪一页、打开了什么、演示了什么"}}],
 "commands_or_code_shown": [{{"time": "MM:SS", "code": "屏幕上出现或被敲入的命令/代码", "purpose": "目的"}}],
 "key_points": ["老师强调的核心知识点"],
 "questions": [{{"time": "MM:SS", "q": "学生问题", "a": "老师回答"}}],
 "homework_or_notices": ["作业、通知、考试提示"],
 "terms": [{{"term": "术语或命令", "say": "老师在课上怎么读它", "explain": "一句话解释"}}],
 "uncertain": ["听不清或看不清、需要人工核对的地方，附时间"]}}
要求：时间以视频起点为零点；忠于音视频内容，不编造；命令、变量名、参数按屏幕上显示的为准（老师念的英文术语可能被听错，以画面为准）。

课程：{course}"""


def transcode_for_omni(
    src: Path,
    dst: Path,
    *,
    width: int = 640,
    fps: int = 10,
    crf: int = 28,
    timeout: float = 3600,
) -> Path:
    """缩到 width 宽、降帧率、单声道 16 kHz AAC。视频 token 与像素数成正比，1024 宽是 640 宽的 2.5 倍。"""
    if dst.exists() and same_duration(dst, src):
        return dst
    require_ffmpeg()
    if shutil.disk_usage(dst.parent).free < 1 << 30:
        raise MediaError("磁盘剩余不足 1 GB，跳过视频模式转码")
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        f"scale={width}:-2,fps={fps}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "48k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=SUBPROCESS_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        dst.unlink(missing_ok=True)
        raise MediaError(f"转码超时（{timeout:.0f}s）") from exc
    if proc.returncode or not dst.exists():
        dst.unlink(missing_ok=True)
        raise MediaError(f"转码失败：{proc.stderr.strip()[-400:]}")
    return dst


def analyze_video(
    video_model: VideoModel,
    video: Path,
    *,
    course_info: str,
    sample_seconds: float = 5.0,
    out: Path,
    force: bool = False,
    timeout: float = 1800.0,
    source: str = "VGA",
) -> dict:
    """out 已存在且参数一致时直接读回。"""
    fps = round(1.0 / sample_seconds, 4)
    model = video_model.name
    if out.exists() and not force:
        cached = json.loads(out.read_text(encoding="utf-8"))
        # 旧产物无 source 字段时视为 VGA
        if cached.get("model") == model and cached.get("fps") == fps and (cached.get("source") or "VGA") == source:
            return cached
    info = probe(video)
    duration_min = duration_seconds(info) / 60
    prompt = PROMPT.format(
        duration=round(duration_min),
        source=SOURCE_DESC.get(source, SOURCE_DESC["VGA"]),
        sample=int(sample_seconds),
        course=course_info,
    )
    result = video_model.video_json(video, prompt, fps=fps, timeout=timeout)
    streams = info.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    a_result = {
        "model": model,
        "source": source,
        "fps": fps,
        "video": {
            "file": video.name,
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "duration_s": round(duration_seconds(info), 1),
            "bytes": video.stat().st_size,
        },
        "result": result.data,
        "usage": result.usage,
    }
    out.write_text(json.dumps(a_result, ensure_ascii=False, indent=1), encoding="utf-8")
    return a_result
