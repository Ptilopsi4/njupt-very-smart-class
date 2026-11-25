# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path

import pytest

from njupt_very_smart_class import download
from njupt_very_smart_class.download import (
    StreamSource,
    download_file,
    fetch_segments,
    parse_index_xml,
    parse_size,
)

INDEX_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<Info><Time></Time><StopTime></StopTime>
<Video1 Width="1920" Height="1080" Src="Video1.mp4" StartTime="0"/>
<Video2 Width="1920" Height="1080" Src="sub/Video2.mp4" StartTime="1621"/>
<Video3 Width="0" Height="0" Src="" StartTime="0"/>
<VGA Width="1024" Height="768" Src="VGA.mp4" StartTime="21"/>
<File Style="mp4" /><Title></Title></Info>"""


def test_parse_index_xml_resolves_relative_urls():
    # urljoin 会丢掉签名 query，mp4 直链本就不带签名
    base = "https://h.example/dir/index.xml?timestamp=1&authKey=x"
    srcs = parse_index_xml(INDEX_XML, base, types=("VGA", "Video1", "Video2"))
    assert srcs == [
        StreamSource("VGA", "https://h.example/dir/VGA.mp4", 21.0, 1024, 768),
        StreamSource("Video1", "https://h.example/dir/Video1.mp4", 0.0, 1920, 1080),
        StreamSource("Video2", "https://h.example/dir/sub/Video2.mp4", 1621.0, 1920, 1080),
    ]


def test_parse_index_xml_requires_info():
    with pytest.raises(ValueError):
        parse_index_xml(b"<Other/>", "https://h/x.xml")


def test_parse_size():
    assert parse_size("5GB") == 5 << 30
    assert parse_size("512 MB") == 512 << 20
    assert parse_size("1024") == 1024


def test_download_full_and_skip(session, range_server, tmp_path):
    payload = b"x" * 300_000
    (tmp_path / "a.bin").write_bytes(payload)
    dest = tmp_path / "out" / "a.bin"
    dest.parent.mkdir()
    r1 = download_file(session, f"{range_server}/a.bin", dest)
    assert dest.read_bytes() == payload
    assert not r1.skipped and not r1.resumed
    r2 = download_file(session, f"{range_server}/a.bin", dest)
    assert r2.skipped


def test_download_resumes_with_range(session, range_server, tmp_path):
    payload = bytes(range(256)) * 1000
    (tmp_path / "b.bin").write_bytes(payload)
    dest = tmp_path / "out" / "b.bin"
    dest.parent.mkdir()
    dest.with_name("b.bin.part").write_bytes(payload[:100_000])
    r = download_file(session, f"{range_server}/b.bin", dest)
    assert r.resumed
    assert dest.read_bytes() == payload


def test_download_restarts_when_server_ignores_range(session, range_server, tmp_path):
    payload = b"y" * 50_000
    (tmp_path / "c.bin").write_bytes(payload)
    dest = tmp_path / "out" / "c.bin"
    dest.parent.mkdir()
    dest.with_name("c.bin.part").write_bytes(b"garbage")
    import tests.conftest as c

    original = c._RangeHandler.ignore_range
    c._RangeHandler.ignore_range = True
    try:
        r = download_file(session, f"{range_server}/c.bin", dest)
    finally:
        c._RangeHandler.ignore_range = original
    assert not r.resumed
    assert dest.read_bytes() == payload


def test_download_part_complete_returns_416_path(session, range_server, tmp_path):
    payload = b"z" * 20_000
    (tmp_path / "d.bin").write_bytes(payload)
    dest = tmp_path / "out" / "d.bin"
    dest.parent.mkdir()
    dest.with_name("d.bin.part").write_bytes(payload)
    r = download_file(session, f"{range_server}/d.bin", dest)
    assert r.resumed and dest.read_bytes() == payload
    assert not Path(str(dest) + ".part").exists()


def test_download_4xx_is_not_retried(session, range_server, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(download.time, "sleep", lambda s: sleeps.append(s))
    calls = []
    real_get = session.get

    def counting_get(url, **kw):
        calls.append(url)
        return real_get(url, **kw)

    monkeypatch.setattr(session, "get", counting_get)
    dest = tmp_path / "out" / "gone.mp4"
    dest.parent.mkdir()
    with pytest.raises(RuntimeError, match="HTTP 404") as excinfo:
        download_file(session, f"{range_server}/gone.mp4?authKey=SECRET", dest)
    assert len(calls) == 1 and sleeps == []  # 4xx 重试多少次都一样
    assert "SECRET" not in str(excinfo.value) and "清理" in str(excinfo.value)


def test_download_5xx_is_retried(session, tmp_path, monkeypatch):
    import requests as rq

    calls, sleeps = [], []

    class _Resp:
        status_code = 503
        headers = {}
        text = "busy"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            raise rq.HTTPError("503")

    monkeypatch.setattr(session, "get", lambda url, **kw: calls.append(url) or _Resp())
    monkeypatch.setattr(download.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(RuntimeError, match="重试"):
        download_file(session, "https://h.example/x.mp4", tmp_path / "o" / "x.mp4")
    assert len(calls) == 11 and len(sleeps) == 10


def test_fetch_segments_missing_media_error_hides_signature(session, range_server, tmp_path):
    from datetime import datetime

    from njupt_very_smart_class.vendor.njupt_smartclass import (
        NjuptSmartclassVideoInfo,
        NjuptSmartclassVideoSegmentInfo,
    )

    seg = NjuptSmartclassVideoSegmentInfo(index_file_uri=f"{range_server}/gone/0/Index.xml?timestamp=1&authKey=SECRET")
    info = NjuptSmartclassVideoInfo(
        id="X",
        title="t",
        start_time=datetime(2024, 1, 1),
        stop_time=datetime(2024, 1, 1),
        course_name="c",
        segments=[seg],
    )
    with pytest.raises(RuntimeError, match="HTTP 404") as excinfo:
        fetch_segments(session, info, tmp_path / "lesson")
    msg = str(excinfo.value)
    assert "SECRET" not in msg and "authKey" not in msg
    assert "清理" in msg


def test_fetch_segments_writes_streams_json(session, range_server, tmp_path):
    from datetime import datetime

    from njupt_very_smart_class.vendor.njupt_smartclass import (
        NjuptSmartclassVideoInfo,
        NjuptSmartclassVideoSegmentInfo,
    )

    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "index.xml").write_bytes(INDEX_XML)
    seg = NjuptSmartclassVideoSegmentInfo(index_file_uri=f"{range_server}/pub/index.xml?authKey=k")
    info = NjuptSmartclassVideoInfo(
        id="Y",
        title="t",
        start_time=datetime(2026, 1, 1),
        stop_time=datetime(2026, 1, 1),
        course_name="c",
        segments=[seg],
    )
    lesson = tmp_path / "lesson"
    segments = fetch_segments(session, info, lesson, types=("VGA", "Video1"))
    assert len(segments) == 1 and [s.kind for s in segments[0].sources] == ["VGA", "Video1"]
    streams = json.loads((lesson / "streams.json").read_text(encoding="utf-8"))
    assert streams[0] == {
        "kind": "VGA",
        "file": "VGA.mp4",
        "start_time": 21.0,
        "width": 1024,
        "height": 768,
        "url": f"{range_server}/pub/VGA.mp4",
    }
    assert (lesson / "index.xml").read_bytes() == INDEX_XML
    # 只补下 Video1 时，VGA 条目保留、Video1 条目补进（StartTime 不能丢）
    fetch_segments(session, info, lesson, types=("Video1",))
    merged = json.loads((lesson / "streams.json").read_text(encoding="utf-8"))
    assert [d["file"] for d in merged] == ["VGA.mp4", "Video1.mp4"] and merged[0]["start_time"] == 21.0
