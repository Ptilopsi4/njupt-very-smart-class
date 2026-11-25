# SPDX-License-Identifier: AGPL-3.0-or-later
import json

import pytest
import requests
import typer

from njupt_very_smart_class import auth
from njupt_very_smart_class.auth import AuthError, import_cookie_header, load_session, save_session
from njupt_very_smart_class.cli import parse_date as _parse_date
from njupt_very_smart_class.paths import sanitize_filename


@pytest.mark.parametrize(
    "header, expected",
    [
        ("a=1; b=2", {"a": "1", "b": "2"}),
        ("Cookie: a=1; Path=/; b=x=y", {"a": "1", "Path": "/", "b": "x=y"}),
        ("  ; ;c=3;; ", {"c": "3"}),
        ("garbage", {}),
    ],
)
def test_import_cookie_header(header, expected):
    s = requests.Session()
    n = import_cookie_header(s, header)
    assert {c.name: c.value for c in s.cookies} == expected
    assert n == len(expected)


def test_session_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "session_file", lambda: tmp_path / ".session.json")
    s = requests.Session()
    s.cookies.set("ASP.NET_SessionId", "abc", domain="njupt.smartclass.cn", path="/")
    s.cookies.set("CASTGC", "tgc", domain="i.njupt.edu.cn", path="/cas")
    save_session(s)
    restored = requests.Session()
    assert load_session(restored)
    jar = {(c.name, c.domain, c.path): c.value for c in restored.cookies}
    assert jar == {
        ("ASP.NET_SessionId", "njupt.smartclass.cn", "/"): "abc",
        ("CASTGC", "i.njupt.edu.cn", "/cas"): "tgc",
    }


def test_load_session_corrupt(tmp_path, monkeypatch):
    p = tmp_path / ".session.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(auth, "session_file", lambda: p)
    with pytest.raises(AuthError):
        load_session(requests.Session())


def test_load_session_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "session_file", lambda: tmp_path / "nope.json")
    assert not load_session(requests.Session())


@pytest.mark.parametrize(
    "value, expected",
    [("2026-11-24", "2026-11-24"), (None, ""), ("", ""), ("2026-1-5", "2026-01-05")],
)
def test_parse_date(value, expected):
    assert _parse_date(value, "--start") == expected


def test_parse_date_invalid():
    with pytest.raises(typer.BadParameter):
        _parse_date("2026-13-40", "--start")


def test_sanitize_filename():
    assert sanitize_filename('a:b/c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert sanitize_filename("  ...  ") == "untitled"
    assert sanitize_filename("x" * 100, max_len=5) == "xxxxx"
    assert json.loads(json.dumps({"ok": sanitize_filename("高数(上)")}))["ok"] == "高数(上)"


def _fake_sc(payload):
    from types import SimpleNamespace

    class _R:
        status_code = 200

        def json(self):
            return payload

    return SimpleNamespace(
        base_url="https://njupt.smartclass.cn",
        get_csrk_token=lambda: "t",
        session=SimpleNamespace(get=lambda *a, **k: _R()),
    )


def test_semester_calendar_and_current():
    from njupt_very_smart_class import client as C

    payload = {
        "Success": True,
        "Value": [
            {
                "ShowName": "2025-2026",
                "ChildList": [{"ShowName": "2", "StartTime": "2026-03-02 00:00:00", "StopTime": "2026-07-05 23:59:59"}],
            },
            {
                "ShowName": "2026-2027",
                "ChildList": [{"ShowName": "1", "StartTime": "2026-08-31 00:00:00", "StopTime": "2027-01-15 23:59:59"}],
            },
        ],
    }
    sc = _fake_sc(payload)
    terms = C.semester_calendar(sc)
    assert [t.name for t in terms] == ["2025-2026 学年第 2 学期", "2026-2027 学年第 1 学期"]
    assert terms[0].start == "2026-03-02" and terms[0].end == "2026-07-05"
    assert C.current_semester(sc, today="2026-11-24") == terms[1]
    assert C.current_semester(sc, today="2026-08-01") == terms[1]  # 假期取最近开学的一学期
    with pytest.raises(ValueError):
        C.semester_calendar(_fake_sc({"Success": False, "Value": None}))


def test_semester_bounds_parsing(monkeypatch):
    from njupt_very_smart_class import cli_lessons as cli

    payload = {
        "Success": True,
        "Value": [
            {
                "ShowName": "2026-2027",
                "ChildList": [{"ShowName": "1", "StartTime": "2026-08-31 00:00:00", "StopTime": "2027-01-15 23:59:59"}],
            }
        ],
    }
    sc = _fake_sc(payload)
    assert cli._semester_bounds(sc, None) == ("", "")
    assert cli._semester_bounds(sc, "current") == ("2026-08-31", "2027-01-15")
    assert cli._semester_bounds(sc, "2026-2027-1") == ("2026-08-31", "2027-01-15")
    assert cli._semester_bounds(sc, "2026-2027 学年第 1 学期") == ("2026-08-31", "2027-01-15")
    with pytest.raises(typer.BadParameter):
        cli._semester_bounds(sc, "1999-2000-1")
