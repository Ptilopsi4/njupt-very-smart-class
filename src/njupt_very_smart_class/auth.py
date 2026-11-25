"""统一身份认证登录与浏览器 Cookie 导入；会话持久化到 data/.session.json（已 gitignore）。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import getpass
import json
import os
from pathlib import Path

import requests

from .paths import session_file
from .vendor.njupt_sso import NjuptSso

SSO_SERVICE = "https://njupt.smartclass.cn/SystemSpace/Redirect.aspx"
SMARTCLASS_HOST = "njupt.smartclass.cn"
# 接口只认这一个 cookie，其余（OnlineUser、loginLivePage_*）不校验。它是 HttpOnly，页面脚本读不到，
# 手工导入只能来自 DevTools 的请求头。
CREDENTIAL_COOKIE = "PowerCreatorCMSAuthenticationID"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class AuthError(RuntimeError):
    pass


class TimeoutSession(requests.Session):
    """vendor 客户端的请求都不带 timeout；在这里统一兜底，服务端不应答时不会无限等。显式传入的 timeout 优先。"""

    default_timeout = 30.0

    def request(self, method: str, url, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.default_timeout)
        return super().request(method, url, **kwargs)


def new_session() -> TimeoutSession:
    session = TimeoutSession()
    session.headers["User-Agent"] = USER_AGENT
    return session


def login_with_password(session: requests.Session, username: str, password: str) -> None:
    session.cookies.clear()
    sso = NjuptSso(session)
    sso.login(username, password)
    sso.grant_service(SSO_SERVICE)


def import_cookie_header(session: requests.Session, cookie_header: str) -> int:
    """把浏览器 DevTools 里复制的 Cookie 头（可带 "Cookie:" 前缀）注入 smartclass 域。

    这里不判断凭证是否有效：无效凭证与有效凭证同名、同格式，只有服务端能分辨，调用方必须再用接口验证。
    """
    header = cookie_header.strip()
    if header.lower().startswith("cookie:"):
        header = header[7:]
    count = 0
    for pair in header.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        session.cookies.set(name.strip(), value.strip(), domain=SMARTCLASS_HOST)
        count += 1
    return count


def read_password() -> str:
    env = os.environ.get("NJUPT_PASSWORD")
    if env:
        return env
    return getpass.getpass("统一身份认证密码（输入不回显）: ")


def save_session(session: requests.Session) -> Path:
    path = session_file()
    jar = [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "expires": c.expires,
            "secure": c.secure,
        }
        for c in session.cookies
    ]
    path.write_text(json.dumps(jar, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_session(session: requests.Session) -> bool:
    path = session_file()
    if not path.exists():
        return False
    try:
        jar = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthError(f"会话文件损坏：{path}（{exc}），请重新 vsc login") from exc
    for c in jar:
        session.cookies.set_cookie(
            requests.cookies.create_cookie(
                name=c["name"],
                value=c["value"],
                domain=c["domain"],
                path=c.get("path") or "/",
                expires=c.get("expires"),
                secure=bool(c.get("secure")),
            )
        )
    return True
