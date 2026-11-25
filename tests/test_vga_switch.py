"""VGA 无教学内容（空桌面 / 动态广告）时切机位流。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import shutil

from PIL import Image

from njupt_very_smart_class import pipeline, slides, vision
from njupt_very_smart_class.models import ModelSet
from tests.fakes import FakeVisionModel, fake_model_set
from tests.test_pipeline import lesson_dir  # noqa: F401  夹具复用


def test_projection_roi_synthetic():
    im = Image.new("RGB", (1920, 1080), (30, 90, 60))
    im.paste(Image.new("RGB", (700, 500), (250, 250, 250)), (1100, 200))
    left, top, right, bottom = slides.projection_roi(im)
    assert 1050 <= left <= 1100 and 150 <= top <= 200 and 1800 <= right <= 1850 and 700 <= bottom <= 730
    assert slides.projection_roi(Image.new("RGB", (640, 480), (40, 40, 40))) is None
    assert slides.projection_roi(Image.new("RGB", (640, 480), (230, 230, 230))) is None  # 全白墙面不是幕布


def test_sample_frames_uniform(synthetic_video, tmp_path):
    frames = slides.sample_frames(synthetic_video, tmp_path / "raw", every_s=2.0)
    assert [round(t, 1) for t, _ in frames] == [0.0, 2.0, 4.0, 6.0, 8.0]
    assert all(p.exists() for _, p in frames)


def _fake_set(screen_kind: str) -> ModelSet:
    ms = fake_model_set()
    ms.vision = FakeVisionModel(screen_kind=screen_kind)
    return ms


def test_judge_screen_thresholds(lesson_dir, synthetic_video):  # noqa: F811
    manifest = [{"index": i + 1, "time": float(i * 10), "file": f"slide_{i + 1:03d}.jpg"} for i in range(8)]
    (lesson_dir / "slides").mkdir(exist_ok=True)
    for i in range(8):
        Image.new("RGB", (64, 64), (i * 10, 0, 0)).save(lesson_dir / "slides" / f"slide_{i + 1:03d}.jpg")
    (lesson_dir / "slides" / "slides.json").write_text(json.dumps(manifest), encoding="utf-8")
    parsed = [slides.Slide(**d) for d in manifest]
    v = vision.judge_screen(FakeVisionModel(screen_kind="ad"), lesson_dir, parsed, lesson_dir / "VGA.mp4")
    assert v["usable"] is False and v["teaching"] == 0 and v["total"] == 8
    v = vision.judge_screen(FakeVisionModel(screen_kind="document"), lesson_dir, parsed, lesson_dir / "VGA.mp4")
    assert v["usable"] is True and v["teaching"] == 8
    # 样本不足时按时间等距截帧
    v = vision.judge_screen(FakeVisionModel(screen_kind="slide"), lesson_dir, [], lesson_dir / "VGA.mp4")
    assert v["total"] == 8 and v["usable"] is True and not (lesson_dir / "slides" / "_judge").exists()


def test_pipeline_switches_to_camera_when_vga_is_ad(lesson_dir, synthetic_video, monkeypatch):  # noqa: F811
    shutil.copy(synthetic_video, lesson_dir / "Video1.mp4")
    models = _fake_set("ad")
    logs: list[str] = []
    stages = ("prepare", "transcribe", "vision", "summarize", "render")
    opts = pipeline.RunOptions(camera_every_s=2.0)
    pipeline.run_lesson(None, "ID42", stages=stages, opts=opts, models=models, log=logs.append)
    state = json.loads((lesson_dir / "state.json").read_text(encoding="utf-8"))
    assert state["vga_judge"]["usable"] is False
    manifest = json.loads((lesson_dir / "slides" / "slides.json").read_text(encoding="utf-8"))
    assert manifest and all(m["source"] == "camera" for m in manifest)
    frames = json.loads((lesson_dir / "slides" / "frames.json").read_text(encoding="utf-8"))
    assert frames and all(f["source"] == "camera" for f in frames)
    assert any("切换到机位流" in line for line in logs) and any("机位关键帧" in line for line in logs)
    assert "camera:" in " ".join(models.vision.calls)
    # 判定结果缓存：再跑一次不再判定
    models2 = _fake_set("ad")
    pipeline.run_lesson(None, "ID42", stages=stages, opts=opts, models=models2, log=lambda *_: None)
    assert not any(c.startswith("judge:") for c in models2.vision.calls)


def test_pipeline_keeps_vga_when_teaching(lesson_dir, synthetic_video):  # noqa: F811
    models = _fake_set("slide")
    stages = ("prepare", "transcribe", "vision", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=models, log=lambda *_: None)
    state = json.loads((lesson_dir / "state.json").read_text(encoding="utf-8"))
    assert state["vga_judge"]["usable"] is True
    assert "camera:" not in " ".join(models.vision.calls)


def test_switch_without_camera_skips_screen_vision(lesson_dir, synthetic_video):  # noqa: F811
    # VGA 不可用又没有机位流：不为桌面/广告烧视觉 token，只留音频路线
    models = _fake_set("desktop")
    logs: list[str] = []
    stages = ("prepare", "transcribe", "vision", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=models, log=logs.append)
    assert any("无法切换视觉源" in line for line in logs)
    assert not any(c.startswith("vision:") for c in models.vision.calls)  # 只有判定调用
    assert (lesson_dir / "summary.md").exists()
