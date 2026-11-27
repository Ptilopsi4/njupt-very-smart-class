"""课时级命令：list / fetch / prepare / status / summary。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import requests
import typer
from rich.table import Table

from . import client, download, lesson, media, pipeline, slides
from .auth import AuthError
from .cli import (
    CTX,
    EXIT_AUTH,
    EXIT_ERROR,
    EXIT_NO_MATCH,
    EXIT_OK,
    EXIT_UPSTREAM,
    JsonFlag,
    apply_json_flag,
    emit_json,
    fail,
    fmt_tokens,
    log,
    out,
    parse_date,
    parse_size,
    succeed,
    warn,
)
from .vendor.njupt_smartclass import NjuptSmartclass, NjuptSmartclassVideoSummary


def _client_or_fail() -> NjuptSmartclass:
    try:
        return client.make_client()
    except (AuthError, ValueError) as exc:
        fail(str(exc), EXIT_AUTH)
    except requests.RequestException as exc:
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)


def _semester_bounds(sc: NjuptSmartclass, semester: str | None) -> tuple[str, str]:
    """--semester current | '2026-2027-1' | '2026-2027 学年第 1 学期' → (start, end)。"""
    if not semester:
        return "", ""
    try:
        if semester.lower() in ("current", "now", "本学期"):
            s = client.current_semester(sc)
            return s.start, s.end
        cleaned = semester.replace("-", " ").replace("学年", " ").replace("第", " ").replace("学期", " ")
        parts = [p for p in cleaned.split() if p]
        if len(parts) < 2:
            raise typer.BadParameter(f"无法识别的学期：{semester}")
        year, term = " ".join(parts[:2]), (parts[2] if len(parts) > 2 else None)
        for s in client.semester_calendar(sc):
            if s.year.replace("-", " ") == year and (term is None or s.term == term):
                return s.start, s.end
        raise typer.BadParameter(f"无法识别的学期：{semester}；可用 current 或 2026-2027-1 这类格式")
    except (AuthError, ValueError) as exc:
        fail(str(exc), EXIT_AUTH)
    except requests.RequestException as exc:
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)


def summary_dict(v: NjuptSmartclassVideoSummary) -> dict:
    return {
        "new_id": v.id,
        "title": v.title,
        "start": v.start_time.isoformat(),
        "stop": v.stop_time.isoformat(),
        "course": v.course_name,
        "teachers": v.teachers,
        "classroom": v.classroom_name,
        "minutes": round((v.stop_time - v.start_time).total_seconds() / 60),
    }


def _iter_videos(
    sc: NjuptSmartclass,
    *,
    title: str,
    start: str | None,
    end: str | None,
    semester: str | None,
    limit: int | None,
    order: str,
) -> list[NjuptSmartclassVideoSummary]:
    if order not in ("asc", "desc"):
        raise typer.BadParameter("--order 只能是 asc 或 desc")
    s_start, s_end = _semester_bounds(sc, semester)
    try:
        return list(
            client.iter_videos(
                sc,
                title_key=title,
                start_date=parse_date(start, "--start") or s_start,
                end_date=parse_date(end, "--end") or s_end,
                max_results=limit,
                descending=(order == "desc"),
            )
        )
    except (AuthError, ValueError) as exc:
        fail(str(exc), EXIT_AUTH)
    except requests.RequestException as exc:
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)


def list_videos(
    title: Annotated[str, typer.Option("--title", "-t", help="标题关键字（课程名/教师/教室都在标题里）")] = "",
    start: Annotated[str | None, typer.Option("--start", help="起始日期 YYYY-MM-DD（含）")] = None,
    end: Annotated[str | None, typer.Option("--end", help="截止日期 YYYY-MM-DD（含）")] = None,
    semester: Annotated[
        str | None, typer.Option("--semester", help="按校历筛选：current 或 2026-2027-1；显式日期优先")
    ] = None,
    order: Annotated[str, typer.Option("--order", help="时间顺序 desc（新→旧，默认）/ asc")] = "desc",
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="最多返回条数")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="输出 JSON（与全局 --json 同一信封）")] = False,
) -> None:
    """列出当前账号可见的录播。agent 用 --json；配合 --semester current --title 圈定范围。

    退出码 6：筛选没匹配到任何录播。
    """
    sc = _client_or_fail()
    videos = _iter_videos(sc, title=title, start=start, end=end, semester=semester, limit=limit, order=order)
    items = [summary_dict(v) for v in videos]
    if CTX.json_mode or json_out:
        emit_json(
            {
                "ok": bool(items),
                "exit_code": EXIT_OK if items else EXIT_NO_MATCH,
                "count": len(items),
                "items": items,
            }
        )
        raise typer.Exit(EXIT_OK if items else EXIT_NO_MATCH)
    if not items:
        warn("没有匹配的录播")
        raise typer.Exit(EXIT_NO_MATCH)
    width = out.width
    fixed = {"NewID": 8, "开始": 11, "时长": 5, "教室": 9}
    flex = {"课程": 1, "标题": 3, "教师": 1}
    order_cols = ("NewID", "开始", "时长", "课程", "标题", "教师", "教室")
    frame = 2 + 3 * len(order_cols)
    remaining = max(width - frame - sum(fixed.values()), 3 * len(flex))
    table = Table(header_style="bold")
    for col in order_cols:
        w = fixed[col] if col in fixed else max(remaining * flex[col] // sum(flex.values()), 3)
        table.add_column(col, width=w, no_wrap=True, overflow="ellipsis")
    for v in videos:
        table.add_row(
            v.id[:8],
            v.start_time.strftime("%m-%d %H:%M"),
            f"{summary_dict(v)['minutes']}m",
            v.course_name,
            v.title,
            v.teachers,
            v.classroom_name,
        )
    out.print(table)
    out.print(f"共 {len(videos)} 条" + (f"（--limit {limit}）" if limit else ""))


def _progress_factory():
    from contextlib import contextmanager

    from rich.progress import BarColumn, DownloadColumn, Progress, TimeRemainingColumn, TransferSpeedColumn

    @contextmanager
    def factory():
        progress = Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=out,
        )
        progress.start()
        try:
            yield lambda label: (
                lambda task_id: lambda done, total: progress.update(task_id, completed=done, total=total or None)
            )(progress.add_task(label, total=None))
        finally:
            progress.stop()

    return factory


def _null_progress():
    from contextlib import contextmanager

    @contextmanager
    def factory():
        yield lambda label: lambda done, total: None

    return factory


def fetch(
    new_id: Annotated[str, typer.Argument(help="录播 NewID（可前缀）")],
    types: Annotated[str, typer.Option("--types", help="候选流，逗号分隔；默认 VGA 有音轨就不再下 Video1")] = ",".join(
        download.DEFAULT_TYPES
    ),
    full_download: Annotated[bool, typer.Option("--full-download", help="下载 --types 里全部存在的流")] = False,
    min_free: Annotated[str, typer.Option("--min-free", help="磁盘剩余低于此值即停止，如 2GB")] = "2GB",
    json_flag: JsonFlag = False,
) -> None:
    """下载 index.xml 与各路 mp4，写 meta.json / streams.json / probe.json。幂等：已有文件跳过。

    老录像的媒体可能已被平台清理（Index.xml 404），会明确提示。
    """
    apply_json_flag(json_flag)
    kinds = tuple(t.strip() for t in types.split(",") if t.strip())
    bad = [k for k in kinds if k not in download.STREAM_TYPES]
    if bad or not kinds:
        raise typer.BadParameter(f"--types 只能包含 {', '.join(download.STREAM_TYPES)}，非法：{bad}")
    min_free_bytes = parse_size(min_free)
    sc = _client_or_fail()
    ctx_mgr = _progress_factory() if CTX.progress else _null_progress()
    try:
        with ctx_mgr() as factory:
            info, lesson_dir, segments = lesson.fetch_lesson(
                sc, new_id, types=kinds, min_free_bytes=min_free_bytes, progress=factory, minimal=not full_download
            )
    except (AuthError, ValueError, RuntimeError, media.MediaError, requests.RequestException) as exc:
        from .cli_run import fail_pipeline_exc

        fail_pipeline_exc(exc)
    available = sorted({s.kind for seg in segments for s in seg.sources})
    on_disk = sorted({k.removesuffix(".mp4") for seg in segments for k in seg.kept})
    missing = sorted(set(kinds) - set(available))
    skipped = sorted(set(available) - set(on_disk))  # minimal 主动没下，不等于教室没有
    streams = []
    for seg in segments:
        for src in seg.sources:
            path = seg.index_path.parent / f"{src.kind}.mp4"
            if path.exists():
                streams.append(
                    {"kind": src.kind, "file": str(path), "bytes": path.stat().st_size, "start_time": src.start_time}
                )
    if CTX.json_mode:
        succeed(
            {
                "new_id": info.id,
                "title": info.title,
                "lesson_dir": str(lesson_dir),
                "streams": streams,
                "missing": missing,
                "skipped": skipped,
            }
        )
        return
    log(f"[green]{info.course_name}[/green] {info.title} → {lesson_dir}")
    if missing:
        warn(f"该录像缺少流 {missing}（教室没有该设备）")
    if skipped:
        log(f"按默认策略未下载 {skipped}（屏幕流已含声音）；需要时加 --full-download")
    # 多段录制时 probe.json 在各分段目录下
    probes: dict[str, dict] = {}
    for seg in segments:
        probe_path = seg.index_path.parent / "probe.json"
        if probe_path.exists():
            probes.update(json.loads(probe_path.read_text(encoding="utf-8")))
    for s in streams:
        name = Path(s["file"]).name
        desc = lesson.describe_streams(probes.get(name, {}))
        log(f"  {name}: {download.fmt_bytes(s['bytes'])}, {desc}")


def prepare(
    new_id: Annotated[str, typer.Argument(help="录播 NewID（可前缀），需先 fetch")],
    threshold: Annotated[
        float, typer.Option("--threshold", help="scene 检测阈值；白底文字 PPT 翻页约 0.1–0.25")
    ] = slides.DEFAULT_THRESHOLD,
    max_slides: Annotated[
        int, typer.Option("--max-slides", help="幻灯片上限，超出等距抽样")
    ] = slides.DEFAULT_MAX_SLIDES,
    max_gap: Annotated[float, typer.Option("--max-gap", help="相邻幻灯片超过此秒数则补一帧")] = slides.DEFAULT_MAX_GAP,
    json_flag: JsonFlag = False,
) -> None:
    """抽 16 kHz wav；从 VGA.mp4 切幻灯片到 slides/。纯本地，不调模型，幂等。"""
    apply_json_flag(json_flag)
    lesson_dir = _lesson_or_fail(new_id)
    try:
        results = lesson.prepare_lesson(lesson_dir, threshold=threshold, max_slides=max_slides, max_gap=max_gap)
    except (ValueError, RuntimeError, media.MediaError) as exc:
        fail(str(exc), EXIT_ERROR)
    payload = {
        "lesson_dir": str(lesson_dir),
        "segments": [
            {
                "dir": str(r.dir),
                "audio": str(r.audio),
                "audio_source": r.audio_source,
                "audio_seconds": round(r.audio_seconds, 1),
                "slides": len(r.slides),
                "notes": r.notes,
            }
            for r in results
        ],
    }
    if CTX.json_mode:
        succeed(payload)
        return
    for r in results:
        log(f"  {r.dir.name}: audio {r.audio_seconds / 60:.1f} min ← {r.audio_source}; slides {len(r.slides)}")
        for n in r.notes:
            warn(f"  {r.dir.name}: {n}")


def _lesson_or_fail(new_id: str) -> Path:
    try:
        return lesson.find_lesson_dir(new_id)
    except ValueError as exc:
        fail(str(exc), EXIT_NO_MATCH)


def status(
    new_id: Annotated[str, typer.Argument(help="录播 NewID（可前缀）")],
    json_flag: JsonFlag = False,
) -> None:
    """查看一节课的处理状态：已完成阶段、错误、处理提示、产物清单、token、判定。不联网。"""
    apply_json_flag(json_flag)
    from . import pipeline, vision

    lesson_dir = _lesson_or_fail(new_id)
    state = pipeline.load_state(lesson_dir)
    meta = json.loads((lesson_dir / "meta.json").read_text(encoding="utf-8"))
    products: dict = {}
    for seg in lesson.segment_dirs(lesson_dir):
        t = seg / "transcript.json"
        tr = json.loads(t.read_text(encoding="utf-8")) if t.exists() else {}
        frames = vision.load_frames(seg / "slides" / "frames.json")
        products[seg.name] = {
            "audio": (seg / "audio.wav").exists(),
            "slides": len(_slides(seg)),
            "frames_described": len(frames),
            "transcript_segments": len(tr.get("segments", [])),
            "no_speech": bool(tr.get("no_speech")),
            "low_speech": bool(tr.get("low_speech")),
            "chunks": _count(seg / "chunks.json"),
            "c_result": (seg / "c_result.json").exists(),
            "a_results": sorted(p.stem.removeprefix("a_result_") for p in seg.glob("a_result_*.json")),
            "final": (seg / "final.json").exists(),
            "videos": sorted(p.name for p in seg.glob("*.mp4") if "_640" not in p.name),
        }
    payload = {
        "ok": True,
        "new_id": meta.get("NewID"),
        "title": meta.get("Title"),
        "course": meta.get("CourseName"),
        "lesson_dir": str(lesson_dir),
        "stages": sorted(state.get("stages", {})),
        "errors": state.get("errors", {}),
        "notes": state.get("notes", []),
        "tokens": state.get("tokens", {}),
        "tokens_detail": state.get("tokens_detail", {}),
        "verdict": pipeline.classify_lesson(lesson_dir),
        "summary": str(pipeline.find_summary(lesson_dir) or lesson_dir / "summary.md"),
        "summary_exists": pipeline.find_summary(lesson_dir) is not None,
        "products": products,
    }
    if CTX.json_mode:
        succeed(payload)
        return
    out.print(f"[bold]{meta.get('CourseName', '')} {meta.get('Title', '')}[/bold]")
    out.print(f"目录 {lesson_dir}")
    out.print(f"阶段完成 {', '.join(payload['stages']) or '（无）'}   判定 {payload['verdict']}")
    for k, v in payload["errors"].items():
        out.print(f"[red]错误 {k}[/red] {v}")
    for n in payload["notes"]:
        out.print(f"[yellow]提示[/yellow] {n}")
    for seg, p in products.items():
        out.print(f"  {seg}: " + ", ".join(f"{k}={v}" for k, v in p.items()))
    if payload["tokens"]:
        out.print("token " + fmt_tokens(payload["tokens"], payload["tokens_detail"]))
    out.print(f"摘要 {payload['summary']}" + ("" if payload["summary_exists"] else "（未生成）"))


def _slides(seg: Path) -> list:
    p = seg / "slides" / "slides.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _count(path: Path) -> int:
    return len(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else 0


def summary(
    new_id: Annotated[str, typer.Argument(help="录播 NewID（可前缀）")],
    print_markdown: Annotated[bool, typer.Option("--print", help="把 summary.md 内容打到 stdout")] = False,
    json_flag: JsonFlag = False,
) -> None:
    """定位（或打印）一节课的 summary.md。未生成时退出码 6 并提示下一步。--print 原样输出，不折行。"""
    apply_json_flag(json_flag)
    lesson_dir = _lesson_or_fail(new_id)
    path = pipeline.find_summary(lesson_dir)
    if path is None:
        fail(
            f"还没有 summary.md：{lesson_dir}；先 vsc run {new_id}",
            EXIT_NO_MATCH,
            summary=str(lesson_dir / "summary.md"),
        )
    final_path = lesson_dir / "final.json"
    payload = {
        "summary": str(path),
        "fused": final_path.exists(),
        "bytes": path.stat().st_size,
    }
    if print_markdown:
        payload["markdown"] = path.read_text(encoding="utf-8")
    if CTX.json_mode:
        succeed(payload)
    elif print_markdown:
        sys.stdout.write(payload["markdown"] if payload["markdown"].endswith("\n") else payload["markdown"] + "\n")
    else:
        out.print(str(path))
