# SPDX-License-Identifier: AGPL-3.0-or-later
import re
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from njupt_very_smart_class import paths


@pytest.fixture(scope="session")
def synthetic_video(tmp_path_factory) -> Path:
    """9 s 视频：testsrc2 / rgbtestsrc / smptebars 各 3 s（两个硬切，均有纹理）。

    配 440 Hz 正弦音频。
    """
    out = tmp_path_factory.mktemp("media") / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:r=10:d=3",
            "-f",
            "lavfi",
            "-i",
            "rgbtestsrc=s=320x240:r=10:d=3",
            "-f",
            "lavfi",
            "-i",
            "smptebars=s=320x240:r=10:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=9",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "3:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(out),
        ],
        check=True,
        timeout=120,
    )
    return out


class _RangeHandler(SimpleHTTPRequestHandler):
    """支持 Range 的静态文件服务；ignore_range=True 时退化为忽略 Range。"""

    ignore_range = False

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        if self.ignore_range or not rng:
            return super().do_GET()
        m = re.match(r"bytes=(\d+)-$", rng)
        if not m:
            return super().do_GET()
        path = Path(self.translate_path(self.path))
        start = int(m.group(1))
        size = path.stat().st_size
        if start >= size:
            self.send_response(416)
            self.end_headers()
            return
        with path.open("rb") as fh:
            fh.seek(start)
            body = fh.read()
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


@pytest.fixture
def range_server(tmp_path):
    handler = type("Handler", (_RangeHandler,), {})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: handler(*a, directory=str(tmp_path), **k))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def session():
    return requests.Session()


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path
