"""幻灯片切分：ffmpeg scene 检测一次扫描 → 过滤过渡/重复帧 → slides/slide_NNN.jpg + slides.json。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import warnings
from pathlib import Path

from PIL import Image

from .media import SUBPROCESS_FLAGS, MediaError, grab_frame, require_ffmpeg

# [Parsed_showinfo_1 @ ...] n:   0 pts:  12345 pts_time:1.234 ...
_SHOWINFO_RE = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:([\d.]+)")


@dataclasses.dataclass(frozen=True)
class Slide:
    index: int
    time: float
    file: str


def detect_scene_frames(
    src: Path, raw_dir: Path, *, threshold: float = 0.08, timeout: float = 7200
) -> list[tuple[float, Path]]:
    """单次扫描输出所有 scene > threshold 的帧（含首帧），返回 [(秒, 文件)]。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("raw_*.jpg"):
        old.unlink()
    select = f"select=eq(n\\,0)+gt(scene\\,{threshold})"
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "info",
        "-i",
        str(src),
        "-vf",
        f"{select},showinfo",
        "-fps_mode",
        "vfr",
        "-q:v",
        "2",
        str(raw_dir / "raw_%05d.jpg"),
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
        raise MediaError(f"scene 检测超时（{timeout:.0f}s）") from exc
    if proc.returncode:
        raise MediaError(f"scene 检测失败：{proc.stderr.strip()[-400:]}")
    times = [float(m.group(3)) for m in _SHOWINFO_RE.finditer(proc.stderr)]
    files = sorted(raw_dir.glob("raw_*.jpg"))
    if len(times) != len(files):
        # showinfo 按输出顺序逐帧打印；个别视频上 ffmpeg 会多/少写一个尾帧。
        # 差在 2 以内时按顺序对齐、丢掉尾部无对应的一两个，不整节作废。
        if abs(len(times) - len(files)) > 2:
            raise MediaError(f"scene 检测结果不对齐：{len(times)} 个时间戳 vs {len(files)} 个文件")
        warnings.warn(
            f"scene 检测时间戳与文件数差 {len(times) - len(files)}，已按顺序截断对齐",
            stacklevel=2,
        )
    return list(zip(times, files, strict=False))[: min(len(times), len(files))]


def dhash(img: Image.Image) -> int:
    """64 位差分哈希：缩到 9x8 灰度，比较水平相邻像素。"""
    gray = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = gray.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def mean_rgb(img: Image.Image) -> tuple[float, float, float]:
    """16x16 缩略图的通道均值；dHash 对纯色块失明，用它区分黑屏/白板/纯色页。"""
    small = img.convert("RGB").resize((16, 16), Image.Resampling.BILINEAR)
    px = small.tobytes()
    n = len(px) // 3
    return tuple(sum(px[c::3]) / n for c in range(3))


def color_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def is_blank(img: Image.Image, *, max_mean: float = 16.0, max_std: float = 8.0) -> bool:
    """黑屏 / 加载圈 / 纯白空页：主体区域灰度均值极低或几乎无起伏。

    屏幕采集是全屏的，顶部标题栏和底部任务栏永远存在且很亮，统计时裁掉（6%–92% 高度），
    否则黑屏帧的均值会被任务栏拉到 25 以上。真实课件 std 通常 > 70。
    """
    w, h = img.size
    body = img.crop((0, int(h * 0.06), w, int(h * 0.92)))
    px = body.convert("L").resize((32, 32), Image.Resampling.BILINEAR).tobytes()
    n = len(px)
    mean = sum(px) / n
    std = (sum((v - mean) ** 2 for v in px) / n) ** 0.5
    return mean < max_mean or std < max_std


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclasses.dataclass(frozen=True)
class FrameSignature:
    dhash: int
    mean_rgb: tuple[float, float, float]

    def similar_to(self, other: FrameSignature, *, min_distance: int, max_color: float) -> bool:
        return (
            hamming(self.dhash, other.dhash) < min_distance
            and color_distance(self.mean_rgb, other.mean_rgb) < max_color
        )


def signature(img: Image.Image) -> FrameSignature:
    return FrameSignature(dhash=dhash(img), mean_rgb=mean_rgb(img))


