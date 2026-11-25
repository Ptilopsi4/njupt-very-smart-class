"""薄封装 vendor 客户端：构造已登录会话、列出录播、取详情。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterator
from dataclasses import dataclass

import requests

from .auth import AuthError, load_session, new_session
from .vendor.njupt_smartclass import (
    NjuptSmartclass,
    NjuptSmartclassVideoSearchCondition,
    NjuptSmartclassVideoSummary,
)


def _auth_error() -> AuthError:
    # 未登录 / Cookie 过期时接口回 HTML 登录页而不是 JSON
    return AuthError("会话无效：接口返回非 JSON（未登录或 Cookie 过期），请重新 vsc login")


def make_client() -> NjuptSmartclass:
    session = new_session()
    if not load_session(session):
        raise AuthError("未登录：先运行 vsc login")
    return NjuptSmartclass(session)


def iter_videos(
    client: NjuptSmartclass,
    *,
    title_key: str = "",
    start_date: str = "",
    end_date: str = "",
    page_size: int = 100,
    max_results: int | None = None,
    descending: bool = True,
) -> Iterator[NjuptSmartclassVideoSummary]:
    """descending=True 对应接口 Order=1（时间倒序）。"""
    condition = NjuptSmartclassVideoSearchCondition(
        title_key=title_key,
        page_size=page_size,
        start_date=start_date,
        end_date=end_date,
        order=1 if descending else 0,
    )
    yielded = 0
    try:
        for video in client.search_video_all(condition):
            yield video
            yielded += 1
            if max_results is not None and yielded >= max_results:
                break
    except requests.exceptions.JSONDecodeError as exc:
        raise _auth_error() from exc


@dataclass(frozen=True)
class Semester:
    year: str  # 如 2026-2027
    term: str  # 如 1
    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD

    @property
    def name(self) -> str:
        return f"{self.year} 学年第 {self.term} 学期"


def semester_calendar(client: NjuptSmartclass) -> list[Semester]:
    """校历：GET /SchoolYear/GetSchoolYears（需 csrkToken），按时间升序。"""
    resp = client.session.get(
        f"{client.base_url}/SchoolYear/GetSchoolYears",
        params={"SchoolID": 0, "csrkToken": client.get_csrk_token()},
        timeout=30,
    )
    try:
        data = resp.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise _auth_error() from exc
    if not data.get("Success") or not isinstance(data.get("Value"), list):
        raise ValueError(f"校历接口返回异常：{str(data)[:200]}")
    terms = []
    for year in data["Value"]:
        for child in year.get("ChildList") or []:
            terms.append(
                Semester(
                    year=str(year.get("ShowName") or ""),
                    term=str(child.get("ShowName") or ""),
                    start=(child.get("StartTime") or "")[:10],
                    end=(child.get("StopTime") or "")[:10],
                )
            )
    return sorted(terms, key=lambda s: s.start)


def current_semester(client: NjuptSmartclass, today: str | None = None) -> Semester:
    """包含今天的那一学期；都不包含时取最近开始的一学期。"""
    from datetime import date

    today = today or date.today().isoformat()
    terms = semester_calendar(client)
    for s in terms:
        if s.start <= today <= s.end:
            return s
    return max(terms, key=lambda s: s.start)


def verify(client: NjuptSmartclass) -> int:
    """用一次 page_size=1 的列表请求验证会话；返回可见录播总数。"""
    try:
        return client.search_video(NjuptSmartclassVideoSearchCondition(page_size=1)).total_count
    except requests.exceptions.JSONDecodeError as exc:
        raise _auth_error() from exc
