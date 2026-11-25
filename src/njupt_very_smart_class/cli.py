"""vsc 命令行入口：全局选项、输出契约、退出码。子命令在 cli_auth / cli_lessons / cli_run。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Annotated

import typer
from rich.console import Console

# ---------- 退出码约定（agent 依赖） ----------
EXIT_OK = 0
EXIT_ERROR = 1  # 其他运行时错误
EXIT_USAGE = 2  # 参数/用法错误（typer 默认）
EXIT_AUTH = 3  # 未登录 / 会话过期 / SSO 失败
EXIT_UPSTREAM = 4  # 模型、智慧课堂接口或网络错误
EXIT_PARTIAL = 5  # 有阶段失败 / 批量里有失败，其余已落盘
EXIT_NO_MATCH = 6  # 筛选没匹配到任何录播 / 课时不存在

if sys.stdout and not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # 被管道/重定向时统一 UTF-8；真实终端沿用控制台编码
if sys.stderr and not sys.stderr.isatty() and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class CliContext:
    json_mode: bool = False  # stdout 只输出一个 JSON 结果，日志走 stderr
    quiet: bool = False  # 不显示进度条，日志只留警告/错误
    verbose: bool = False

    @property
    def progress(self) -> bool:
        return not self.json_mode and not self.quiet and sys.stdout.isatty()


CTX = CliContext()
out = Console()  # 结果输出（stdout）
err = Console(stderr=True)  # 日志输出（json 模式下全部走这里）


def emit_json(payload: dict) -> None:
    """机器可读输出：直接写 stdout，不经 rich（避免按终端宽度折行/截断），保证可被 json.loads 解析。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    """人类可读的进度日志：json 模式下一律进 stderr，保证 stdout 可解析。"""
    if CTX.json_mode:
        err.print(message, markup=False, highlight=False)
    else:
        out.print(message, markup=False, highlight=False)


def warn(message: str) -> None:
    err.print(f"[yellow]{message}[/yellow]")


def fail(message: str, code: int = EXIT_ERROR, **payload) -> None:
    """统一失败出口：json 模式输出 {"ok": false, ...}，否则红字。"""
    if CTX.json_mode:
        emit_json({"ok": False, "error": message, "exit_code": code, **payload})
    else:
        err.print(f"[red]{message}[/red]")
    raise typer.Exit(code)


def succeed(payload: dict) -> None:
    """统一成功出口（仅 json 模式需要显式调用）。"""
    emit_json({"ok": True, **payload})


JsonFlag = Annotated[
    bool, typer.Option("--json", "-j", help="stdout 只输出 JSON 结果（与全局 --json 等价，放在命令后也可）")
]


def apply_json_flag(value: bool) -> None:
    """允许 vsc run --json ... 这种把 --json 放在命令后的写法。"""
    if value:
        CTX.json_mode = True


def parse_size(s: str) -> int:
    from . import download

    try:
        return download.parse_size(s)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def parse_date(value: str | None, name: str) -> str:
    from datetime import datetime

    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise typer.BadParameter(f"{name} 需为 YYYY-MM-DD，得到 {value!r}") from None


app = typer.Typer(
    help="NJUPT 智慧课堂录播 → 视频理解 → 课堂摘要。\n\n"
    "动线：doctor → login --status → list --json → run --json → status → summary --print。\n"
    "加 --json 后 stdout 只输出一个 JSON 结果，日志转到 stderr；所有阶段幂等，可随时重跑续上。",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


@app.callback(invoke_without_command=True)
def _root(
    json_mode: Annotated[bool, typer.Option("--json", "-j", help="stdout 只输出 JSON 结果，日志转到 stderr")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="不显示进度条，日志只留警告/错误")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="更详细的日志")] = False,
    version: Annotated[bool, typer.Option("--version", help="打印版本并退出")] = False,
) -> None:
    from . import config

    config.load_dotenv()  # 所有命令统一在入口读 .env，NJUPT_* / VSC_DATA_DIR 与密钥、模型名同样生效
    CTX.json_mode, CTX.quiet, CTX.verbose = json_mode, quiet, verbose
    if version:
        from importlib.metadata import version as pkg_version

        try:
            v = pkg_version("njupt-very-smart-class")
        except Exception:  # noqa: BLE001
            v = "dev"
        out.print(f"vsc {v}")
        raise typer.Exit(EXIT_OK)


def _register() -> None:
    from .cli_auth import doctor, login, semesters
    from .cli_lessons import fetch, list_videos, prepare, status, summary
    from .cli_run import run

    names = {"list_videos": "list"}
    for cmd in (doctor, login, semesters, list_videos, fetch, prepare, run, status, summary):
        app.command(names.get(cmd.__name__, cmd.__name__.replace("_", "-")))(cmd)


_register()
