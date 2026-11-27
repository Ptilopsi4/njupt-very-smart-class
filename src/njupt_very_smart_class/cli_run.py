"""run：单节 / 批量 / 本地续跑。选项按"选择 / 阶段 / 下载 / 处理 / 模型"分组。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import requests
import typer
from rich.table import Table

from . import client, download, media, paths, pipeline
from .auth import AuthError
from .bailian import BailianError
from .cli import (
    CTX,
    EXIT_AUTH,
    EXIT_ERROR,
    EXIT_NO_MATCH,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_UPSTREAM,
    JsonFlag,
    apply_json_flag,
    emit_json,
    fail,
    log,
    out,
    parse_size,
    warn,
)
from .cli_lessons import _iter_videos
from .config import ConfigError, effective_models


def run(
    new_id: Annotated[
        str | None, typer.Argument(help="录播 NewID（可前缀）。省略则按 --title/--start/--end/--semester/--limit 批量")
    ] = None,
    json_flag: JsonFlag = False,
    # ---- 选择 ----
    title: Annotated[str, typer.Option("--title", "-t", help="批量：标题关键字", rich_help_panel="选择")] = "",
    start: Annotated[
        str | None, typer.Option("--start", help="批量：起始日期 YYYY-MM-DD", rich_help_panel="选择")
    ] = None,
    end: Annotated[str | None, typer.Option("--end", help="批量：截止日期 YYYY-MM-DD", rich_help_panel="选择")] = None,
    semester: Annotated[
        str | None,
        typer.Option("--semester", help="批量：current 或 2026-2027-1；显式日期优先", rich_help_panel="选择"),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", help="批量：最多处理条数（时间倒序取前 N 条）", rich_help_panel="选择"),
    ] = None,
    order: Annotated[str, typer.Option("--order", help="批量：时间顺序 desc/asc", rich_help_panel="选择")] = "desc",
    local: Annotated[
        list[Path] | None,
        typer.Option(
            "--local",
            help="本地课时目录或课程目录（含 meta.json 的子目录算一节），可重复；不需要登录，不含 fetch",
            rich_help_panel="选择",
        ),
    ] = None,
    # ---- 阶段 ----
    stages: Annotated[str, typer.Option("--stages", help="要执行的阶段，逗号分隔", rich_help_panel="阶段")] = ",".join(
        pipeline.STAGES
    ),
    no_a_path: Annotated[
        bool,
        typer.Option(
            "--no-a-path", help="不看整段视频：跳过视频模式，只靠转写和课件（费用约减半）", rich_help_panel="阶段"
        ),
    ] = False,
    prune_video: Annotated[
        bool,
        typer.Option(
            "--prune-video",
            help="处理完后删除 mp4 原件（本次有阶段失败则不删），保留 wav / 关键帧 / *_640.mp4",
            rich_help_panel="阶段",
        ),
    ] = False,
    no_skip_done: Annotated[
        bool, typer.Option("--no-skip-done", help="批量时不跳过已完成的课时", rich_help_panel="阶段")
    ] = False,
    # ---- 下载 ----
    types: Annotated[
        str, typer.Option("--types", help="fetch 阶段候选流，逗号分隔", rich_help_panel="下载")
    ] = ",".join(download.DEFAULT_TYPES),
    full_download: Annotated[
        bool,
        typer.Option(
            "--full-download", help="下载 --types 里的全部流；默认 VGA 有音轨就不再下 Video1", rich_help_panel="下载"
        ),
    ] = False,
    # ---- 处理 ----
    concurrency: Annotated[
        int, typer.Option("--concurrency", help="幻灯片描述并发数", min=1, rich_help_panel="处理")
    ] = 4,
    jobs: Annotated[
        int,
        typer.Option(
            "--jobs",
            help="批量时同时处理几节课（线程，各课独立调用模型）；1 为顺序处理",
            min=1,
            rich_help_panel="处理",
        ),
    ] = 1,
    force_vision: Annotated[
        bool, typer.Option("--force-vision", help="忽略已有 frames.json，重新描述所有幻灯片", rich_help_panel="处理")
    ] = False,
    omni_input: Annotated[
        list[str] | None,
        typer.Option("--omni-input", help="视频模式看哪路录像，可重复：VGA / Video1", rich_help_panel="处理"),
    ] = None,
    omni_sample_seconds: Annotated[
        float,
        typer.Option("--omni-sample-seconds", help="视频模式每多少秒取一帧", min=0.001, rich_help_panel="处理"),
    ] = 5.0,
    force_omni: Annotated[
        bool, typer.Option("--force-omni", help="忽略已有 a_result_*.json", rich_help_panel="处理")
    ] = False,
    force_fuse: Annotated[
        bool, typer.Option("--force-fuse", help="忽略已有 final.json", rich_help_panel="处理")
    ] = False,
    min_free: Annotated[
        str, typer.Option("--min-free", help="磁盘剩余低于此值即停止下载，如 2GB", rich_help_panel="下载")
    ] = "2GB",
    # ---- 模型 ----
    model_asr: Annotated[
        str | None,
        typer.Option("--model-asr", help="ASR 模型；默认取 .env VSC_MODEL_ASR，否则代码默认", rich_help_panel="模型"),
    ] = None,
    model_vision: Annotated[
        str | None,
        typer.Option("--model-vision", help="幻灯片描述模型；默认取 .env VSC_MODEL_VISION", rich_help_panel="模型"),
    ] = None,
    model_text: Annotated[
        str | None,
        typer.Option("--model-text", help="摘要/融合模型；默认取 .env VSC_MODEL_TEXT", rich_help_panel="模型"),
    ] = None,
    model_omni: Annotated[
        str | None,
        typer.Option(
            "--model-omni", help="视频模式（整段视频）的模型；默认取 .env VSC_MODEL_OMNI", rich_help_panel="模型"
        ),
    ] = None,
) -> None:
    """一键：fetch → prepare → transcribe → vision → summarize → omni → fuse → render，各阶段幂等。

    单节：vsc run <NewID>            批量：vsc run --semester current --title 编译原理 --limit 5
    续跑：vsc run --local data/编译原理   不看整段视频：--no-a-path   机器可读：全局 --json

    退出码：0 全部完成；5 有阶段/课时失败（其余已完成，可重跑续上）；6 没匹配到；3 未登录；4 模型/接口错误。
    """
    apply_json_flag(json_flag)
    try:
        wanted = pipeline.parse_stages(stages)
        kinds = tuple(t.strip() for t in types.split(",") if t.strip())
        bad_types = [k for k in kinds if k not in download.STREAM_TYPES]
        if bad_types or not kinds:
            raise typer.BadParameter(f"--types 只能包含 {', '.join(download.STREAM_TYPES)}，非法：{bad_types}")
        omni_inputs = tuple(omni_input or ["VGA"])
        bad_inputs = [k for k in omni_inputs if k not in download.STREAM_TYPES]
        if bad_inputs:
            raise typer.BadParameter(f"--omni-input 只能包含 {', '.join(download.STREAM_TYPES)}，非法：{bad_inputs}")
        eff = effective_models()
        opts = pipeline.RunOptions(
            types=kinds,
            minimal=not full_download,
            concurrency=concurrency,
            force_vision=force_vision,
            omni_sample_seconds=omni_sample_seconds,
            omni_inputs=omni_inputs,
            force_omni=force_omni,
            force_fuse=force_fuse,
            prune_video=prune_video,
            min_free_bytes=parse_size(min_free),
            models={
                "asr": model_asr or eff["asr"],
                "vision": model_vision or eff["vision"],
                "text": model_text or eff["text"],
                "omni": None if no_a_path else (model_omni or eff["omni"]),
            },
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None

    if local and new_id is not None:
        raise typer.BadParameter("--local 与录播 NewID 不能同时给出")
    if local:  # 批量/本地分支自行处理错误与退出码（typer.Exit 是 RuntimeError 子类，不能放进下面的 try）
        return _run_local(local, wanted, opts, no_skip_done, jobs)
    if new_id is None and not (title or start or end or limit or semester):
        raise typer.BadParameter("批量模式需要至少一个筛选条件：--title/--start/--end/--semester/--limit")
    sc = _client_or_fail()
    if new_id is None:
        return _run_batch(sc, new_id, wanted, opts, no_skip_done, title, start, end, semester, limit, order, jobs)
    try:
        report = pipeline.run_lesson(sc, new_id, stages=wanted, opts=opts, log=log)
    except (
        ConfigError,
        BailianError,
        AuthError,
        ValueError,
        RuntimeError,
        media.MediaError,
        requests.RequestException,
    ) as exc:
        fail_pipeline_exc(exc)

    state = report["state"]
    errors = state.get("errors", {})
    code = EXIT_PARTIAL if errors else EXIT_OK
    payload = {
        "mode": "single",
        "lesson_dir": str(report["lesson_dir"]),
        "summary": str(report["lesson_dir"] / "summary.md"),
        "summary_exists": pipeline.find_summary(report["lesson_dir"]) is not None,
        "stages_done": sorted(state.get("stages", {})),
        "errors": errors,
        "notes": state.get("notes", []),
        "tokens": state.get("tokens", {}),
        "tokens_detail": state.get("tokens_detail", {}),
        "verdict": pipeline.classify_lesson(report["lesson_dir"]),
    }
    if CTX.json_mode:
        emit_json({"ok": code == EXIT_OK, "exit_code": code, **payload})
        raise typer.Exit(code)
    if errors:
        warn(f"完成但有阶段失败：{errors}（重跑并带上这些阶段可续上；没请求的阶段沿用上次的结论）")
    else:
        log(f"[green]完成[/green] {report['lesson_dir'].name}  判定 {payload['verdict']}")
    for n in payload["notes"]:
        warn(f"提示 {n}")
    if payload["tokens"]:
        log("token 用量：" + fmt_tokens(payload["tokens"], payload["tokens_detail"]))
    log(f"摘要：{payload['summary']}" + ("" if payload["summary_exists"] else "（未生成）"))
    raise typer.Exit(code)


def fmt_tokens(tokens: dict, detail: dict) -> str:
    """`模型 总数（入 x / 出 y）`；没有分项计数的模型只显示总数。"""
    parts = []
    for model, n in tokens.items():
        d = detail.get(model)
        parts.append(f"{model} {n}" + (f"（入 {d['input']} / 出 {d['output']}）" if d else ""))
    return "，".join(parts)


def fail_pipeline_exc(exc: Exception) -> None:
    """阶段层异常 → 退出码：会话 3；配置缺失 1；模型/网络 4（含重试耗尽后包成 RuntimeError 的下载失败）；找不到课时 6；其余 1。"""
    if isinstance(exc, AuthError):
        fail(str(exc), EXIT_AUTH)
    if isinstance(exc, ConfigError):
        fail(str(exc), EXIT_ERROR)
    if isinstance(exc, BailianError):
        fail(str(exc), EXIT_UPSTREAM)
    if isinstance(exc, requests.RequestException):
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)
    if "匹配" in str(exc):
        fail(str(exc), EXIT_NO_MATCH)
    if isinstance(exc.__cause__, requests.RequestException):
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)
    fail(str(exc), EXIT_ERROR)


def _client_or_fail():
    try:
        return client.make_client()
    except (AuthError, ValueError) as exc:
        fail(str(exc), EXIT_AUTH)
    except requests.RequestException as exc:
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)


def _run_local(
    local: list[Path], wanted: tuple[str, ...], opts: pipeline.RunOptions, no_skip_done: bool, jobs: int
) -> None:
    found = pipeline.discover_local(local)
    if not found:
        fail("--local 下没有找到课时目录（需含 meta.json）", EXIT_NO_MATCH)
    videos = [v for v, _ in found]
    lesson_dirs = {v.id: d for v, d in found}  # 就在给的目录上处理，不去数据目录里按 NewID 找
    wanted = tuple(s for s in wanted if s != "fetch")
    log(f"本地 {len(videos)} 节，阶段 {','.join(wanted)}" + (f"，{jobs} 节并行" if jobs > 1 else ""))
    try:
        report = pipeline.run_batch(
            None,
            videos,
            stages=wanted,
            opts=opts,
            log=log,
            skip_done=not no_skip_done,
            lesson_dirs=lesson_dirs,
            jobs=jobs,
        )
    except (ConfigError, BailianError) as exc:
        fail_pipeline_exc(exc)
    _finish_batch(report, "local")


def _run_batch(sc, new_id, wanted, opts, no_skip_done, title, start, end, semester, limit, order, jobs) -> None:
    if not (title or start or end or limit or semester):
        raise typer.BadParameter("批量模式需要至少一个筛选条件：--title/--start/--end/--semester/--limit")
    videos = _iter_videos(sc, title=title, start=start, end=end, semester=semester, limit=limit, order=order)
    if not videos:
        if CTX.json_mode:
            emit_json({"ok": False, "exit_code": EXIT_NO_MATCH, "mode": "batch", "count": 0, "results": []})
        else:
            warn("没有匹配的录播")
        raise typer.Exit(EXIT_NO_MATCH)
    log(f"批量 {len(videos)} 节，阶段 {','.join(wanted)}" + (f"，{jobs} 节并行" if jobs > 1 else ""))
    try:
        report = pipeline.run_batch(
            sc, videos, stages=wanted, opts=opts, log=log, skip_done=not no_skip_done, jobs=jobs
        )
    except (ConfigError, BailianError) as exc:
        fail_pipeline_exc(exc)
    _finish_batch(report, "batch")


def _finish_batch(report: dict, mode: str) -> None:
    results = report["results"]
    has_error = any(r["status"] in ("error", "partial") for r in results)
    report_path = pipeline.write_batch_report(report, paths.data_dir() / "_reports")
    code = EXIT_PARTIAL if has_error else EXIT_OK
    if report.get("aborted") == "auth":
        code = EXIT_AUTH
    if CTX.json_mode:
        emit_json(
            {
                "ok": code == EXIT_OK,
                "exit_code": code,
                "mode": mode,
                "count": len(results),
                "results": results,
                "tokens": report["tokens"],
                "tokens_detail": report.get("tokens_detail", {}),
                "jobs": report.get("jobs", 1),
                "report": str(report_path),
            }
        )
        raise typer.Exit(code)
    table = Table(header_style="bold")
    fixed = (("状态", 9), ("判定", 17), ("课程", 10), ("累计tok", 8))
    frame = 2 + 3 * (len(fixed) + 1)
    title_width = max(out.width - frame - sum(w for _, w in fixed), 12)
    for col, width in fixed:
        table.add_column(col, no_wrap=True, overflow="ellipsis", width=width)
    table.add_column("标题", no_wrap=True, overflow="ellipsis", width=title_width)
    counts: dict[str, int] = {}
    for r in results:
        status = r["status"]
        counts[status] = counts.get(status, 0) + 1
        tokens = (
            str(sum(v for v in r["tokens"].values() if isinstance(v, int)))
            if r.get("tokens")
            else (r.get("error", "")[:40] if status == "error" else "")
        )
        name = r.get("title", "") or (r.get("error", "")[:60] if status == "error" else "")
        table.add_row(status, r.get("verdict", ""), r.get("course", ""), tokens, name)
    out.print(table)
    total = sum(report["tokens"].values())
    log(
        "，".join(f"{k} {v}" for k, v in counts.items())
        + f"；本次模型调用 {total} tokens（"
        + fmt_tokens(report["tokens"], report.get("tokens_detail", {}))
        + "）"
    )
    log(f"报告：{report_path}")
    raise typer.Exit(code)
