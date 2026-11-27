"""视觉模型调用：VGA 可用性判定、幻灯片/机位关键帧描述。阶段逻辑只依赖 VisionModel 接口。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from .media import duration_seconds, probe
from .models import VisionModel
from .slides import Slide, projection_roi, sample_frames

# 学科无关的画面分类：教学内容 = 任何向课堂展示的讲授材料
KINDS = (
    "slide（任何学科的课件页）",
    "document（讲义、教材、试卷、PDF、文档）",
    "diagram（图表、公式推导、地图、图片、乐谱等视觉材料）",
    "demo（软件操作、编程、实验演示、网页浏览等现场演示）",
    "video_playback（为教学播放的音视频）",
    "projection（机位拍到的投影幕/白幕上的内容）",
    "blackboard（板书）",
    "desktop（空桌面、文件管理器、与授课无关的窗口）",
    "ad（广告、弹窗、推广、屏保广告）",
    "system（登录、锁屏、系统通知、设备连接提示）",
    "teacher（画面主要是教师，没有可读的讲授材料）",
    "other",
)
TEACHING_KINDS = {"slide", "document", "diagram", "demo", "video_playback", "projection", "blackboard"}
_KIND_LIST = "、".join(K.split("（")[0] for K in KINDS)

_CLASSIFY_T = """这是教室录播画面的一张采样帧，请只判断画面类型，只输出一个 JSON 对象：
{{"kind": {_KIND_LIST} 之一,
 "is_teaching_content": true 或 false}}
