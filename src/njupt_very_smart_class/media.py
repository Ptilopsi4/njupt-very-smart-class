"""ffmpeg / ffprobe 封装：探测流、抽音频、按时间截帧。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

# ffmpeg 的解码/编码会短时占满所有核；Windows 下以低优先级运行，避免抢前台工作的算力
SUBPROCESS_FLAGS = subprocess.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0


class MediaError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise MediaError("需要 ffmpeg 与 ffprobe 在 PATH 中")


def _run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=SUBPROCESS_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"命令超时（{timeout:.0f}s）：{' '.join(cmd[:3])} …") from exc


def probe(path: Path) -> dict:
    """返回精简的 ffprobe 结果：{"format": {...}, "streams": [{index, codec_type, ...}]}。"""
    require_ffmpeg()
    proc = _run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        timeout=300,
    )
    if proc.returncode:
        raise MediaError(f"ffprobe 失败：{proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
    stream_keys = (
        "index",
        "codec_type",
        "codec_name",
        "width",
        "height",
        "sample_rate",
        "channels",
        "duration",
        "bit_rate",
    )
    fmt = data.get("format", {})
    return {
        "format": {k: fmt[k] for k in ("duration", "size", "bit_rate", "format_name") if k in fmt},
        "streams": [{k: s[k] for k in stream_keys if k in s} for s in data.get("streams", [])],
    }


def has_audio(probe_result: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe_result.get("streams", []))


def duration_seconds(probe_result: dict) -> float:
    try:
        return float(probe_result.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        return 0.0


def same_duration(a: Path, b: Path, tolerance: float = 2.0) -> bool:
    """两个媒体文件时长是否一致（±tolerance 秒；容器间差异远小于此）；探测失败视为不一致。
    进程被硬杀留下的半成品音视频靠这个挡住，否则会被当成已完成直接送给模型。"""
    try:
        da, db = duration_seconds(probe(a)), duration_seconds(probe(b))
    except MediaError:
        return False
    return da > 0 and db > 0 and abs(da - db) <= tolerance


def extract_audio(src: Path, dst: Path, *, sample_rate: int = 16000) -> Path:
    """抽出 16 kHz 单声道 16-bit PCM wav；dst 已存在且与源等长则跳过。"""
    if dst.exists() and same_duration(dst, src):
        return dst
    require_ffmpeg()
    proc = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        timeout=3600,
    )
    if proc.returncode or not dst.exists():
        dst.unlink(missing_ok=True)
        raise MediaError(f"抽音频失败：{proc.stderr.strip()}")
    return dst


def grab_frame(src: Path, seconds: float, dst: Path, *, timeout: float = 120) -> Path:
    """按时间点截一帧（缩略图用）。"""
    require_ffmpeg()
    proc = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(dst),
        ],
        timeout=timeout,
    )
    if proc.returncode or not dst.exists():
        dst.unlink(missing_ok=True)
        raise MediaError(f"截帧失败：{proc.stderr.strip()}")
    return dst
