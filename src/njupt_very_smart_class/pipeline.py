"""流水线编排：fetch → prepare → transcribe → vision → summarize → omni → fuse → render。

各阶段幂等，完成情况记在课时目录的 state.json。


SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from . import asr, fuse, lesson, render, slides, summarize, video_llm, vision
from .auth import AuthError
from .bailian import dashscope_model_set
from .config import effective_models
from .models import ModelSet, NullVideoModel
from .vendor.njupt_smartclass import NjuptSmartclass, NjuptSmartclassVideoSummary

STAGES = ("fetch", "prepare", "transcribe", "vision", "summarize", "omni", "fuse", "render")
Log = Callable[[str], None]


@dataclasses.dataclass
class RunOptions:
    types: tuple[str, ...] = lesson.download.DEFAULT_TYPES
    minimal: bool = True
    threshold: float = slides.DEFAULT_THRESHOLD
    max_slides: int | None = slides.DEFAULT_MAX_SLIDES
    max_gap: float = slides.DEFAULT_MAX_GAP
    concurrency: int = 4
    force_vision: bool = False
    omni_sample_seconds: float = 5.0
    camera_every_s: float = 30.0
    prune_video: bool = False
    omni_inputs: tuple[str, ...] = ("VGA",)
    force_omni: bool = False
    force_fuse: bool = False
    min_free_bytes: int = 2 << 30
    models: dict = dataclasses.field(default_factory=lambda: dict(effective_models()))  # 角色名 → 模型名


def parse_stages(spec: str) -> tuple[str, ...]:
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    bad = [s for s in wanted if s not in STAGES]
    if bad:
        raise ValueError(f"未知阶段 {bad}，可选：{', '.join(STAGES)}")
    return tuple(s for s in STAGES if s in wanted)


# 会调用模型的阶段：只有请求了其中之一才需要 API Key，纯本地的重渲染不用
MODEL_STAGES = frozenset({"transcribe", "vision", "summarize", "omni", "fuse"})


class _NoteBook:
    """处理提示按产生它的阶段记账：重跑只重算请求过的阶段，其余阶段的提示沿用上次。

    对外仍是摊平的列表（state.notes、summary 的「处理提示」、run / status --json）。
    """

    ORDER = ("fetch", "prepare", "transcribe", "vga", "vision", "summarize", "omni", "fuse", "render")
    LEGACY = "(旧)"  # 旧版 state 只有摊平的 notes，不知道各条来自哪个阶段

    def __init__(self, by_stage: dict[str, list[str]] | None = None) -> None:
        self.by_stage: dict[str, list[str]] = {
            k: [n for n in v if isinstance(n, str)] for k, v in (by_stage or {}).items() if isinstance(v, list)
        }
        self.stage = "prepare"

    def append(self, note: str) -> None:
        self.by_stage.setdefault(self.stage, []).append(note)

    def extend(self, items) -> None:
        for item in items:
            self.append(item)

    def flat(self) -> list[str]:
        known = [n for s in self.ORDER for n in self.by_stage.get(s, [])]
        extra = [n for s, ns in self.by_stage.items() if s not in self.ORDER for n in ns]
        return list(dict.fromkeys(known + extra))  # 旧版摊平的提示与本阶段的可能重复，去重保序

    def snapshot(self) -> dict[str, list[str]]:
        return {k: v for k, v in self.by_stage.items() if v}

    def inherit(self, by_stage: dict, attempted: set[str]) -> None:
        """把上次留下的、本次没重算的阶段的提示接进来（本次已写的保留）。"""
        for stage, items in by_stage.items():
            if stage not in attempted and isinstance(items, list) and items:
                self.by_stage.setdefault(stage, [n for n in items if isinstance(n, str)])

    @contextmanager
    def tag(self, stage: str):
        """把 with 块里的提示记到指定阶段，不受当前所处阶段影响。"""
        outer = self.stage
        self.stage = stage
        try:
            yield
        finally:
            self.stage = outer


def _has_video(seg_dir: Path, kind: str) -> bool:
    """原件在，或者 --prune-video 之后留下的转码件在。"""
    return (seg_dir / f"{kind}.mp4").exists() or (seg_dir / f"{kind}_640.mp4").exists()


def _attempted(wants: set[str], needs_media: bool) -> set[str]:
    """本次运行会重写结论的阶段：请求了的；prepare 只要跑了（后续阶段需要媒体）它的提示就是当下的事实；
    视觉源判定的提示与错误跟 vision / omni 一起重算。"""
    attempted = set(wants)
    if wants & {"vision", "omni"}:
        attempted.add("vga")
    if needs_media:
        attempted.add("prepare")
    return attempted


def _inherit_state(lesson_dir: Path, attempted: set[str], notes: _NoteBook, errors: dict[str, str]) -> None:
    """没重算的阶段沿用上次的 errors 与处理提示：只重渲染时，摘要里「哪里可能不完整」不能凭空少掉。"""
    previous = load_state(lesson_dir)
    by_stage = previous.get("notes_by_stage")
    if not isinstance(by_stage, dict):
        by_stage = {_NoteBook.LEGACY: [n for n in (previous.get("notes") or []) if isinstance(n, str)]}
    # 旧版摊平的提示不知道各条来自哪个阶段：只重渲染时原样保留，重算过会写提示的阶段后不再保留
    if attempted & (set(_NoteBook.ORDER) - {"render", "prepare"}):
        by_stage.pop(_NoteBook.LEGACY, None)
    notes.inherit(by_stage, attempted)
    old_errors = previous.get("errors")
    if isinstance(old_errors, dict):
        for key, msg in old_errors.items():
            owner = "vga" if key.startswith("vga") else key  # vga-judge / vga-switch / vga-extra 属于视觉源判定
            if owner not in attempted and isinstance(msg, str):
                errors.setdefault(key, msg)


def load_state(lesson_dir: Path) -> dict:
    path = lesson_dir / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _mark(lesson_dir: Path, stage: str, **extra) -> None:
    state = load_state(lesson_dir)
    state.setdefault("stages", {})[stage] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **extra}
    (lesson_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _sig(paths: list[Path]) -> tuple:
    """一组产物文件的内容签名：阶段前后各取一次，就知道这个阶段是否真的重写了产物（而不是命中缓存）。"""
    return tuple(hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths)


def _run_segment(
    m: lesson.SegmentMedia,
    *,
    models: ModelSet | None,
    opts: RunOptions,
    wants: set[str],
    sc: NjuptSmartclass | None,
    meta: dict,
    course_info: str,
    course_name: str,
    notes: _NoteBook,
    errors: dict[str, str],
    log: Log,
) -> None:
    """一个录制段的全部阶段。单阶段失败只记录到 errors，后续阶段继续，摘要总是尽量渲染。"""
    state = {"m": m}  # 切换视觉源后要替换 m.slides，用容器避免在闭包里重新绑定
    seg_dir = m.dir
    ctx: dict = {
        "failed": False,  # 本段本次有阶段失败：--prune-video 不删原件，修好重跑时不用重新下载
        "vga_unusable": False,
        "camera": False,
        "frame_source": "VGA.mp4",
        "extra_dir": None,
        "visual_ready": False,
        "c_inputs_changed": False,  # 本次运行重写了转写或课件描述：摘要、融合不能沿用旧结果
        "a_changed": False,  # 本次运行重算了视频模式的结果：融合不能沿用旧结果
    }

    def fail(stage: str, exc: Exception) -> None:
        ctx["failed"] = True
        errors[stage] = str(exc)[:300]
        notes.append(f"阶段 {stage} 失败：{str(exc)[:160]}")
        log(f"[{stage}] 失败：{exc}")

    def ensure_visual() -> None:
        """VGA 可能只是空桌面或广告（老师自带电脑投屏）：判它有没有教学内容，没有就把视觉源切到机位流。

        判定本身出错时按 VGA 可用继续，只记提示。
        """
        if ctx["visual_ready"]:
            return
        ctx["visual_ready"] = True
        cached = load_state(seg_dir).get("vga_judge")
        if not (seg_dir / "VGA.mp4").exists():
            # 原件已删（--prune-video）无从再判：沿用上次的结论，免得把桌面 / 广告的转码件喂给视频模式
            ctx["vga_unusable"] = isinstance(cached, dict) and cached.get("usable") is False
            return
        if not wants & {"vision", "omni"}:
            return
        with notes.tag("vga"):  # 这些提示属于视觉源判定，vision 或 omni 哪个先跑到都记在同一处
            _judge_and_switch()

    def _judge_and_switch() -> None:
        try:
            verdict = _judge_vga(models, seg_dir, state["m"], sc, meta.get("NewID", ""), log, force=opts.force_vision)
        except Exception as exc:
            fail("vga-judge", exc)
            return
        if not verdict["usable"]:
            ctx["vga_unusable"] = True
            notes.append(
                f"屏幕流无教学内容（{verdict['teaching']}/{verdict['total']} 张样本是教学内容），视觉源切换到机位流"
            )
            log(f"[vga] {seg_dir.name}: 屏幕流无教学内容（{verdict['teaching']}/{verdict['total']}），切换到机位流")
            try:
                new_slides, ctx["frame_source"] = _switch_to_camera(
                    seg_dir,
                    sc,
                    meta.get("NewID", ""),
                    log,
                    every_s=opts.camera_every_s,
                    min_free_bytes=opts.min_free_bytes,
                )
            except Exception as exc:
                fail("vga-switch", exc)
                new_slides, ctx["frame_source"] = [], "VGA.mp4"
            state["m"] = dataclasses.replace(state["m"], slides=new_slides)
            ctx["camera"] = ctx["frame_source"] != "VGA.mp4"
        elif (seg_dir / "Video1.mp4").exists() and verdict["teaching"] <= 5:
            # 混合课：一部分时间用教室电脑、一部分用自带电脑，机位流补一套关键帧
            try:
                slides.camera_keyframes(
                    seg_dir / "Video1.mp4", seg_dir, every_s=opts.camera_every_s, subdir="slides_camera"
                )
                ctx["extra_dir"] = seg_dir / "slides_camera"
                notes.append(
                    f"屏幕流只有 {verdict['teaching']}/{verdict['total']} 张样本是教学内容，"
                    "已额外提取机位流关键帧补充投影内容"
                )
                log(f"[vga] {seg_dir.name}: 部分教学内容在投影上，补充机位关键帧")
            except Exception as exc:
                fail("vga-extra", exc)

    def s_transcribe() -> None:
        transcript_path = state["m"].audio.with_name("transcript.json")
        before = _sig([transcript_path])
        t = asr.transcribe(models.asr, state["m"].audio)
        ctx["c_inputs_changed"] |= _sig([transcript_path]) != before
        if t.no_speech:
            notes.append(f"无有效语音（{t.reason}），转写模式不写笔记，仅基于画面")
            log(f"[transcribe] {seg_dir.name}: 无有效语音（{t.reason}），转写模式将跳过")
            return
        if t.low_speech:
            ratio = t.speech_ratio or 0
            notes.append(
                f"有效语音只占 {ratio:.0%}（{t.duration_ms / 60000:.1f} / "
                f"{state['m'].audio_seconds / 60:.1f} min），可能大半时间无人说话或噪音被误识别，摘要可信度降低"
            )
        log(
            f"[transcribe] {seg_dir.name}: {len(t.segments)} 句 / {t.chars} 字 / {t.duration_ms / 60000:.1f} min"
            + ("（低语音比）" if t.low_speech else "")
        )

    def _describe(subdir: str, slide_list: list[slides.Slide], camera: bool) -> list[dict]:
        def on_done(f: dict) -> None:
            ctx["c_inputs_changed"] = True  # 有帧是新描述的
            log(
                f"[vision] #{f['index']:02d} {vision.fmt_ts(f['time'])} {f.get('kind', '?'):8s} "
                f"《{f.get('title') or ''}》 {(f.get('usage') or {}).get('prompt_tokens')} tok"
            )

        return vision.describe_slides(
            models.vision,
            seg_dir / subdir,
            slide_list,
            concurrency=opts.concurrency,
            force=opts.force_vision,
            camera=camera,
            on_done=on_done,
        )

    def _unreadable_note(frames: list[dict], label: str) -> None:
        cw = vision.courseware_frames(frames)
        bad = [f for f in cw if vision.as_bool(f.get("readable")) is False]
        if cw and len(bad) >= max(1, len(cw) // 5):
            notes.append(f"{label}有 {len(bad)}/{len(cw)} 张课件文字不清晰，相关内容可能缺失或识别有误")

    def s_vision() -> None:
        ensure_visual()
        cur = state["m"]
        if not cur.slides:
            log(f"[vision] {seg_dir.name}: 无可用视觉源，跳过")
            return
        frames = _describe("slides", cur.slides, ctx["camera"])
        _unreadable_note(frames, "机位流" if ctx["camera"] else "屏幕流")
        log(
            f"[vision] {seg_dir.name}: {len(frames)} 张（{'机位' if ctx['camera'] else '屏幕'}），"
            f"教学内容 {len(vision.courseware_frames(frames))} 张"
        )
        if ctx["extra_dir"]:
            extra_slides = [
                slides.Slide(index=int(d["index"]), time=float(d["time"]), file=d["file"])
                for d in json.loads((ctx["extra_dir"] / "slides.json").read_text(encoding="utf-8"))
            ]
            extra = _describe("slides_camera", extra_slides, True)
            _unreadable_note(extra, "机位补帧")
            log(f"[vision] {seg_dir.name}: 机位补充 {len(extra)} 张")

    def s_summarize() -> None:
        if not (seg_dir / "transcript.json").exists():
            # transcribe 失败或没跑：按"没有转写"处理，融合/渲染照常进行，原因写进处理提示
            failed = "transcribe" in errors
            summarize.write_no_transcript_result(
                seg_dir,
                model=models.text.name,
                course_info=course_info,
                course_name=course_name,
                reason="transcribe_failed" if failed else "transcript_missing",
            )
            why = f"transcribe 阶段失败：{errors['transcribe']}" if failed else "没有 transcript.json"
            notes.append(
                "没有转写结果，转写模式不写笔记，仅基于画面"  # 失败原因已由 errors 带进处理提示
                if failed
                else "没有 transcript.json（未执行 transcribe），转写模式不写笔记，仅基于画面"
            )
            log(f"[summarize] {seg_dir.name}: 没有转写结果（{why}），转写模式跳过")
            return
        c = summarize.summarize_segment(
            models.text,
            seg_dir,
            course_info=course_info,
            course_name=course_name,
            processing_notes=notes.flat(),
            on_done=lambda n: log(f"[summarize] 段 {n['index']}: {n.get('topics')}"),
            force=ctx["c_inputs_changed"],
        )
        if c.get("no_speech"):
            log(f"[summarize] {seg_dir.name}: 无转写（{c.get('reason')}），转写模式跳过")
        else:
            log(f"[summarize] {seg_dir.name}: {len(c['chunks'])} 段 → {len(c['result'].get('chapters', []))} 章")
        # 输出被 max_tokens 截断时 JSON 已按闭合修复，看不出来缺了什么：只能如实记一笔
        cut = [n["index"] for n in c.get("chunks", []) if n.get("_truncated")]
        if cut:
            notes.append(f"有 {len(cut)} 段分段笔记的模型输出被长度上限截断（段 {cut}），对应内容可能缺失")
        if (c.get("result") or {}).get("_truncated"):
            notes.append("全课笔记的模型输出被长度上限截断，末尾内容可能缺失")

    def s_omni() -> None:
        if isinstance(models.video, NullVideoModel):
            log("[omni] 未配置视频模型，跳过视频模式")
            return
        ensure_visual()
        inputs = [k for k in opts.omni_inputs if _has_video(seg_dir, k)]
        if ctx["vga_unusable"]:
            inputs = [k for k in inputs if k != "VGA"]  # 屏幕流是桌面/广告，不喂
        if not inputs and _has_video(seg_dir, "Video1"):
            inputs = ["Video1"]
            log(f"[omni] {seg_dir.name}: 视频模式改用 Video1")
        if not inputs:
            notes.append("没有可用的视频流，视频模式跳过")
            return
        outputs = [seg_dir / f"a_result_{k}.json" for k in inputs]
        before = _sig(outputs)
        for kind in inputs:
            small = seg_dir / f"{kind}_640.mp4"
            if (seg_dir / f"{kind}.mp4").exists():
                small = video_llm.transcode_for_omni(seg_dir / f"{kind}.mp4", small)
            # 原件已被 --prune-video 删除时，直接用留下的转码件
            a = video_llm.analyze_video(
                models.video,
                small,
                course_info=course_info,
                sample_seconds=opts.omni_sample_seconds,
                out=seg_dir / f"a_result_{kind}.json",
                force=opts.force_omni,
                source=kind,
            )
            res = a["result"]
            if res.get("_truncated"):
                notes.append(f"视频模式（{kind}）的输出被长度上限截断，末尾内容可能缺失")
            log(
                f"[omni] {seg_dir.name}/{kind}: 视频 {a['video']['bytes'] / 1e6:.0f} MB, "
                f"{len(res.get('chapters', []))} 章, {len(res.get('screen_events', []))} 个屏幕事件, "
                f"{a['usage'].get('prompt_tokens')} in / {a['usage'].get('completion_tokens')} out"
            )

        ctx["a_changed"] |= _sig(outputs) != before

    def s_fuse() -> None:
        final = fuse.fuse(
            models.text,
            seg_dir,
            course_info=course_info,
            force=opts.force_fuse or ctx["c_inputs_changed"] or ctx["a_changed"],
        )
        if final is None:
            log(f"[fuse] {seg_dir.name}: 没有任何 a_result_*.json，跳过融合（render 退回转写模式）")
            return
        res = final["result"]
        if res.get("_truncated"):
            notes.append("两种模式的融合结果被长度上限截断，末尾内容可能缺失")
        log(
            f"[fuse] {seg_dir.name}: {len(res.get('chapters', []))} 章, {len(res.get('demos', []))} 个演示, "
            f"{len(res.get('asr_corrections', []))} 条勘误, {len(res.get('uncertain', []))} 待复核"
        )

    def s_render() -> None:
        frames_list = vision.load_frames_list(seg_dir / "slides" / "frames.json")
        if ctx["extra_dir"]:
            # 机位补帧在另一个目录，和屏幕流截图同名：链接要带上它自己的目录
            frames_list += [
                dict(f, dir=ctx["extra_dir"].name) for f in vision.load_frames_list(ctx["extra_dir"] / "frames.json")
            ]
        c_path = seg_dir / "c_result.json"
        if not c_path.exists():
            log(f"[render] {seg_dir.name}: 没有 c_result.json，先执行 summarize")
            notes.append(f"{seg_dir.name}: 没有 c_result.json，未渲染摘要")
            return
        c = json.loads(c_path.read_text(encoding="utf-8"))
        final_path = seg_dir / "final.json"
        if final_path.exists():
            summary_obj, a = json.loads(final_path.read_text(encoding="utf-8")), None
        else:
            summary_obj, a = c, None
            a_paths = sorted(seg_dir.glob("a_result_*.json"))
            if a_paths:
                a = json.loads(a_paths[0].read_text(encoding="utf-8"))
        out = render.render_summary(
            summary_obj,
            meta,
            frames_list,
            seg_dir / "summary.md",
            a_result=a,
            chunks=c.get("chunks"),
            notes=notes.flat(),
            errors=dict(errors),
        )
        log(f"[render] {out}")

    ordered = (
        ("transcribe", s_transcribe),
        ("vision", s_vision),
        ("summarize", s_summarize),
        ("omni", s_omni),
        ("fuse", s_fuse),
        ("render", s_render),
    )
    for name, fn in ordered:
        if name not in wants:
            continue
        notes.stage = name  # 这个阶段新写的提示记在它名下；上次的已在 run_lesson 里按「请求了哪些阶段」清掉
        try:
            fn()
        except Exception as exc:  # 单阶段失败不拖垮整节课
            fail(name, exc)
    if opts.prune_video:
        if ctx["failed"]:
            log(f"[prune] {seg_dir.name}: 本次有阶段失败，保留视频原件，修好重跑后再删")
        else:
            removed = lesson.prune_videos(seg_dir)
            if removed:
                log(f"[prune] {seg_dir.name}: 已删除视频原件 {removed}（保留 wav / 关键帧 / *_640.mp4）")


def _delta(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """累计计数减去基线，只留正增量。"""
    return {k: v - baseline.get(k, 0) for k, v in current.items() if v - baseline.get(k, 0) > 0}


def _delta_detail(current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for model, d in current.items():
        base = baseline.get(model) or {}
        delta = {k: int(d.get(k) or 0) - int(base.get(k) or 0) for k in ("input", "output")}
        if delta["input"] > 0 or delta["output"] > 0:
            out[model] = delta
    return out


def run_lesson(
    sc: NjuptSmartclass | None,
    new_id: str,
    *,
    stages: tuple[str, ...] = STAGES,
    opts: RunOptions | None = None,
    models: ModelSet | None = None,
    log: Log = print,
    lesson_dir: Path | None = None,
) -> dict:
    """逐阶段处理一节课。fetch/prepare 失败直接抛出（没有媒体无从继续）；其余阶段失败只记录到
    state.errors 并继续，摘要总是尽量渲染出来，并在「处理提示」里写明哪里降级了。

    请求了的阶段用本次结果覆盖上次的 errors 与处理提示，没请求的沿用：--stages 子集重跑不会把别的阶段的结论冲掉。
    lesson_dir 指定时就在这个目录上处理（--local），不去数据目录里按 NewID 找。
    """
    opts = opts or RunOptions()
    wants = set(stages)
    # 只有请求了会调模型的阶段才需要 API Key：纯本地的重渲染、重合并不该因为缺 key 失败
    models = models or (dashscope_model_set(**opts.models) if wants & MODEL_STAGES else None)
    baseline = models.spent() if models else {}
    baseline_detail = models.spent_detail() if models else {}
    needs_media = bool(wants & {"prepare", "transcribe", "vision", "summarize", "omni", "fuse", "render"})
    notes = _NoteBook()
    errors: dict[str, str] = {}

    if "fetch" in wants:
        if sc is None:
            raise ValueError("fetch 阶段需要已登录的智慧课堂客户端")
        notes.stage = "fetch"
        info, lesson_dir, segments = lesson.fetch_lesson(
            sc, new_id, types=opts.types, minimal=opts.minimal, min_free_bytes=opts.min_free_bytes
        )
        available = sorted({s.kind for seg in segments for s in seg.sources})
        on_disk = sorted({k.removesuffix(".mp4") for seg in segments for k in seg.kept})
        missing = sorted(set(opts.types) - set(available))
        skipped = sorted(set(available) - set(on_disk))
        log(
            f"[fetch] {info.course_name} {info.title} → {lesson_dir}（{len(segments)} 段）；"
            f"可用流 {available}，已下载 {on_disk}"
            + (f"，该录像缺少 {missing}" if missing else "")
            + (f"，按默认策略未下载 {skipped}（需要时 --full-download）" if skipped else "")
        )
        if missing:
            notes.append(f"录像缺少流 {missing}（教室没有该设备）")
        _mark(lesson_dir, "fetch", segments=len(segments))
    else:
        lesson_dir = lesson_dir or lesson.find_lesson_dir(new_id)
        log(f"[fetch] 跳过，使用 {lesson_dir}")
    _inherit_state(lesson_dir, _attempted(wants, needs_media), notes, errors)

    media: list[lesson.SegmentMedia] = []
    if needs_media:
        notes.stage = "prepare"
        media = lesson.prepare_lesson(
            lesson_dir, threshold=opts.threshold, max_slides=opts.max_slides, max_gap=opts.max_gap
        )
        for m in media:
            log(
                f"[prepare] {m.dir.name}: audio {m.audio_seconds / 60:.1f} min ← {m.audio_source}; "
                f"slides {len(m.slides)}"
            )
            for n in m.notes:
                log(f"[prepare] {m.dir.name}: {n}")
        notes.extend(n for m in media for n in m.notes)
        if "prepare" in wants:
            _mark(lesson_dir, "prepare", segments=len(media))

    meta = json.loads((lesson_dir / "meta.json").read_text(encoding="utf-8"))
    course_name = meta.get("CourseName", "")
    course_info = f"{course_name} · {meta.get('Title', '')} · {meta.get('StartTime', '')[:16]}".strip()

    for m in media:
        _run_segment(
            m,
            models=models,
            opts=opts,
            wants=wants,
            sc=sc,
            meta=meta,
            course_info=course_info,
            course_name=course_name,
            notes=notes,
            errors=errors,
            log=log,
        )

    for stage in ("transcribe", "vision", "summarize", "omni", "fuse", "render"):
        if stage in wants and stage not in errors and (stage != "render" or find_summary(lesson_dir) is not None):
            _mark(lesson_dir, stage)
    if "render" in wants and "render" not in errors:
        merged = compose_lesson_summary(lesson_dir, meta)
        if merged:
            log(f"[render] 课时摘要 {merged}")
    state = load_state(lesson_dir)
    # errors 与处理提示按阶段合并（见 _inherit_state）：请求过的阶段是本次的结论，没请求的沿用上次
    state["notes"] = notes.flat()
    state["notes_by_stage"] = notes.snapshot()
    state["errors"] = errors
    # 批量里 ModelSet 跨课复用，计数是累计值；state.tokens / tokens_detail 只记本课本次的增量
    spent = _delta(models.spent() if models else {}, baseline)
    if spent:
        total = {k: v for k, v in state.get("tokens", {}).items() if isinstance(v, int)}
        for model_name, n in spent.items():
            total[model_name] = total.get(model_name, 0) + n
        state["tokens"] = total
    spent_detail = _delta_detail(models.spent_detail() if models else {}, baseline_detail)
    if spent_detail:
        total_detail = {k: v for k, v in state.get("tokens_detail", {}).items() if isinstance(v, dict)}
        for model_name, d in spent_detail.items():
            item = total_detail.setdefault(model_name, {"input": 0, "output": 0})
            item["input"] += d["input"]
            item["output"] += d["output"]
        state["tokens_detail"] = total_detail
    (lesson_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"lesson_dir": lesson_dir, "state": state, "tokens": spent, "tokens_detail": spent_detail}


# ---------- 批量 ----------
def _judge_vga(models, seg_dir: Path, m, sc, new_id: str, log: Log, *, force: bool = False) -> dict:
    """判定 VGA 是否有教学内容；结果缓存在 state.json，换视觉模型或 --force-vision 才重判。"""
    state = load_state(seg_dir)
    cached = state.get("vga_judge")
    usable = isinstance(cached, dict) and "usable" in cached
    # 旧版判定结果没记模型名 / prompt 版本：按当前看待
    if (
        usable
        and not force
        and cached.get("model") in (None, models.vision.name)
        and vision.prompt_version_ok(cached.get("prompt_version"))
    ):
        return cached
    verdict = vision.judge_screen(models.vision, seg_dir, m.slides, seg_dir / "VGA.mp4")
    state["vga_judge"] = verdict
    (seg_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return verdict


def _switch_to_camera(seg_dir: Path, sc, new_id: str, log: Log, *, every_s: float = 30.0, min_free_bytes: int = 0):
    """补下 Video1（需要会话）并生成机位关键帧；没有 Video1 时返回 ([], "VGA.mp4")。"""
    cam = seg_dir / "Video1.mp4"
    if not cam.exists() and sc is not None:
        log(f"[vga] {seg_dir.name}: 补下机位流 Video1（约 1 GB）")
        lesson.fetch_lesson(sc, new_id, types=("Video1",), minimal=False, min_free_bytes=min_free_bytes)
    if not cam.exists():
        log(f"[vga] {seg_dir.name}: 没有 Video1.mp4，无法切换视觉源；有会话时请 vsc fetch {new_id[:8]} --types Video1")
        return [], "VGA.mp4"
    keyframes = slides.camera_keyframes(cam, seg_dir, every_s=every_s)
    log(f"[vga] {seg_dir.name}: 机位关键帧 {len(keyframes)} 张")
    return keyframes, "Video1.mp4"


def lesson_done(lesson_dir: Path, stages: tuple[str, ...]) -> bool:
    """请求的阶段都已在 state.json 里标记，且有摘要（顶层或任一分段）。"""
    state = load_state(lesson_dir)
    marked = set(state.get("stages", {}))
    return all(s in marked for s in stages) and find_summary(lesson_dir) is not None


def find_summary(lesson_dir: Path) -> Path | None:
    """顶层 summary.md（多段录制的合并版）；旧课时没有顶层文件时退回第一个分段的。"""
    top = lesson_dir / "summary.md"
    if top.exists():
        return top
    for seg in lesson.segment_dirs(lesson_dir):
        if (seg / "summary.md").exists():
            return seg / "summary.md"
    return None


def compose_lesson_summary(lesson_dir: Path, meta: dict) -> Path | None:
    """把各分段的 summary.md 合成课时级 summary.md：单段原样复制，多段每段降一级标题后依次拼接。"""
    parts = [seg / "summary.md" for seg in lesson.segment_dirs(lesson_dir) if (seg / "summary.md").exists()]
    if not parts:
        return None
    out = lesson_dir / "summary.md"
    if len(parts) == 1:
        out.write_text(parts[0].read_text(encoding="utf-8"), encoding="utf-8")
        return out
    course = meta.get("CourseName", "")
    start = str(meta.get("StartTime", ""))[:16].replace("T", " ")
    lines = [
        f"# {course} {start}".strip(),
        "",
        f"> 多段录制，共 {len(parts)} 段；各段摘要依次如下，时间戳为段内时间。",
        "",
    ]
    for i, part in enumerate(parts, 1):
        lines.extend(["---", ""])
        fenced = False
        for line in part.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
                lines.append(line)
            elif fenced:
                lines.append(line)  # 代码块里的 # 是注释，不是标题
            elif line.startswith("# "):
                lines.append(f"## 第 {i} 段（{part.parent.name}）{line[2:].strip()}")
            elif line.startswith("#"):
                lines.append("#" + line)
            else:
                lines.append(line)
        lines.append("")
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


def run_batch(
    sc: NjuptSmartclass | None,
    videos: list,
    *,
    stages: tuple[str, ...] = STAGES,
    opts: RunOptions | None = None,
    models: ModelSet | None = None,
    log: Log = print,
    skip_done: bool = True,
    lesson_dirs: dict[str, Path] | None = None,
) -> dict:
    """对列表中的录播逐节执行 run_lesson；单节失败不中断，结束时返回汇总。

    lesson_dirs：--local 给出的课时目录（NewID → 目录），有就在那里处理，不去数据目录里找。
    """
    opts = opts or RunOptions()
    models = models or (dashscope_model_set(**opts.models) if set(stages) & MODEL_STAGES else None)
    results = []
    aborted = None
    for i, v in enumerate(videos, 1):
        head = f"[batch {i}/{len(videos)}] {v.start_time:%m-%d %H:%M} {v.course_name} {v.title}"
        try:
            try:
                lesson_dir = (lesson_dirs or {}).get(v.id) or lesson.find_lesson_dir(v.id)
            except ValueError:
                lesson_dir = None
            if skip_done and lesson_dir is not None and lesson_done(lesson_dir, stages):
                log(f"{head} → 已完成，跳过")
                results.append(
                    {
                        "new_id": v.id,
                        "status": "skipped",
                        "verdict": classify_lesson(lesson_dir),
                        "title": v.title,
                        "course": v.course_name,
                        "summary": str(find_summary(lesson_dir) or lesson_dir / "summary.md"),
                    }
                )
                continue
            if lesson_dir is None and "fetch" not in stages:
                raise RuntimeError("课时未下载且未请求 fetch 阶段")
            log(head)
            report = run_lesson(sc, v.id, stages=stages, opts=opts, models=models, log=log, lesson_dir=lesson_dir)
            stage_errors = report["state"].get("errors", {})
            results.append(
                {
                    "new_id": v.id,
                    "status": "partial" if stage_errors else "ok",
                    "errors": stage_errors,
                    "verdict": classify_lesson(Path(report["lesson_dir"])),
                    "title": v.title,
                    "course": v.course_name,
                    "start": v.start_time.isoformat(),
                    "summary": str(
                        find_summary(Path(report["lesson_dir"])) or Path(report["lesson_dir"]) / "summary.md"
                    ),
                    "tokens": report["state"].get("tokens", {}),
                    "tokens_detail": report["state"].get("tokens_detail", {}),
                }
            )
        except AuthError as exc:  # 会话失效时后续课时必然同样失败，提前结束
            log(f"{head} → 会话失效，终止批量：{exc}")
            results.append(
                {"new_id": v.id, "status": "error", "title": v.title, "course": v.course_name, "error": str(exc)[:500]}
            )
            aborted = "auth"
            break
        except Exception as exc:  # 单节失败不影响后续
            log(f"{head} → 失败：{exc}")
            results.append(
                {"new_id": v.id, "status": "error", "title": v.title, "course": v.course_name, "error": str(exc)[:500]}
            )
    return {
        "results": results,
        "tokens": models.spent() if models else {},
        "tokens_detail": models.spent_detail() if models else {},
        "stages": list(stages),
        "aborted": aborted,
    }


def discover_local(roots: list[Path]) -> list[tuple[NjuptSmartclassVideoSummary, Path]]:
    """本地课时目录 → [(伪列表项, 课时目录)]，供 run_batch 不登录也能跑。

    列表项只需 id / title / course_name / start_time。
    """
    from datetime import datetime

    found: dict[str, Path] = {}
    for root in roots:
        if (root / "meta.json").exists():
            found[root.name] = root
        elif root.is_dir():
            for sub in sorted(root.iterdir()):
                if sub.is_dir() and (sub / "meta.json").exists():
                    found[sub.name] = sub
    videos = []
    for new_id, d in found.items():
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        try:
            start = datetime.fromisoformat(meta.get("StartTime") or "")
        except ValueError:
            start = datetime.fromtimestamp(d.stat().st_ctime)
        videos.append(
            (
                NjuptSmartclassVideoSummary(
                    id=new_id,
                    title=meta.get("Title") or new_id,
                    start_time=start,
                    stop_time=start,
                    course_name=meta.get("CourseName") or d.parent.name,
                    teachers="",
                    classroom_name="",
                    cover_url="",
                ),
                d,
            )
        )
    videos.sort(key=lambda item: item[0].start_time)
    return videos


def classify_lesson(lesson_dir: Path) -> str:
    """ok / no_speech（无有效语音）/ asr_failed（转写阶段失败，不代表没声音）/
    empty（无教学画面内容）/ c_only（有转写但没有视频模式的结果）。

    转写是否失败看 c_result.reason 而不是 state.errors：产物里的原因不会变，而后者曾经随局部重跑被冲掉。
    """
    verdicts = set()
    for seg_dir in lesson.segment_dirs(lesson_dir):
        c_path = seg_dir / "c_result.json"
        c = json.loads(c_path.read_text(encoding="utf-8")) if c_path.exists() else {}
        no_transcript = bool(c.get("no_speech"))
        asr_failed = c.get("reason") == "transcribe_failed"
        if asr_failed:
            verdicts.add("asr_failed")
        elif no_transcript:
            verdicts.add("no_speech")
        final_path = seg_dir / "final.json"
        src = c.get("result") or {}
        if final_path.exists():
            src = json.loads(final_path.read_text(encoding="utf-8"))["result"]
        # 没有任何材料（summarize 失败）、或转写失败又没有视频模式的结果时，无从判断有没有内容，不下"空教室"的结论
        undetermined = not (c_path.exists() or final_path.exists()) or (asr_failed and not final_path.exists())
        if not undetermined and not (src.get("chapters") or src.get("key_points") or src.get("knowledge_points")):
            verdicts.add("empty")
        if not final_path.exists() and not no_transcript:
            verdicts.add("c_only")
    return "+".join(sorted(verdicts)) or "ok"


def write_batch_report(batch: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"batch-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
