# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from njupt_very_smart_class import client
from njupt_very_smart_class.cli import app
from njupt_very_smart_class.vendor.njupt_smartclass import NjuptSmartclassVideoSummary

runner = CliRunner()
TZ = timezone(timedelta(hours=8))


def _summary(i: int, title: str) -> NjuptSmartclassVideoSummary:
    start = datetime(2026, 11, 20, 8, 0, tzinfo=TZ)
    return NjuptSmartclassVideoSummary(
        id=f"ID-{i:04d}-ABCDEFGH",
        title=title,
        start_time=start,
        stop_time=start + timedelta(minutes=45),
        course_name="高等数学（上）",
        teachers="张老师",
        classroom_name="教1-101",
        cover_url="",
    )


@pytest.fixture
def fake_client(monkeypatch):
    videos = [
        _summary(1, "第一章 极限与连续 · 这是一个非常非常长的标题用于验证省略号是否生效"),
        _summary(2, "第二章 导数"),
    ]
    monkeypatch.setattr(client, "make_client", lambda: object())
    monkeypatch.setattr(client, "iter_videos", lambda sc, **kw: iter(videos[: kw.get("max_results") or None]))
    return videos


def test_list_table_renders(fake_client):
    result = runner.invoke(app, ["list", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert "ID-0001" in result.output
    assert "11-20 08:00" in result.output
    assert "45m" in result.output
    assert "共 1 条（--limit 1）" in result.output


def test_list_table_fits_narrow_console(fake_client, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert max(len(line) for line in lines) <= 60, result.output
    assert "…" in result.output  # 长标题被省略


def test_list_json(fake_client):
    result = runner.invoke(app, ["--json", "list", "--limit", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["count"] == 2
    item = payload["items"][0]
    assert item["new_id"] == "ID-0001-ABCDEFGH" and item["course"] == "高等数学（上）" and item["minutes"] == 45


def test_list_json_no_match_exit_code(fake_client, monkeypatch):
    videos = []
    monkeypatch.setattr(client, "make_client", lambda: object())
    monkeypatch.setattr(client, "iter_videos", lambda sc, **kw: iter(videos))
    result = runner.invoke(app, ["--json", "list"])
    assert result.exit_code == 6 and json.loads(result.output)["ok"] is False


def test_list_per_command_json_flag_still_works(fake_client):
    result = runner.invoke(app, ["list", "--json", "--limit", "1"])
    payload = json.loads(result.output)  # 与全局 --json 同一信封
    assert result.exit_code == 0 and payload["count"] == 1 and payload["items"][0]["new_id"].startswith("ID-0001")
