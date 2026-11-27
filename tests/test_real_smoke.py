# SPDX-License-Identifier: AGPL-3.0-or-later
"""真实百炼接口的冒烟测试：临时上传、一次图片调用、一次转写。要网络和 API Key，默认跳过。

    VSC_REAL_SMOKE=1 uv run pytest -q -m real

改了 bailian.py / media.py 的上传与转写路径后跑一次；CI 不跑。假模型读不到文件句柄，关句柄那类 bug 只有真上传才会暴露。
"""

import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from njupt_very_smart_class import bailian
from njupt_very_smart_class.config import effective_models, load_dotenv

if os.environ.get("VSC_REAL_SMOKE"):
    load_dotenv()  # 只在主动开启时读 .env，不让本地 .env 影响其余离线测试

pytestmark = [
    pytest.mark.real,
    pytest.mark.skipif(
        not (os.environ.get("VSC_REAL_SMOKE") and os.environ.get("DASHSCOPE_API_KEY")),
        reason="需要 VSC_REAL_SMOKE=1 和 DASHSCOPE_API_KEY",
    ),
]


def _sine_wav(path: Path) -> Path:
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
            "sine=frequency=440:duration=1",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path


def test_upload_image_and_transcription_roundtrip(tmp_path):
    models = effective_models()
    client = bailian.BailianClient()
    # 1. 临时上传：和转写走同一条路（wav → FLAC → OSS），回一个 oss:// 地址
    wav = _sine_wav(tmp_path / "smoke.wav")
    uploaded = bailian.DashScopeAsrModel._uploadable(wav)
    url = client.upload_temp(uploaded, models["asr"])
    assert url.startswith("oss://") and url.endswith(".flac"), url
    # 2. 视觉：一张图 → JSON 对象
    image = tmp_path / "dot.png"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(image)
    vision = bailian.DashScopeVisionModel(client, models["vision"])
    out = vision.images_json('只输出一个 JSON 对象：{"ok": true}，不要其它内容', [image.read_bytes()], thinking=False)
    assert out.data.get("ok") is True, out.data
    assert client.spent_detail[models["vision"]]["input"] > 0
    # 3. 转写：1 秒正弦波 → 提交、轮询、下载三步走完；无词或被判成噪音都算链路通
    try:
        result = bailian.DashScopeAsrModel(client, models["asr"]).transcribe(wav)
    except bailian.BailianError as exc:
        assert "没有句子" in str(exc), exc
    else:
        assert result.no_speech or result.segments
