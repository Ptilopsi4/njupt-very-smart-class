"""课时级操作：解析 NewID、下载各段与各路流、准备音频与幻灯片。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

from . import client, download, media, paths, slides
from .vendor.njupt_smartclass import NjuptSmartclass, NjuptSmartclassVideoInfo

_STREAM_FILES = ("VGA.mp4", "Video1.mp4", "Video2.mp4", "Video3.mp4")


def resolve_new_id(sc: NjuptSmartclass, id_or_prefix: str) -> NjuptSmartclassVideoInfo:
    """完整 NewID 直接取；否则按前缀在可见列表里唯一匹配。"""
    try:
        return sc.get_video_info_by_id(id_or_prefix)
    except (ValueError, KeyError, TypeError):
        pass
    matches = [v for v in client.iter_videos(sc) if v.id.startswith(id_or_prefix)]
    if len(matches) == 1:
        return sc.get_video_info_by_id(matches[0].id)
    titles = "；".join(f"{m.id[:8]} {m.title}" for m in matches[:5])
    raise ValueError(f"NewID 前缀 {id_or_prefix!r} 匹配 {len(matches)} 条：{titles}")


def segment_dirs(lesson_dir: Path) -> list[Path]:
    segs = sorted(p for p in lesson_dir.glob("Seg[0-9]*") if p.is_dir())
    return segs or [lesson_dir]


def _download_and_probe(sc, src, dest, min_free_bytes, progress, seg_dir) -> dict:
    free = download.free_bytes(seg_dir)
    if not dest.exists() and min_free_bytes and free < min_free_bytes:
        raise RuntimeError(
            f"磁盘剩余 {download.fmt_bytes(free)} 低于下限，已停在 {dest.name}；用 --min-free 调整或清理空间"
        )
    cb = progress(f"{seg_dir.name}/{src.kind}") if progress else None
    download.download_file(sc.session, src.url, dest, progress=cb)
    try:
        info = media.probe(dest)
    except media.MediaError as exc:
        dest.unlink(missing_ok=True)  # 坏文件不能留着，否则每次 fetch 都会跳过下载然后失败
        raise RuntimeError(f"{dest.name} 下载后无法探测（{str(exc)[:120]}），已删除，请重跑") from exc
    if not info.get("streams") or media.duration_seconds(info) <= 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name} 下载后探测无效（无流或时长为 0），已删除")
    return info


def fetch_lesson(
    sc: NjuptSmartclass,
    new_id: str,
    *,
    types=download.DEFAULT_TYPES,
    min_free_bytes: int = 0,
    progress: Callable[[str], Callable[[int, int], None]] | None = None,
    minimal: bool = True,
) -> tuple[NjuptSmartclassVideoInfo, Path, list[download.Segment]]:
    """下载 meta.json、各段 index.xml 与各路 mp4，并写 probe.json。返回 (info, 课时目录, 各段)。

    minimal=True：VGA 有音轨时不再下载 Video1（VGA 与机位流是同一路混音，体积约 1/7）；
    VGA 不存在或没有音轨时才下载 Video1。minimal=False 下载 types 里全部存在的流。
    """
    info = resolve_new_id(sc, new_id)
    lesson_dir = paths.lesson_dir(info.course_name, info.id)
    download.write_meta(lesson_dir, info)
    segments = download.fetch_segments(sc.session, info, lesson_dir, types=types)
    for seg in segments:
        seg_dir = seg.index_path.parent
        probes: dict[str, dict] = {}
        deferred: list[download.StreamSource] = []
        has_vga = any(s.kind == "VGA" for s in seg.sources)
        for src in seg.sources:
            dest = seg_dir / f"{src.kind}.mp4"
            if minimal and src.kind != "VGA" and has_vga:
                deferred.append(src)  # 先下 VGA，探明有音轨再决定
                continue
            probes[dest.name] = _download_and_probe(sc, src, dest, min_free_bytes, progress, seg_dir)
        vga_has_audio = any(media.has_audio(p) for n, p in probes.items() if n.startswith("VGA"))
        for src in deferred:
            dest = seg_dir / f"{src.kind}.mp4"
            if dest.exists():
                probes[dest.name] = media.probe(dest)
            elif minimal and vga_has_audio:
                continue  # VGA 已含音轨，省下 1 GB
            else:
                probes[dest.name] = _download_and_probe(sc, src, dest, min_free_bytes, progress, seg_dir)
        kept = [s for s in seg.sources if f"{s.kind}.mp4" in probes]
        segments[segments.index(seg)] = dataclasses.replace(seg, sources=kept)
        if probes:
            (seg_dir / "probe.json").write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    return info, lesson_dir, segments


def stream_start_times(seg_dir: Path) -> dict[str, float]:
    """各路流相对录制起点的开始秒数（来自 streams.json）；缺失时视为 0。"""
    path = seg_dir / "streams.json"
    if not path.exists():
        return {}
    return {d["file"]: float(d.get("start_time") or 0.0) for d in json.loads(path.read_text(encoding="utf-8"))}


def pick_audio_source(seg_dir: Path) -> Path | None:
    """优先 VGA（体积小），其次 Video1/2/3；以 probe.json 为准，缺失时现场探测。"""
    probe_path = seg_dir / "probe.json"
    probes = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else {}
    for name in _STREAM_FILES:
        path = seg_dir / name
        if path.exists() and name in probes and media.has_audio(probes[name]):
            return path
    for name in _STREAM_FILES:
        path = seg_dir / name
        if path.exists() and media.has_audio(media.probe(path)):
            return path
    return None


def describe_streams(probe_result: dict) -> str:
    parts = []
    for s in probe_result.get("streams", []):
        if s.get("codec_type") == "video":
            parts.append(f"video {s.get('codec_name')} {s.get('width')}x{s.get('height')}")
        elif s.get("codec_type") == "audio":
            parts.append(f"audio {s.get('codec_name')} {s.get('sample_rate')}Hz")
    dur = media.duration_seconds(probe_result)
    return f"{dur / 60:.1f} min, " + ", ".join(parts)


@dataclasses.dataclass(frozen=True)
class SegmentMedia:
    dir: Path
    audio: Path
    audio_source: str
    audio_seconds: float
    slides: list[slides.Slide]
    slides_video: Path | None
    notes: list[str] = dataclasses.field(default_factory=list)  # 处理提示：降级、异常、可疑之处


def find_lesson_dir(id_or_prefix: str) -> Path:
    """在 data/<课程>/<NewID>/ 里按 NewID（可前缀）定位课时目录。"""
    candidates = [
        lesson
        for course in paths.data_dir().iterdir()
        if course.is_dir()
        for lesson in course.iterdir()
        if lesson.is_dir() and lesson.name.startswith(id_or_prefix)
    ]
    if len(candidates) == 1:
        return candidates[0]
    names = "，".join(str(c) for c in candidates[:5]) or "无"
    raise ValueError(f"课时目录匹配 {len(candidates)} 个：{names}")


def prepare_lesson(
    lesson_dir: Path,
    *,
    threshold: float = 0.08,
    max_slides: int | None = 150,
    max_gap: float = 180.0,
) -> list[SegmentMedia]:
    """每段：抽 16k 单声道 wav；若存在 VGA.mp4 则切幻灯片（失败只记提示）。全部幂等。"""
    media.require_ffmpeg()
    results = []
    for seg_dir in segment_dirs(lesson_dir):
        notes: list[str] = []
        audio_src = pick_audio_source(seg_dir)
        if audio_src is None:
            have = [n for n in _STREAM_FILES if (seg_dir / n).exists()]
            raise RuntimeError(
                f"{seg_dir} 没有含音轨的视频流（已有：{have or '无'}）；"
                "请用 --types 重新 fetch 至少一路含音轨的流（VGA 通常已含）"
            )
        wav = media.extract_audio(audio_src, seg_dir / "audio.wav")
        probe = media.probe(wav)
        audio_seconds = media.duration_seconds(probe)
        if audio_seconds < 1.0:
            raise RuntimeError(
                f"{audio_src.name} 抽出的音频只有 {audio_seconds:.1f} s，文件可能损坏，请删除 {wav.name} 后重跑"
            )
        notes.extend(_stream_duration_notes(seg_dir, audio_src.name, audio_seconds))
        vga = seg_dir / "VGA.mp4"
        slide_list: list[slides.Slide] = []
        if vga.exists():
            try:
                slide_list = slides.extract_slides(
                    vga, seg_dir / "slides", threshold=threshold, max_slides=max_slides, max_gap=max_gap
                )
            except media.MediaError as exc:  # 幻灯片是可选的，失败不该让整节课停摆
                notes.append(f"屏幕流切幻灯片失败：{str(exc)[:120]}")
        else:
            notes.append("该录像没有屏幕流（VGA）")
        results.append(
            SegmentMedia(
                dir=seg_dir,
                audio=wav,
                audio_source=audio_src.name,
                audio_seconds=audio_seconds,
                slides=slide_list,
                slides_video=vga if vga.exists() else None,
                notes=notes,
            )
        )
    return results


def _stream_duration_notes(seg_dir: Path, audio_name: str, audio_seconds: float) -> list[str]:
    """两条流都在时比较时长；差异大说明其中一条中途才开播或提前结束。StartTime 语义未确认，因此只提示不改音源。"""
    starts = stream_start_times(seg_dir)
    notes = []
    for name in _STREAM_FILES:
        if name == audio_name or not (seg_dir / name).exists():
            continue
        try:
            other = media.duration_seconds(media.probe(seg_dir / name))
        except media.MediaError:
            continue
        if abs(other - audio_seconds) > 5:
            notes.append(
                f"{audio_name} 音频 {audio_seconds / 60:.1f} min，{name} 时长 {other / 60:.1f} min，"
                f"相差 {other - audio_seconds:+.0f} s（{name} StartTime {starts.get(name, 0):.0f} s），时间轴可能有偏移"
            )
    return notes


def prune_videos(seg_dir: Path) -> list[str]:
    """处理完后删除视频原件（VGA/Video1/…mp4），保留 wav、关键帧、*_640.mp4（视频模式重跑够用）。"""
    removed = []
    for name in _STREAM_FILES:
        path = seg_dir / name
        if path.exists():
            path.unlink()
            removed.append(name)
    return removed