"教学内容"指任何向课堂展示的讲授材料：任何学科的课件、讲义、教材、试卷、公式推导、图表、地图、图片、网页、音视频材料、软件或实验演示、板书、投影内容。
不属于教学内容：空桌面、文件管理器、广告弹窗、登录锁屏、系统通知、与授课无关的个人内容。"""

_PROMPT_T = """这是大学课堂录播中教室电脑屏幕的第 {index}/{total} 张截图，出现在课程时间 {ts}。
判断画面类型并提取信息，只输出一个 JSON 对象：
{{"kind": {_KIND_LIST} 之一,
 "is_courseware": true 或 false（画面是否呈现了教学内容：任何学科的课件、讲义、文档、图表、演示、教学音视频；桌面/广告/系统界面为 false）,
 "title": "页面标题，没有则空字符串",
 "summary": "两句话概括这一页讲什么，保留关键术语、命令、公式、定义、数字",
 "keywords": ["3 到 6 个"],
 "formulas_or_code": ["原样摘录的公式、命令、代码、定义或关键句，没有则空数组"],
 "readable": true 或 false（文字是否清晰可读）}}"""

_CAMERA_T = """这是大学课堂录播中教室机位摄像机的第 {index}/{total} 张关键帧（已尽量裁到投影幕区域），出现在课程时间 {ts}。
画面可能是投影幕上的课件、白板/黑板板书，或教师本人。只输出一个 JSON 对象：
{{"kind": {_KIND_LIST} 之一,
 "is_courseware": true 或 false（是否能读到讲授材料：投影内容或板书为 true）,
 "title": "投影页标题或板书主题，没有则空字符串",
 "summary": "两句话概括读到的内容，尽量忠实转写投影/板书上的文字、公式、定义；看不清的地方如实说明",
 "keywords": ["3 到 6 个"],
 "formulas_or_code": ["原样转写的公式、代码、定义、关键句，没有则空数组"],
 "readable": true 或 false（文字是否清晰可读）}}"""

# 类别表在模块加载时填入；调用期的 {index}/{total}/{ts} 仍用 format
CLASSIFY_PROMPT = _CLASSIFY_T.replace("{_KIND_LIST}", _KIND_LIST)
PROMPT = _PROMPT_T.replace("{_KIND_LIST}", _KIND_LIST)
CAMERA_PROMPT = _CAMERA_T.replace("{_KIND_LIST}", _KIND_LIST)

# 缓存版本 = 三个 prompt 模板的哈希：模板一改，旧描述自动作废，不用记得手动加版本号
PROMPT_VERSION = hashlib.sha256(f"{CLASSIFY_PROMPT}\n{PROMPT}\n{CAMERA_PROMPT}".encode()).hexdigest()[:12]
_LEGACY_VERSIONS = (1,)  # 手填常量时代的值：当时没有哈希，与今天的判定一致，按当前版本看待


def prompt_version_ok(recorded) -> bool:
    """产物里记的版本能否当作当前版本：没记（更旧的产物）或等于手填的 1 都算。"""
    return recorded is None or recorded == PROMPT_VERSION or recorded in _LEGACY_VERSIONS


def prepare_image(
    path: Path, *, max_side: int = 1280, quality: int = 85, crop: tuple[int, int, int, int] | None = None
) -> bytes:
    """长边缩到 max_side 以内（token 与像素成正比），转 JPEG；给定 crop 时先裁再放大到长边 ≥ 1000。"""
    with Image.open(path) as im:
        im = im.convert("RGB")
        if crop:
            im = im.crop(crop)
            if max(im.size) < 1000:
                scale = 1000 / max(im.size)
                im = im.resize((int(im.width * scale), int(im.height * scale)), Image.Resampling.LANCZOS)
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def fmt_ts(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def load_frames(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    return {int(d["index"]): d for d in json.loads(path.read_text(encoding="utf-8"))}


def load_frames_list(path: Path) -> list[dict]:
    """frames.json → 按 index 排序的列表；文件不存在返回空列表。"""
    frames = load_frames(path)
    return [frames[k] for k in sorted(frames)]


def _save(path: Path, frames: dict[int, dict]) -> None:
    items = [frames[k] for k in sorted(frames)]
    path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- VGA 可用性判定 ----------
def judge_screen(
    vision_model: VisionModel,
    seg_dir: Path,
    slides: list[Slide],
    video: Path,
    *,
    max_samples: int = 8,
    min_teaching: int = 2,
) -> dict:
    """抽 ≤ max_samples 张 VGA 画面让视觉模型分类；教学内容 ≥ min_teaching 张才算可用。

    纯几何规则（静止/补帧）不可靠：动态广告会让 scene 检测频繁触发。结果带模型名写进 state，由调用方负责缓存。
    """
    tmp = seg_dir / "slides" / "_judge"
    if len(slides) >= max_samples:
        step = len(slides) / max_samples
        picked = [(s.time, seg_dir / "slides" / s.file) for s in (slides[int(i * step)] for i in range(max_samples))]
    else:
        duration = duration_seconds(probe(video))
        every = max(duration / max_samples, 1.0)
        picked = sample_frames(video, tmp, every_s=every)[:max_samples]
    results = []
    for t, path in picked:
        out = vision_model.images_json(CLASSIFY_PROMPT, [prepare_image(path, max_side=768)], thinking=False)
        kind = str(out.data.get("kind") or "other").split("（")[0].strip()
        flag = as_bool(out.data.get("is_teaching_content"))
        if flag is None:
            flag = kind in TEACHING_KINDS
        results.append({"time": round(t, 1), "kind": kind, "teaching": bool(flag)})
    teaching = sum(r["teaching"] for r in results)
    if tmp.exists():
        for old in tmp.iterdir():
            old.unlink()
        tmp.rmdir()
    return {
        "usable": teaching >= min_teaching,
        "teaching": teaching,
        "total": len(results),
        "samples": results,
        "model": vision_model.name,
        "prompt_version": PROMPT_VERSION,
    }


# ---------- 关键帧描述 ----------
def _stale(frames: dict[int, dict], slides: list[Slide], camera: bool, model: str) -> bool:
    for s in slides:
        f = frames.get(s.index)
        if f and (f.get("time") != s.time or f.get("file") != s.file or (f.get("source") == "camera") != camera):
            return True
    # 旧版 frames.json 没记 prompt_version：按当前版本看待，升级不至于整课重算
    return any(f.get("model") != model or not prompt_version_ok(f.get("prompt_version")) for f in frames.values())


def _backfill(frames: dict[int, dict], offset_s: float, model: str | None = None) -> bool:
    """旧版 frames.json 没有 source / model / prompt_version 字段，在这里补记；audio_time 是派生字段，每次按当前偏移重算。

    model 只在调用方明确给出当前模型时补记：旧描述按当前模型看待，升级后不至于整课重算。
    """
    changed = False
    for f in frames.values():
        # 描述本身与时间轴无关（prompt 里用的是帧在视频里的时间），所以换了偏移只改标注，不重新描述
        audio_time = round(float(f.get("time") or 0) + offset_s, 3)
        if f.get("audio_time") != audio_time:
            f["audio_time"] = audio_time
            changed = True
        if "source" not in f:
            f["source"] = "screen"
            changed = True
        if model is not None and "model" not in f:
            f["model"] = model
            changed = True
        if "prompt_version" not in f:
            f["prompt_version"] = PROMPT_VERSION
            changed = True
    return changed


def describe_slides(
    vision_model: VisionModel,
    slide_dir: Path,
    slides: list[Slide],
    *,
    offset_s: float = 0.0,
    concurrency: int = 4,
    force: bool = False,
    camera: bool = False,
    on_done: Callable[[dict], None] | None = None,
) -> list[dict]:
    """offset_s：帧时间戳换算到音频时间轴的偏移。camera=True 时机位帧先裁投影区再送模型。

    frames.json 只保留当前清单里的帧：重新切页后不在清单里的旧描述一并清掉。
    """
    model = vision_model.name
    out = slide_dir / "frames.json"
    frames: dict[int, dict] = {} if force else load_frames(out)
    orphans = False
    if slides:
        wanted = {s.index for s in slides}
        orphans = not wanted >= set(frames)
        frames = {i: f for i, f in frames.items() if i in wanted}
    if frames or orphans:
        filled = _backfill(frames, offset_s, model)
        if _stale(frames, slides, camera, model):
            frames = {}  # 清单变了（换视觉源或改采样间隔）或换了视觉模型 / prompt：旧描述不可信
        elif filled or orphans:
            _save(out, frames)
    todo = [s for s in slides if s.index not in frames]
    prompt_tpl = CAMERA_PROMPT if camera else PROMPT
    if todo:
        _save(out, frames)
        total = len(slides)

        def one(s: Slide) -> dict:
            path = slide_dir / s.file
            crop = None
            if camera:
                with Image.open(path) as im:
                    crop = projection_roi(im)
            image = prepare_image(path, crop=crop)
            result = vision_model.images_json(
                prompt_tpl.format(index=s.index, total=total, ts=fmt_ts(s.time)), [image], thinking=False
            )
            obj, usage = result.data, result.usage
            return {
                "index": s.index,
                "time": s.time,
                "audio_time": round(s.time + offset_s, 3),
                "file": s.file,
                "source": "camera" if camera else "screen",
                "model": model,
                "prompt_version": PROMPT_VERSION,
                "cropped": bool(crop),
                **{
                    k: obj.get(k)
                    for k in (
                        "kind",
                        "is_courseware",
                        "title",
                        "summary",
                        "keywords",
                        "formulas_or_code",
                        "readable",
                    )
                },
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                },
            }

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(one, s): s for s in todo}
            for fut in as_completed(futures):
                item = fut.result()
                frames[item["index"]] = item
                _save(out, frames)
                if on_done:
                    on_done(item)
    return [frames[k] for k in sorted(frames)]


def as_bool(value) -> bool | None:
    """结构化输出只保证是 JSON：布尔偶尔会写成字符串（"false" / "True" / "否"）。认不出的返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "y", "1", "是"):
            return True
        if v in ("false", "no", "n", "0", "否"):
            return False
    if isinstance(value, int):
        return bool(value)
    return None


def is_courseware(f: dict) -> bool:
    flag = as_bool(f.get("is_courseware"))
    return flag is True or f.get("kind") == "slide"


def courseware_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if is_courseware(f)]
