"""认证与预检：doctor / login / semesters。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
from typing import Annotated

import requests
import typer

from . import auth, client
from .auth import AuthError
from .cli import (
    CTX,
    EXIT_AUTH,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_UPSTREAM,
    JsonFlag,
    apply_json_flag,
    emit_json,
    fail,
    log,
    out,
    succeed,
)
from .config import ConfigError, effective_models
from .vendor.njupt_sso import NjuptSsoException

# 凭证是 HttpOnly，只能从请求头复制；页面看着已登录时凭证也可能已失效，所以步骤里带 view-source 自查。
COOKIE_COPY_STEPS = (
    "重新获取：1) 浏览器登录后打开 https://njupt.smartclass.cn/UserSpace/CourseList.aspx；"
    "2) 自查：地址栏打开 view-source:https://njupt.smartclass.cn/UserSpace/CourseList.aspx，能搜到你的学号才是有效凭证，"
    "否则先重新登录；"
    "3) F12 → Network → 刷新 → 点第一条 CourseList.aspx → Request Headers → cookie 行右键 Copy value；"
    '4) vsc login --cookie "<粘贴>"，再 vsc login --status 确认'
)


def doctor(json_flag: JsonFlag = False) -> None:
    """预检：ffmpeg、模型密钥、登录会话、磁盘、模型配置。任何一项不满足都会给出修复命令。

    退出码：0 全部通过；3 会话无效（最常见，先 vsc login）；1 其他不通过。
    """
    from .paths import data_dir

    apply_json_flag(json_flag)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str, fix: str | None = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    ffmpeg_ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    check(
        "ffmpeg",
        ffmpeg_ok,
        "ffmpeg + ffprobe 在 PATH 中" if ffmpeg_ok else "缺少 ffmpeg/ffprobe",
        None if ffmpeg_ok else "安装 ffmpeg 并加入 PATH",
    )
    try:
        key = auth_dashscope_key_present()
        check(
            "api_key",
            key,
            "DASHSCOPE_API_KEY 已配置" if key else "缺少 DASHSCOPE_API_KEY",
            None if key else "在项目根 .env 写入 DASHSCOPE_API_KEY=...，或设置环境变量",
        )
    except Exception as exc:  # noqa: BLE001
        check("api_key", False, f"读取密钥出错：{exc}")
    session_state = "unknown"
    session_detail = ""
    try:
        sc = client.make_client()
        total = client.verify(sc)
        session_state = "ok"
        session_detail = f"会话有效，可见录播 {total} 条"
    except (AuthError, ValueError) as exc:
        session_state = "expired"
        session_detail = f"会话无效：{exc}"
    except requests.RequestException as exc:
        session_state = "network"
        session_detail = f"网络错误：{exc}"
    check(
        "session",
        session_state == "ok",
        session_detail,
        None
        if session_state == "ok"
        else "vsc login；用浏览器里已登录的会话则 vsc login --cookie，复制步骤见 vsc login --help",
    )
    free_gb = shutil.disk_usage(data_dir()).free / (1 << 30)
    check(
        "disk",
        free_gb >= 5,
        f"data/ 所在磁盘剩余 {free_gb:.0f} GB" + ("" if free_gb >= 5 else "（< 5 GB）"),
        None if free_gb >= 5 else "vsc run --prune-video 或清理 data/",
    )
    models = effective_models()
    check("models", True, "文本 {text} · 视觉 {vision} · ASR {asr} · 视频 {omni}".format(**models))
    ok_all = all(c["ok"] for c in checks)
    if ok_all:
        code = EXIT_OK
    elif session_state == "expired":
        code = EXIT_AUTH
    elif session_state == "network":
        code = EXIT_UPSTREAM
    else:
        code = EXIT_ERROR
    if CTX.json_mode:
        emit_json({"ok": ok_all, "exit_code": code, "checks": checks})
    else:
        for c in checks:
            mark = "[green]✓[/green]" if c["ok"] else "[red]✗[/red]"
            out.print(f"{mark} {c['name']:8s} {c['detail']}" + (f"  → {c['fix']}" if c["fix"] else ""))
    raise typer.Exit(code)


def auth_dashscope_key_present() -> bool:
    from .config import dashscope_api_key

    try:
        return bool(dashscope_api_key())
    except ConfigError:
        return False


def _read_password(stdin_flag: bool) -> str | None:
    if stdin_flag:
        line = sys.stdin.readline().strip()
        return line or None
    env = os.environ.get("NJUPT_PASSWORD")
    if env:
        return env
    if sys.stdin.isatty():
        return getpass.getpass("统一身份认证密码（输入不回显）: ")
    return None  # 非交互且没给密码：交由调用方报错，绝不挂起等待


def login(
    username: Annotated[
        str | None, typer.Option("--username", "-u", help="统一身份认证账号；也可用环境变量 NJUPT_USERNAME")
    ] = None,
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help="从 stdin 读第一行作为密码（非交互式）")
    ] = False,
    cookie: Annotated[
        str | None,
        typer.Option(
            "--cookie",
            help="浏览器登录后，从 DevTools 的 Network → 第一条 CourseList.aspx 请求 → Request Headers 的 cookie 行复制整行；"
            "登录凭证 PowerCreatorCMSAuthenticationID 是 HttpOnly，Console 里的 document.cookie 读不到",
        ),
    ] = None,
    status: Annotated[
        bool, typer.Option("--status", help="只检查当前会话是否有效，不登录（退出码 0 有效 / 3 无效）")
    ] = False,
    json_flag: JsonFlag = False,
) -> None:
    """登录并保存会话到 data/.session.json（约 1 小时过期）。

    非交互式：vsc login --username <学号> --password-stdin < pw.txt   或   NJUPT_PASSWORD=... vsc login -u <学号>
    浏览器 Cookie：vsc login --cookie "PowerCreatorCMSAuthenticationID=...; OnlineUser=..."（从 DevTools 的请求头复制，见 --cookie）
    """
    apply_json_flag(json_flag)
    username = username or os.environ.get("NJUPT_USERNAME")
    session = auth.new_session()
    try:
        if status:
            try:
                total = client.verify(client.make_client())  # 与 doctor 同一条路：先加载 data/.session.json
            except AuthError as exc:
                fail(str(exc), EXIT_AUTH, valid=False)
            except ValueError as exc:
                fail(f"会话无效：{exc}", EXIT_AUTH, valid=False)
            succeed_or_text(
                {"valid": True, "visible_recordings": total, "session_file": str(auth.session_file())},
                f"[green]会话有效[/green]：可见录播 {total} 条，保存于 {auth.session_file()}",
            )
            return
        if cookie:
            count = auth.import_cookie_header(session, cookie)
            if count == 0:
                raise typer.BadParameter("Cookie 头解析不到任何键值对")
            log(f"已导入 {count} 个 cookie")
            if not any(c.name == auth.CREDENTIAL_COOKIE for c in session.cookies):
                fail(
                    f"导入的 Cookie 里没有登录凭证 {auth.CREDENTIAL_COOKIE}：它是 HttpOnly，页面脚本（document.cookie）读不到。"
                    + COOKIE_COPY_STEPS,
                    EXIT_AUTH,
                )
        else:
            if not username:
                raise typer.BadParameter("非交互模式需要 --username（或环境变量 NJUPT_USERNAME）")
            password = _read_password(password_stdin)
            if password is None:
                raise typer.BadParameter(
                    "非交互模式需要 --password-stdin 或环境变量 NJUPT_PASSWORD（不会等待输入）；交互终端可省略"
                )
            auth.login_with_password(session, username, password)
            log("SSO 登录成功")
        total = client.verify(client.NjuptSmartclass(session))
        path = auth.save_session(session)
        if cookie:
            log("导入的 Cookie 不带过期时间，是否失效以 vsc login --status 为准")
        succeed_or_text(
            {"valid": True, "visible_recordings": total, "session_file": str(path)},
            f"[green]会话有效[/green]：可见录播 {total} 条，已保存到 {path}",
        )
    except NjuptSsoException as exc:
        fail(f"SSO 登录失败 [{exc.code}] {exc.message}", EXIT_AUTH)
    except AuthError as exc:
        if cookie:
            fail(
                f"导入的 Cookie 未通过验证：这张 {auth.CREDENTIAL_COOKIE} 是无效凭证或已过期，页面能打开不代表凭证有效。"
                + COOKIE_COPY_STEPS,
                EXIT_AUTH,
            )
        fail(str(exc), EXIT_AUTH)
    except ValueError as exc:
        fail(str(exc), EXIT_AUTH)
    except requests.RequestException as exc:
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)


def succeed_or_text(payload: dict, text: str) -> None:
    if CTX.json_mode:
        succeed(payload)
    else:
        out.print(text)


def semesters(json_flag: JsonFlag = False) -> None:
    """列出校历（学年 / 学期 / 起止日期），标出当前学期。供 --semester current / '2026-2027-1' 使用。"""
    apply_json_flag(json_flag)
    try:
        sc = client.make_client()
        terms = client.semester_calendar(sc)
        cur = client.current_semester(sc)
    except (AuthError, ValueError) as exc:
        fail(str(exc), EXIT_AUTH)
        return
    except requests.RequestException as exc:
        fail(f"网络错误：{exc}", EXIT_UPSTREAM)
        return
    payload = {
        "current": {"name": cur.name, "year": cur.year, "term": cur.term, "start": cur.start, "end": cur.end},
        "terms": [vars(t) | {"name": t.name} for t in terms],
    }
    if CTX.json_mode:
        succeed(payload)
        return
    for t in terms:
        mark = "  ← 当前" if t == cur else ""
        out.print(f"{t.name}  {t.start} ~ {t.end}{mark}")