def select_frames(
    frames: list[tuple[float, Path]],
    *,
    min_gap: float = 2.0,
    quiet: float = 30.0,
    min_distance: int = 10,
    max_color_distance: float = 24.0,
    max_slides: int | None = None,
) -> list[tuple[float, Path]]:
    """过滤过渡帧与重复帧。

    - 距上一张保留帧 < min_gap 秒：翻页过渡的中间帧，丢。
    - 距上一张保留帧 < quiet 秒且画面相似（dHash 近且颜色近）：弹窗开/关这类成对事件，丢。
    - 静默 >= quiet 秒后的首次变化：直接保留。白底文字 PPT 翻页的 dHash 距离常只有 5–13，
      不能被哈希否掉。
    """
    kept: list[tuple[float, Path]] = []
    last_sig: FrameSignature | None = None
    for t, path in frames:
        if kept and t - kept[-1][0] < min_gap:
            continue
        with Image.open(path) as img:
            if is_blank(img):
                continue
            sig = signature(img)
        if (
            last_sig is not None
            and t - kept[-1][0] < quiet
            and sig.similar_to(last_sig, min_distance=min_distance, max_color=max_color_distance)
        ):
            continue
        kept.append((t, path))
        last_sig = sig
    if max_slides is not None and len(kept) > max_slides:
        step = len(kept) / max_slides
        kept = [kept[int(i * step)] for i in range(max_slides)]
    return kept


def _fill_long_gaps(
    kept: list[tuple[float, Path]], src: Path, raw_dir: Path, duration: float, max_gap: float
) -> list[tuple[float, Path]]:
    """相邻保留帧间隔超过 max_gap 时按 max_gap 等距补帧（终端演示等缓慢变化的内容）。"""
    times = [t for t, _ in kept] + [duration]
    extra: list[tuple[float, Path]] = []
    for i, (t, _path) in enumerate(kept):
        nxt = times[i + 1]
        probe = t + max_gap
        while probe < nxt - max_gap / 2:
            dst = raw_dir / f"fill_{len(extra):05d}.jpg"
            grab_frame(src, probe, dst)
            extra.append((probe, dst))
            probe += max_gap
    return sorted(kept + extra)


