"""把 c_result.json（只有转写模式）或 final.json（转写模式 + 视频模式融合）渲染成 summary.md。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


def _fmt_ms(ms: int) -> str:
    s = int(ms) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def _ts(value) -> str:
    """时间统一成 MM:SS（满一小时用 H:MM:SS）。接受 "MM:SS" / "M:SS" / "H:MM:SS" / 毫秒数；空值给 00:00。"""
    if isinstance(value, bool):
        return "00:00"
    if isinstance(value, int | float):
        return _fmt_ms(value)
    if isinstance(value, str):
        parts = value.strip().split(":")
        if all(p.isdigit() for p in parts) and 2 <= len(parts) <= 3:
            nums = [int(p) for p in parts]
            total = nums[0] * 60 + nums[1] if len(nums) == 2 else nums[0] * 3600 + nums[1] * 60 + nums[2]
            if total >= 3600:
                return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
            return f"{total // 60:02d}:{total % 60:02d}"
        return value.strip() or "00:00"
    return "00:00"


def _bullets(items) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    elif isinstance(items, dict):
        items = list(items)  # dict.fromkeys 去重后的结果：取键
    if not isinstance(items, Iterable):
        items = [items]
    return [f"- {x}" for x in items if x is not None and str(x).strip()]


def _str_list(value) -> list[str]:
    """字符串数组；单条字符串视为一个元素；None/其他类型视为空。"""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return []


def _as_list(value) -> list:
    """模型偶尔把数组写成字符串或字典；统一成列表，渲染不因类型异常而中断。"""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _pick_key_slides(frames: list[dict], limit: int = 10) -> list[dict]:
    cw = [f for f in frames if f.get("is_courseware") or f.get("kind") == "slide"]
    if len(cw) <= limit:
        return cw
    step = len(cw) / limit
    return [cw[int(i * step)] for i in range(limit)]


def _source_line(summary: dict, a_result: dict | None) -> str:
    src = summary.get("sources")
    if src:
        parts = (
            src["a"]
            if isinstance(src.get("a"), dict)
            else {"VGA": {"model": src.get("a_model"), "fps": src.get("a_fps")}}  # 旧结构
        )
        a_desc = "，".join(f"视频-{k} {v.get('model')} @ {v.get('fps')} fps" for k, v in parts.items())
        return f"转写模式 + 视频模式融合：转写 {src.get('c_model')}，{a_desc}"
    if a_result is not None:
        return f"仅转写模式（{summary.get('model')}）；视频模式的结果存在但未融合"
    return f"仅转写模式（{summary.get('model')}）；视频模式未运行"


def render_summary(
    summary: dict,
    meta: dict,
    frames: list[dict],
    out: Path,
    *,
    a_result: dict | None = None,
    slides_rel: str = "slides",
    chunks: list[dict] | None = None,
    notes: list[str] | None = None,
    errors: dict[str, str] | None = None,
) -> Path:
    """summary：c_result 或 final；chunks 仅在没有融合结果时用于回退收集待复核条目。"""
    r = summary["result"]
    start = meta.get("StartTime", "")[:16].replace("T", " ")
    stop = meta.get("StopTime", "")[11:16]
    fused = "sources" in summary
    lines: list[str] = []
    # 平台 Title 形如「课程 教师 教室 起止时间」，直接作为副标题，不再重复拼课程名
    lines.append(f"# {summary.get('course', '')} {start[:10]} {start[11:16]}–{stop}".strip())
    lines.append("")
    lines.append(f"> {meta.get('Title', '')}  ")
    lines.append(
        f"> NewID {meta.get('NewID', '')} · 转写 {summary.get('segments', 0)} 句 / "
        f"{summary.get('transcript_chars', 0)} 字 · 生成 {datetime.now():%Y-%m-%d %H:%M} · "
        f"{_source_line(summary, a_result if not fused else None)}"
    )
    lines.append("")
    if not summary.get("segments"):
        if (errors or {}).get("transcribe"):
            warning = (
                "> [!WARNING] 本节课的语音转写阶段失败（见「处理提示」），摘要仅基于画面；修复后重跑同一命令可补上。"
            )
        else:
            warning = "> [!WARNING] 本节课没有可用的语音转写（老师未用麦或音轨静音），摘要仅基于画面。"
        lines.append(warning)
        lines.append("")
    lines.append("## 一句话概览")
    lines.append(r.get("overview", "") or "（无内容）")
    lines.append("")
    non_teaching = _as_list(r.get("non_teaching"))
    if non_teaching:
        lines.append(
            "非教学时段："
            + "；".join(f"{_ts(x.get('start'))}–{_ts(x.get('end'))} {x.get('what', '')}" for x in non_teaching)
        )
        lines.append("")
    lines.append("## 章节时间轴")
    for ch in _as_list(r.get("chapters")):
        lines.append(f"### {_ts(ch.get('start'))}–{_ts(ch.get('end'))} {ch.get('title', '')}")
        lines.extend(_bullets(ch.get("points")))
        lines.append("")
    lines.append("## 核心知识点")
    lines.extend(_bullets(r.get("knowledge_points")))
    lines.append("")
    demos = _as_list(r.get("demos"))
    if demos:
        lines.append("## 课堂演示")
        for d in demos:
            lines.append(f"**{_ts(d.get('time'))}** {d.get('purpose', '')}")
            lines.append("")
            lines.append("```")  # 屏幕上的内容不一定是 shell，不做语言标注
            lines.append(str(d.get("code", "")).strip())
            lines.append("```")
            if d.get("output"):
                lines.append("")
                lines.append(f"输出：`{str(d['output']).strip()}`")
            lines.append("")
    key = _pick_key_slides(frames)
    if key:
        lines.append("## 关键幻灯片")
        for f in key:
            t = _fmt_ms(int(f["audio_time"] * 1000))
            cap = f.get("title") or f.get("summary") or f.get("file", "")
            lines.append(f"**{t} {cap}**")
            lines.append("")
            lines.append(f"![{cap}]({slides_rel}/{f['file']})")
            lines.append("")
    lines.append("## 问答与互动")
    qa = _as_list(r.get("qa"))
    if qa:
        for q in qa:
            t = q.get("time")
            prefix = f"**{t}** " if t else ""
            lines.append(f"{prefix}问：{q.get('q', '')}  ")
            pad = " " * (len(str(t)) + 4) if t else ""
            lines.append(f"{pad}答：{q.get('a', '')}")
    else:
        lines.append("（未识别到问答）")
    lines.append("")
    lines.append("## 作业 / 通知 / 考试提示")
    lines.extend(_bullets(r.get("homework_or_notices")) or ["（无）"])
    lines.append("")
    terms = _as_list(r.get("terms"))
    lines.append("## 术语表")
    if terms:
        lines.append("| 术语 | 解释 |")
        lines.append("|---|---|")
        for t in terms:
            lines.append(f"| {t.get('term', '')} | {t.get('explain', '')} |")
    else:
        lines.append("（无）")
    lines.append("")
    corrections = _as_list(r.get("asr_corrections"))
    if corrections:
        lines.append("## ASR 勘误（由视频模式的音视频证据裁定）")
        lines.append("| 时间 | 转写 | 应为 |")
        lines.append("|---|---|---|")
        for c in corrections:
            lines.append(f"| {_ts(c.get('time'))} | {c.get('asr', '')} | {c.get('corrected', '')} |")
        lines.append("")
    unc = _str_list(r.get("uncertain"))
    if not unc and not fused and chunks:
        for ch in chunks:
            unc.extend(_str_list(ch.get("uncertain")))
    lines.append("## 待人工复核")
    lines.extend(_bullets(dict.fromkeys(unc)) or ["（无）"])
    lines.append("")
    failed = errors or {}
    # 阶段失败同时记在 notes（给 reduce 当上下文，截断到 160 字）和 errors（全文）里；渲染只留全文那条
    tips = [n for n in (notes or []) if not any(n.startswith(f"阶段 {k} 失败：") for k in failed)]
    tips += [f"阶段 {k} 失败：{v}" for k, v in failed.items()]
    if tips:
        lines.append("## 处理提示")
        lines.append("摘要的哪些部分可能不完整，由流水线自动记录：")
        lines.extend(_bullets(dict.fromkeys(tips)))
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
