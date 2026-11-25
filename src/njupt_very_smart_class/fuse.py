"""融合两种模式的结果：以视频模式的细时间轴与屏幕证据为骨架，用转写模式的要点补全，冲突进待复核。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import TextModel, cache_key

PROMPT = """你是助教。下面是同一节课的两份机器笔记，一份来自「转写模式」，一份来自「视频模式」；你的输出里提到它们时也用这两个叫法，不要用字母或其他代号：
【转写模式】基于语音识别转写（同音错误多，例如 echo 被识别成 apple、PATH 被识别成「东哥」）和课件页描述生成，章节粗但忠实于老师原话；
【视频模式-VGA】由全模态模型看整段屏幕录像并听音频生成，时间轴细，带屏幕事件与命令，但会把课前准备、远程桌面断连等当成章节；
【视频模式-Video1】（如果有）同一模型看教室机位摄像机画面并听音频生成，能看到板书与老师动作，但看不清课件细节。
几份视频模式的结果互相印证：板书内容以【视频模式-Video1】为准，命令/课件以【视频模式-VGA】为准。
请融合成一份最终课堂笔记，只输出一个 JSON 对象：
{{"overview": "三句话以内",
 "chapters": [{{"start": "MM:SS", "end": "MM:SS", "title": "...", "points": ["要点，含具体命令/公式/结论"]}}],
 "knowledge_points": ["按重要性排序，保留术语、命令、公式"],
 "demos": [{{"time": "MM:SS", "code": "命令或代码", "purpose": "目的", "output": "屏幕上显示的输出"}}],
 "qa": [{{"time": "MM:SS", "q": "...", "a": "..."}}],
 "homework_or_notices": ["作业、通知、考试提示"],
 "terms": [{{"term": "...", "explain": "..."}}],
 "asr_corrections": [{{"time": "MM:SS", "asr": "转写原文", "corrected": "应为"}}],
 "non_teaching": [{{"start": "MM:SS", "end": "MM:SS", "what": "课前准备 / 课间 / 断连等"}}],
 "uncertain": ["两种模式冲突或都无法确认的点，附时间并说明原因"]}}
如果【转写模式】是空对象或缺少内容，说明这节课没有可用的语音转写或课件（例如老师没用麦克风、教室没有屏幕流），此时只根据视频模式整理，并在 uncertain 里注明。
原则：
1. 章节以视频模式的细时间轴为骨架，用转写模式的要点补全老师强调的内容；课前、课间、断连等非教学段落不进 chapters，放 non_teaching。
2. 命令、变量名、参数、输出以屏幕显示为准（视频模式）；老师原话以转写为准（转写模式），除非是明显同音错误。
3. 转写模式的 uncertain 里能被视频模式的音视频证据裁定的，写成 asr_corrections；两种模式说法不一致且无法裁定的，进 uncertain 并写明分歧。
4. 不编造材料里没有的内容。

【课程】{course}

【转写模式】
{c}

{a}"""


def _load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_a_results(seg_dir: Path) -> dict[str, dict]:
    """seg_dir/a_result_*.json → {"VGA": {...}, "Video1": {...}}；兼容旧的 a_result.json（视为 VGA）。"""
    out: dict[str, dict] = {}
    for path in sorted(seg_dir.glob("a_result_*.json")):
        out[path.stem.removeprefix("a_result_")] = json.loads(path.read_text(encoding="utf-8"))
    legacy = seg_dir / "a_result.json"
    if legacy.exists() and "VGA" not in out:
        out["VGA"] = json.loads(legacy.read_text(encoding="utf-8"))
    return out


def _c_view(c: dict) -> dict:
    """给融合模型看的 C：reduce 结果 + 各段的例子与作业通知（reduce 可能漏掉）。"""
    view = dict(c.get("result", {}))
    view["chunk_examples"] = [x for ch in c.get("chunks", []) for x in ch.get("examples_or_demo", [])]
    view["chunk_homework"] = [x for ch in c.get("chunks", []) for x in ch.get("homework_or_notices", [])]
    return view


def _a_view(a: dict) -> dict:
    """容忍模型把键名写错：homework_or_noties / homework_or_notice 等一律归位。"""
    r = dict(a.get("result", {}))
    for key in list(r.keys()):
        if key.startswith("homework_or_not"):
            r.setdefault("homework_or_notices", r.pop(key))
    return r


def fuse(
    text_model: TextModel,
    seg_dir: Path,
    *,
    course_info: str,
    force: bool = False,
    timeout: float = 900.0,
) -> dict | None:
    model = text_model.name
    c = _load(seg_dir / "c_result.json")
    a_all = load_a_results(seg_dir)
    if c is None:
        raise RuntimeError(f"{seg_dir} 没有 c_result.json，先执行 summarize")
    out = seg_dir / "final.json"
    if not a_all:
        if out.exists():
            out.unlink()
        return None  # 没有视频模式的结果就不融合，render 退回转写模式
    a_block = "\n\n".join(
        f"【视频模式-{label}】\n{json.dumps(_a_view(a), ensure_ascii=False)}" for label, a in a_all.items()
    )
    prompt = PROMPT.format(course=course_info, c=json.dumps(_c_view(c), ensure_ascii=False), a=a_block)
    key = cache_key(model, prompt)
    if out.exists() and not force:
        cached = json.loads(out.read_text(encoding="utf-8"))
        if "cache_key" in cached:
            fresh = cached["cache_key"] == key
        else:
            fresh = cached.get("model") == model  # 旧版 final.json 只记了模型
        if fresh:
            return cached
    result = text_model.complete_json(prompt, thinking=True, max_tokens=32000, timeout=timeout)
    obj = dict(result.data)
    if result.truncated:
        obj["_truncated"] = True  # 输出被截断，JSON 已按闭合修复，末尾内容可能缺失
    final = {
        "model": model,
        "course": c.get("course", ""),
        "course_info": course_info,
        "segments": c.get("segments"),
        "transcript_chars": c.get("transcript_chars"),
        "no_speech": c.get("no_speech"),
        "reason": c.get("reason"),  # 没有转写的原因跟着走到 final.json，渲染 WARNING 不依赖当次运行的 errors
        "result": obj,
        "usage": result.usage,
        "cache_key": key,
        "sources": {
            "c_model": c.get("model"),
            "a": {label: {"model": a.get("model"), "fps": a.get("fps")} for label, a in a_all.items()},
        },
    }
    out.write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
    return final