def extract_slides(
    src: Path,
    out_dir: Path,
    *,
    threshold: float = 0.08,
    max_slides: int | None = 150,
    max_gap: float = 180.0,
) -> list[Slide]:
    """主入口：从屏幕流视频切出幻灯片，写 slides.json；已存在则直接读回。"""
    require_ffmpeg()
    manifest = out_dir / "slides.json"
    if manifest.exists():
        return [
            Slide(index=int(d["index"]), time=float(d["time"]), file=d["file"])
            for d in json.loads(manifest.read_text(encoding="utf-8"))
        ]
    raw_dir = out_dir / "raw"
    frames = select_frames(detect_scene_frames(src, raw_dir, threshold=threshold), max_slides=None)
    frames = _fill_long_gaps(frames, src, raw_dir, media_duration(src), max_gap)
    if max_slides is not None and len(frames) > max_slides:
        step = len(frames) / max_slides
        frames = [frames[int(i * step)] for i in range(max_slides)]
    for old in out_dir.glob("slide_*.jpg"):
        old.unlink()
    slides = []
    for i, (t, path) in enumerate(frames, start=1):
        name = f"slide_{i:03d}.jpg"
        shutil.move(str(path), out_dir / name)
        slides.append(Slide(index=i, time=round(t, 3), file=name))
    shutil.rmtree(raw_dir, ignore_errors=True)
    manifest.write_text(
        json.dumps([{**dataclasses.asdict(s), "source": "screen"} for s in slides], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return slides


def media_duration(src: Path) -> float:
    from .media import duration_seconds, probe

    return duration_seconds(probe(src))


# ---------- 机位流关键帧 ----------
def sample_frames(
    src: Path, raw_dir: Path, *, every_s: float = 30.0, timeout: float = 3600
) -> list[tuple[float, Path]]:
    """按固定间隔均匀抽帧（机位流不适合 scene 检测：老师一直在动）。时间按序号 × 间隔推算，误差 < 1 s。"""
    require_ffmpeg()
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("raw_*.jpg"):
        old.unlink()
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        f"fps=1/{every_s},showinfo",
        "-fps_mode",
        "vfr",
        "-q:v",
        "2",
        str(raw_dir / "raw_%05d.jpg"),
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
        raise MediaError(f"抽帧超时（{timeout:.0f}s）") from exc
    if proc.returncode:
        raise MediaError(f"抽帧失败：{proc.stderr.strip()[-400:]}")
    files = sorted(raw_dir.glob("raw_*.jpg"))
    if not files:  # 视频比采样间隔还短时 fps 滤镜一帧都不出，至少留首帧
        from .media import grab_frame

        first = raw_dir / "raw_00001.jpg"
        grab_frame(src, 0.0, first)
        files = [first]
    return [(i * every_s, path) for i, path in enumerate(files)]


def projection_roi(img: Image.Image) -> tuple[int, int, int, int] | None:
    """找画面里最亮的大矩形（投影幕）。

    1/8 缩略图上，亮度阈值取"中位数 + 40"（夹在 140–200）以适应课件底色和墙面反光，按列/行密度取最长的密区间。
    返回原图坐标 (left, top, right, bottom)，外扩 3%；面积占画面 8%–80% 之外、宽高比不像幕布时返回 None。
    """
    w, h = img.size
    small = img.convert("L").resize((max(w // 8, 1), max(h // 8, 1)), Image.Resampling.BILINEAR)
    sw, sh = small.size
    px = small.load()
    values = sorted(px[x, y] for y in range(sh) for x in range(sw))
    threshold = min(max(values[len(values) // 2] + 40, 140), 200)
    bright = [[px[x, y] > threshold for x in range(sw)] for y in range(sh)]

    def longest_run(density: list[float], threshold: float) -> tuple[int, int] | None:
        best, start = None, None
        for i, d in enumerate(density + [0.0]):
            if d > threshold and start is None:
                start = i
            elif d <= threshold and start is not None:
                if best is None or i - start > best[1] - best[0]:
                    best = (start, i)
                start = None
        if best is None:
            return None
        # 标题行、页脚等亮像素稀疏，按主密度找不到；两端向外延伸到几乎无亮像素为止
        lo, hi = best
        while lo > 0 and density[lo - 1] > 0.08:
            lo -= 1
        while hi < len(density) and density[hi] > 0.08:
            hi += 1
        return (lo, hi)

    col = [sum(bright[y][x] for y in range(sh)) / sh for x in range(sw)]
    row = [sum(bright[y]) / sw for y in range(sh)]
    xs = longest_run(col, 0.25)
    ys = longest_run(row, 0.25)
    if not xs or not ys:
        return None
    area = (xs[1] - xs[0]) * (ys[1] - ys[0]) / (sw * sh)
    aspect = (xs[1] - xs[0]) / max(ys[1] - ys[0], 1)
    if not (0.08 <= area <= 0.8) or not (0.9 <= aspect <= 2.4):
        return None
    sx, sy = w / sw, h / sh
    left, top, right, bottom = xs[0] * sx, ys[0] * sy, xs[1] * sx, ys[1] * sy
    mx, my = (right - left) * 0.03, (bottom - top) * 0.03
    return (int(max(left - mx, 0)), int(max(top - my, 0)), int(min(right + mx, w)), int(min(bottom + my, h)))


def write_keyframes(
    seg_dir: Path,
    frames: list[tuple[float, Path]],
    *,
    source: str,
    every_s: float | None = None,
    subdir: str = "slides",
) -> list[Slide]:
    """把机位关键帧写成 <subdir>/slide_NNN.jpg + slides.json（source 字段标明来源），覆盖该目录旧内容。"""
    out_dir = seg_dir / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("slide_*.jpg"):
        old.unlink()
    for extra in (out_dir / "frames.json", out_dir / "slides.json"):
        extra.unlink(missing_ok=True)
    slides = []
    for i, (t, path) in enumerate(frames, start=1):
        name = f"slide_{i:03d}.jpg"
        if path.parent != out_dir:
            shutil.move(str(path), out_dir / name)
        slides.append(Slide(index=i, time=round(t, 3), file=name))
    manifest = [{**dataclasses.asdict(s), "source": source, "every_s": every_s} for s in slides]
    (out_dir / "slides.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return slides


def camera_keyframes(
    video: Path, seg_dir: Path, *, every_s: float = 30.0, max_slides: int | None = 120, subdir: str = "slides"
) -> list[Slide]:
    """机位流 → 均匀采样 → 去重 → 落盘。老师一直在动，所以不用 scene 检测。
    已有同采样间隔的机位清单时直接读回，避免每次重跑都全量解码。"""
    manifest = seg_dir / subdir / "slides.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data and all(d.get("source") == "camera" and float(d.get("every_s") or 0) == every_s for d in data):
            return [Slide(index=int(d["index"]), time=float(d["time"]), file=d["file"]) for d in data]
    raw_dir = seg_dir / subdir / "raw"
    frames = select_frames(sample_frames(video, raw_dir, every_s=every_s), min_gap=20.0, quiet=30.0)
    if max_slides is not None and len(frames) > max_slides:
        step = len(frames) / max_slides
        frames = [frames[int(i * step)] for i in range(max_slides)]
    slides = write_keyframes(seg_dir, frames, source="camera", every_s=every_s, subdir=subdir)
    shutil.rmtree(raw_dir, ignore_errors=True)
    return slides
