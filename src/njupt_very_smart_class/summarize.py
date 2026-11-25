"""转写模式第三步：转写 + 课件描述 → 分段笔记（map）→ 全课结构化笔记（reduce）→ c_result.json。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .asr import Chunk, Transcript, chunk_segments, load_transcript
from .models import TextModel, cache_key
from .vision import courseware_frames

MAP_PROMPT = """你是一名助教，正在为一节大学课程整理笔记。下面是这节课第 {index}/{total} 段（{start}–{end}）的老师讲课转写（语音识别结果，可能有同音错别字），以及这段时间教室屏幕上出现的课件信息。
只输出一个 JSON 对象：
{{"topics": ["本段讲的主题，按出现顺序"],
 "key_points": ["具体知识点：定义、命令、公式、结论、例子。术语、命令、参数、数字必须与材料一致"],
 "examples_or_demo": ["课堂演示或例题，含命令与步骤"],
 "questions": [{{"time": "MM:SS", "q": "学生的问题", "a": "老师的回答"}}],
 "homework_or_notices": ["作业、通知、考试提示、下次课安排"],
 "uncertain": ["转写明显有误或听不清、需要人工核对的地方，附时间 MM:SS"]}}
要求：忠于材料，不编造；口语和重复可以整理，但不要丢掉具体信息；某项没有内容就给空数组。

【转写】
{transcript}

【课件：这段时间屏幕上出现的页面】
{slides}

【全课术语表（命令、变量名、符号以此为准；转写里的同音误识别请按此纠正）】
{glossary}"""

REDUCE_PROMPT = """下面是一节课 {n} 段的分段笔记（JSON 数组）、全课的课件页面目录和课程信息。请把它们合成一份完整的课堂笔记，只输出一个 JSON 对象：
{{"overview": "三句话以内概括本节课",
 "chapters": [{{"start": "MM:SS", "end": "MM:SS", "title": "章节标题", "points": ["3 到 6 条要点，含具体命令/公式/结论"]}}],
 "knowledge_points": ["按重要性排序的核心知识点，保留术语、命令、公式"],
 "qa": [{{"time": "MM:SS", "q": "学生问题", "a": "老师回答"}}],
 "homework_or_notices": ["作业、通知、考试提示"],
 "terms": [{{"term": "术语", "explain": "一句话解释"}}],
 "uncertain": ["需要人工核对的点，附时间"],
 "courseware": ["本节课用到的课件页标题，按出现顺序去重"]}}
要求：章节按时间顺序且覆盖整节课；合并各段重复内容；保留具体术语、命令、数字；不编造材料里没有的内容。

【课程信息】{course}

【课件目录】
{outline}

【分段笔记】
{chunks}

