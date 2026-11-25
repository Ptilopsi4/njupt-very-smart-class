"""下载 index.xml 与各路 mp4：Range 续传、指数退避、幂等跳过。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
import json
import re
import shutil
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin

import requests

from .vendor.njupt_smartclass import NjuptSmartclassVideoInfo

STREAM_TYPES = ("VGA", "Video1", "Video2", "Video3")
# VGA 自带与机位流相同的麦克风混音（体积约 1/7），屏幕流同时是幻灯片来源；
# 默认列表是 VGA,Video1，但 fetch 默认 minimal：VGA 有音轨就只留 VGA，没有 VGA（或 VGA 无音轨）才下 Video1。
DEFAULT_TYPES = ("VGA", "Video1")


@dataclasses.dataclass(frozen=True)
class StreamSource:
    kind: str
    url: str
    start_time: float = 0.0  # 相对录制起点的秒数；VGA 常比机位晚几十秒开始，对齐音频时要用
    width: int = 0
    height: int = 0


def _float(value: str | None) -> float:
    try:
        return float(value) if value else 0.0
    except ValueError:
        return 0.0


@dataclasses.dataclass(frozen=True)
class Segment:
    index_path: Path
    sources: list[StreamSource]


@dataclasses.dataclass
class DownloadResult:
    path: Path
    bytes: int
    resumed: bool
    skipped: bool


def parse_index_xml(xml_bytes: bytes, index_url: str, *, types=STREAM_TYPES) -> list[StreamSource]:
    root = ET.fromstring(xml_bytes)
    info = root if root.tag == "Info" else root.find("Info")
    if info is None:
        raise ValueError("index.xml 缺少 <Info> 节点")
    sources = []
    for kind in types:
        for node in info.findall(kind):
            src = node.get("Src")
            if src:
                sources.append(
                    StreamSource(
                        kind=kind,
                        url=urljoin(index_url, src),
                        start_time=_float(node.get("StartTime")),
                        width=int(_float(node.get("Width"))),
                        height=int(_float(node.get("Height"))),
                    )
                )
    return sources


def fetch_segments(
    session: requests.Session,
    info: NjuptSmartclassVideoInfo,
    lesson_dir: Path,
    *,
    types=DEFAULT_TYPES,
) -> list[Segment]:
    """下载各段 index.xml 到课时目录，解析出各路流的绝对 URL。"""
    multi = len(info.segments) > 1
    segments = []
    for i, seg in enumerate(info.segments, start=1):
        seg_dir = lesson_dir / f"Seg{i}" if multi else lesson_dir
        seg_dir.mkdir(parents=True, exist_ok=True)
        index_path = seg_dir / "index.xml"
        if not index_path.exists():
            resp = session.get(seg.index_file_uri, timeout=30)
            if resp.status_code != 200:
                # 报错里不带签名参数
                signed_path = seg.index_file_uri.split("?", 1)[0]
                raise RuntimeError(
                    f"Index.xml 返回 HTTP {resp.status_code}：{signed_path}\n"
                    "该录像的媒体文件可能已被平台清理（常见于老录像），请换近期的录播；"
                    "可用 vsc list --start/--title 筛选"
                )
            index_path.write_bytes(resp.content)
        sources = parse_index_xml(index_path.read_bytes(), seg.index_file_uri, types=types)
        streams_path = seg_dir / "streams.json"
        known = json.loads(streams_path.read_text(encoding="utf-8")) if streams_path.exists() else []
        entries = {d["file"]: d for d in known}
        for s in sources:  # 补下新流时合并进来，缺了 StartTime 时间轴偏移就会按 0 算
            entries[f"{s.kind}.mp4"] = {
                "kind": s.kind,
                "file": f"{s.kind}.mp4",
                "start_time": s.start_time,
                "width": s.width,
                "height": s.height,
                "url": s.url,
            }
        streams_path.write_text(json.dumps(list(entries.values()), ensure_ascii=False, indent=2), encoding="utf-8")
        segments.append(Segment(index_path=index_path, sources=sources))
    return segments


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    max_retries: int = 10,
    initial_timeout: float = 1.0,
    max_timeout: float = 120.0,
    chunk_size: int = 1 << 16,
    progress: Callable[[int, int], None] | None = None,
) -> DownloadResult:
    """带 Range 续传的下载；dest 已存在则直接跳过。"""
    if dest.exists():
        return DownloadResult(dest, dest.stat().st_size, resumed=False, skipped=True)
    part = dest.with_name(dest.name + ".part")
    retries, timeout, resumed = 0, initial_timeout, part.exists()
    while True:
        try:
            start = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={start}-"} if start else {}
            with session.get(url, headers=headers, stream=True, timeout=30) as resp:
                if resp.status_code == 416 and start:
                    part.rename(dest)
                    return DownloadResult(dest, start, resumed=True, skipped=False)
                if resp.status_code not in (200, 206):
                    resp.raise_for_status()
                if resp.status_code == 200 and start:
                    # 服务端不支持 Range，从头来
                    part.unlink()
                    start = 0
                    resumed = False
                cr = resp.headers.get("Content-Range", "")
                total = int(cr.rsplit("/", 1)[-1]) if cr else int(resp.headers.get("Content-Length", 0)) + start
                done = start
                with part.open("ab" if start else "wb") as fh:
                    for chunk in resp.iter_content(chunk_size):
                        if chunk:
                            fh.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
            part.rename(dest)
            return DownloadResult(dest, done, resumed=resumed, skipped=False)
        except (requests.RequestException, OSError) as exc:
            retries += 1
            if retries > max_retries:
                raise RuntimeError(f"下载失败（{retries} 次重试）：{url}") from exc
            time.sleep(timeout)
            timeout = min(timeout * 2, max_timeout)


def write_meta(lesson_dir: Path, info: NjuptSmartclassVideoInfo, **extra) -> Path:
    meta = {
        "NewID": info.id,
        "Title": info.title,
        "StartTime": info.start_time.isoformat(),
        "StopTime": info.stop_time.isoformat(),
        "CourseName": info.course_name,
        "Segments": len(info.segments),
        **extra,
    }
    path = lesson_dir / "meta.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fmt_bytes(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMG]?B?)$", re.I)


def parse_size(s: str) -> int:
    m = _SIZE_RE.match(s.strip())
    if not m:
        raise ValueError(f"无法解析大小：{s!r}")
    num, unit = float(m.group(1)), m.group(2).upper()
    factor = {
        "": 1,
        "B": 1,
        "K": 1 << 10,
        "KB": 1 << 10,
        "M": 1 << 20,
        "MB": 1 << 20,
        "G": 1 << 30,
        "GB": 1 << 30,
    }[unit]
    return int(num * factor)


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free
