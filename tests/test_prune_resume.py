# SPDX-License-Identifier: AGPL-3.0-or-later
"""--prune-video：阶段失败不删原件；删了之后本地还能续跑，视频模式改用留下的转码件。"""

from njupt_very_smart_class import pipeline
from tests.fakes import FailingVideoModel, FakeVideoModel, FakeVisionModel, fake_model_set
from tests.test_pipeline import lesson_dir  # noqa: F401  夹具复用

ALL = ("prepare", "transcribe", "vision", "summarize", "omni", "fuse", "render")


class _Recording(FakeVideoModel):
    def __init__(self) -> None:
        super().__init__("fake-video")
        self.videos: list[str] = []

    def video_json(self, video, prompt, *, fps, timeout=None):
        self.videos.append(video.name)
        return super().video_json(video, prompt, fps=fps, timeout=timeout)


def _models(video=None):
    ms = fake_model_set()
    if video is not None:
        ms.video = video
    return ms


def test_prune_keeps_videos_when_a_stage_failed(lesson_dir):  # noqa: F811
    logs: list[str] = []
    report = pipeline.run_lesson(
        None,
        "ID42",
        stages=ALL,
        models=_models(FailingVideoModel("omni 挂了")),
        opts=pipeline.RunOptions(prune_video=True),
        log=logs.append,
    )
    assert report["state"]["errors"] == {"omni": "omni 挂了"}
    assert (lesson_dir / "VGA.mp4").exists()  # 没删：修好重跑时不用重新下载
    assert any("保留视频原件" in ln for ln in logs)


def test_pruned_lesson_resumes_locally(lesson_dir):  # noqa: F811
    opts = pipeline.RunOptions(prune_video=True)
    report = pipeline.run_lesson(None, "ID42", stages=ALL, models=_models(_Recording()), opts=opts, log=lambda *_: None)
    assert report["state"]["errors"] == {}
    assert not (lesson_dir / "VGA.mp4").exists() and (lesson_dir / "VGA_640.mp4").exists()

    again = _models(_Recording())  # 重算融合并重新渲染：原件不在也要能跑
    report = pipeline.run_lesson(
        None,
        "ID42",
        stages=("fuse", "render"),
        models=again,
        opts=pipeline.RunOptions(force_fuse=True),
        log=lambda *_: None,
    )
    assert report["state"]["errors"] == {} and (lesson_dir / "summary.md").exists()
    assert [c.split(":")[0] for c in again.text.calls] == ["fuse"]
    prepare_notes = pipeline.load_state(lesson_dir)["notes_by_stage"]["prepare"]
    assert any("没有屏幕流" in n for n in prepare_notes)  # prepare 跟着后续阶段跑了，提示就是当下的事实

    (lesson_dir / "a_result_VGA.json").unlink()  # 重算视频模式：用留下的转码件
    third = _models(_Recording())
    report = pipeline.run_lesson(None, "ID42", stages=("omni", "fuse", "render"), models=third, log=lambda *_: None)
    assert report["state"]["errors"] == {} and third.video.videos == ["VGA_640.mp4"]


def test_omni_after_prune_honors_cached_vga_verdict(lesson_dir, synthetic_video):  # noqa: F811
    import shutil

    shutil.copy(synthetic_video, lesson_dir / "Video1.mp4")
    models = _models(_Recording())
    models.vision = FakeVisionModel(screen_kind="ad")  # 屏幕流无教学内容 → 机位流
    opts = pipeline.RunOptions(camera_every_s=2.0, prune_video=True)
    report = pipeline.run_lesson(None, "ID42", stages=ALL, models=models, opts=opts, log=lambda *_: None)
    assert report["state"]["errors"] == {} and models.video.videos == ["Video1_640.mp4"]

    (lesson_dir / "a_result_Video1.json").unlink()
    (lesson_dir / "VGA_640.mp4").write_bytes(b"stale")  # 屏幕流的转码件在也不能喂：上次已判定它没有教学内容
    again = _models(_Recording())
    report = pipeline.run_lesson(None, "ID42", stages=("omni",), models=again, log=lambda *_: None)
    assert report["state"]["errors"] == {} and again.video.videos == ["Video1_640.mp4"]