【处理提示（事实性说明，供判断可信度，不要写进笔记正文）】
{notes}"""


def _fmt_ms(ms: int) -> str:
    s = int(ms) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


_BOOKKEEPING = ("usage", "cache_key")  # 分段笔记里的记账字段，不进 reduce 的 prompt


def glossary(frames: list[dict]) -> list[str]:
    """全课课件里出现过的术语、命令、代码，去重保序；给 map 阶段纠正 ASR 同音错误用。"""
    seen: dict[str, None] = {}
    for f in courseware_frames(frames):
        for x in [f.get("title"), *(f.get("keywords") or []), *(f.get("formulas_or_code") or [])]:
            if x and str(x).strip():
                seen.setdefault(str(x).strip(), None)
    return list(seen)


def slides_block(frames: list[dict], start_ms: int, end_ms: int) -> str:
    items = [f for f in courseware_frames(frames) if start_ms <= f["audio_time"] * 1000 <= end_ms]
    if not items:
        return "（这段时间没有检测到课件翻页；可能沿用上一页或在演示/板书）"
    lines = []
    for f in items:
        title = f.get("title") or "无标题"
        lines.append(f"- {_fmt_ms(int(f['audio_time'] * 1000))} 《{title}》{f.get('summary', '')}")
        if f.get("keywords"):
            lines.append(f"  关键词：{'、'.join(f['keywords'])}")
        if f.get("formulas_or_code"):
            lines.append("  代码/公式：" + " | ".join(str(x) for x in f["formulas_or_code"]))
    return "\n".join(lines)


def _load_frames(seg_dir: Path) -> list[dict]:
    """slides/frames.json（屏幕流或切换后的机位关键帧）+ slides_camera/frames.json（混合课的机位补帧）。"""
    items: list[dict] = []
    for sub in ("slides", "slides_camera"):
        path = seg_dir / sub / "frames.json"
        if path.exists():
            items.extend(sorted(json.loads(path.read_text(encoding="utf-8")), key=lambda f: f.get("audio_time", 0)))
    return items


def _load_items(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    return {int(d["index"]): d for d in json.loads(path.read_text(encoding="utf-8"))}


def map_chunks(
    text_model: TextModel,
    transcript: Transcript,
    frames: list[dict],
    *,
    concurrency: int = 3,
    max_chars: int = 3500,
    on_done=None,
    force: bool = False,
) -> list[dict]:
    out = transcript.path.with_name("chunks.json")
    done = {} if force else _load_items(out)
    chunks = chunk_segments(transcript.segments, max_chars=max_chars)
    model = text_model.name
    glossary_text = "、".join(glossary(frames)) or "（无）"
    total = len(chunks)

    def build(chunk: Chunk) -> str:
        return MAP_PROMPT.format(
            index=chunk.index,
            total=total,
            start=_fmt_ms(chunk.start_ms),
            end=_fmt_ms(chunk.end_ms),
            transcript=chunk.text,
            slides=slides_block(frames, chunk.start_ms, chunk.end_ms),
            glossary=glossary_text,
        )

    keys = {c.index: cache_key(model, build(c)) for c in chunks}
    todo = [c for c in chunks if _stale_chunk(done.get(c.index), keys[c.index])]
    if todo:

        def one(chunk: Chunk) -> dict:
            prompt = build(chunk)
            result = text_model.complete_json(prompt, thinking=True, max_tokens=16000)
            obj, usage = result.data, result.usage
            return {
                "index": chunk.index,
                "start_ms": chunk.start_ms,
                "end_ms": chunk.end_ms,
                **obj,
                "cache_key": keys[chunk.index],
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                },
            }

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(one, c): c for c in todo}
            for fut in as_completed(futures):
                item = fut.result()
                done[item["index"]] = item
                out.write_text(
                    json.dumps([done[k] for k in sorted(done)], ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                if on_done:
                    on_done(item)
    return [done[k] for k in sorted(done)]


def _stale_chunk(old: dict | None, key: str) -> bool:
    """分段笔记是否要重算。旧版 chunks.json 没有 cache_key，无法判断输入是否变过：只认序号。"""
    return old is None or ("cache_key" in old and old["cache_key"] != key)


def reduce_prompt(course: str, frames: list[dict], notes: list[dict], extra_notes: list[str] | None = None) -> str:
    outline = [
        f"- {_fmt_ms(int(f['audio_time'] * 1000))} 《{f.get('title') or '无标题'}》" for f in courseware_frames(frames)
    ]
    material = [{k: v for k, v in n.items() if k not in _BOOKKEEPING} for n in notes]
    return REDUCE_PROMPT.format(
        n=len(notes),
        course=course,
        outline="\n".join(outline) or "（未检测到课件）",
        chunks=json.dumps(material, ensure_ascii=False),
        notes="\n".join(f"- {n}" for n in (extra_notes or [])) or "（无）",
    )


def reduce_notes(text_model: TextModel, prompt: str) -> tuple[dict, dict]:
    result = text_model.complete_json(prompt, thinking=True, max_tokens=32000)
    obj = dict(result.data)
    if result.truncated:
        obj["_truncated"] = True
    return obj, result.usage


def write_no_transcript_result(
    seg_dir: Path, *, model: str, course_info: str, course_name: str, reason: str | None
) -> dict:
    """没有可用转写（无人声，或 transcribe 阶段失败）：转写模式没有材料，写一个空结果让融合/渲染继续，并标明原因。"""
    empty = {
        "model": model,
        "course": course_name,
        "course_info": course_info,
        "segments": 0,
        "transcript_chars": 0,
        "chunks": [],
        "result": {},
        "usage": {},
        "no_speech": True,
        "reason": reason,
    }
    (seg_dir / "c_result.json").write_text(json.dumps(empty, ensure_ascii=False, indent=1), encoding="utf-8")
    return empty


def summarize_segment(
    text_model: TextModel,
    seg_dir: Path,
    *,
    course_info: str,
    course_name: str,
    on_done=None,
    force: bool = False,
    processing_notes: list[str] | None = None,
) -> dict:
    model = text_model.name
    transcript = load_transcript(seg_dir / "transcript.json")
    if transcript is None:
        raise RuntimeError(f"{seg_dir} 没有 transcript.json，先执行 transcribe")
    if not transcript.segments:
        # 无人声：转写模式没有材料，写一个空结果让融合/渲染能继续，并标明原因
        return write_no_transcript_result(
            seg_dir, model=model, course_info=course_info, course_name=course_name, reason=transcript.reason
        )
    frames = _load_frames(seg_dir)  # 屏幕流 + 机位补帧
    notes = map_chunks(text_model, transcript, frames, on_done=on_done, force=force)
    prompt = reduce_prompt(course_info, frames, notes, extra_notes=processing_notes)
    key = cache_key(model, prompt)
    cached_path = seg_dir / "c_result.json"
    if not force and cached_path.exists():
        cached = json.loads(cached_path.read_text(encoding="utf-8"))
        if "cache_key" in cached:
            fresh = cached["cache_key"] == key
        else:  # 旧版 c_result.json 只记了模型：沿用旧规则（模型一致且分段序号一致）
            fresh = cached.get("model") == model and [c["index"] for c in cached.get("chunks", [])] == [
                c["index"] for c in notes
            ]
        if fresh:
            return cached  # 输入没变，reduce 不重跑
    result, reduce_usage = reduce_notes(text_model, prompt)
    map_tokens = sum((n.get("usage") or {}).get("prompt_tokens") or 0 for n in notes)
    c_result = {
        "model": model,
        "course": course_name,
        "course_info": course_info,
        "transcript_chars": transcript.chars,
        "segments": len(transcript.segments),
        "chunks": notes,
        "result": result,
        "notes": list(processing_notes or []),
        "usage": {"map_prompt_tokens": map_tokens, "reduce": reduce_usage},
        "cache_key": key,
    }
    cached_path.write_text(json.dumps(c_result, ensure_ascii=False, indent=1), encoding="utf-8")
    return c_result
