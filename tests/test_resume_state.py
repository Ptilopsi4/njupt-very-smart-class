# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态取自落盘产物：--stages 子集重跑后，没请求的阶段沿用上次的 errors 与处理提示，verdict 和 WARNING 不变。"""

import json

from typer.testing import CliRunner

from njupt_very_smart_class import client, pipeline
from njupt_very_smart_class.cli import app
from tests.fakes import FailingAsrModel, FakeAsrModel, FakeVideoModel, fake_model_set
from tests.test_pipeline import lesson_dir  # noqa: F401  夹具复用

ALL = ("prepare", "transcribe", "vision", "summarize", "omni", "fuse", "render")
runner = CliRunner()


def _asr_failed_set():
    ms = fake_model_set()
    ms.asr, ms.video = FailingAsrModel("ASR 挂了"), FakeVideoModel()
    return ms


def test_asr_failure_survives_partial_rerun(lesson_dir, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    monkeypatch.setattr(client, "make_client", lambda: object())
    r1 = pipeline.run_lesson(None, "ID42", stages=ALL, models=_asr_failed_set(), log=lambda *_: None)
    assert r1["state"]["errors"] == {"transcribe": "ASR 挂了"}

    r = runner.invoke(app, ["--json", "run", lesson_dir.name, "--stages", "fuse,render"])
    assert r.exit_code == 5, r.output  # 转写没解决，课时就没完成
    payload = json.loads(r.stdout)
    assert payload["errors"] == {"transcribe": "ASR 挂了"} and "asr_failed" in payload["verdict"]
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert "转写阶段失败" in text and "老师未用麦" not in text and "阶段 transcribe 失败" in text
    state = pipeline.load_state(lesson_dir)
    assert any("阶段 transcribe 失败" in n for n in state["notes"])  # 没请求的阶段，处理提示也还在
    assert any("阶段 transcribe 失败" in n for n in state["notes_by_stage"]["transcribe"])

    r2 = pipeline.run_lesson(None, "ID42", stages=ALL, models=fake_model_set(), log=lambda *_: None)
    assert r2["state"]["errors"] == {} and "asr_failed" not in pipeline.classify_lesson(lesson_dir)


def test_verdict_comes_from_artifact_not_last_run(lesson_dir):  # noqa: F811
    (lesson_dir / "c_result.json").write_text(
        json.dumps({"no_speech": True, "reason": "transcribe_failed", "result": {}}), encoding="utf-8"
    )
    (lesson_dir / "state.json").write_text(json.dumps({"errors": {}}), encoding="utf-8")
    assert pipeline.classify_lesson(lesson_dir) == "asr_failed"


def test_notes_from_unrequested_stages_are_kept(lesson_dir, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    models = fake_model_set()
    models.asr = FakeAsrModel(speech_ms=1000)  # 低语音比：transcribe 阶段的提示
    stages = ("prepare", "transcribe", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=models, log=lambda *_: None)
    assert any("有效语音只占" in n for n in pipeline.load_state(lesson_dir)["notes"])

    pipeline.run_lesson(None, "ID42", stages=("render",), models=None, log=lambda *_: None)
    state = pipeline.load_state(lesson_dir)
    assert any("有效语音只占" in n for n in state["notes"])
    assert any("有效语音只占" in n for n in state["notes_by_stage"]["transcribe"])
    assert "有效语音只占" in (lesson_dir / "summary.md").read_text(encoding="utf-8")
    assert list(state["notes_by_stage"]) == ["transcribe"]  # 没有提示的阶段不占键，顺序按阶段先后


def test_requested_stage_replaces_its_old_notes(lesson_dir):  # noqa: F811
    models = fake_model_set()
    models.asr = FakeAsrModel(speech_ms=1000)
    pipeline.run_lesson(None, "ID42", stages=("prepare", "transcribe", "render"), models=models, log=lambda *_: None)
    assert any("有效语音只占" in n for n in pipeline.load_state(lesson_dir)["notes"])
    (lesson_dir / "transcript.json").unlink()  # 重新转写：这次语音占比正常
    pipeline.run_lesson(
        None, "ID42", stages=("prepare", "transcribe", "render"), models=fake_model_set(), log=lambda *_: None
    )
    assert not any("有效语音只占" in n for n in pipeline.load_state(lesson_dir)["notes"])


def test_legacy_flat_notes_migrate_once(lesson_dir):  # noqa: F811
    stages = ("prepare", "transcribe", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=fake_model_set(), log=lambda *_: None)
    state = pipeline.load_state(lesson_dir)
    state["notes"] = ["旧版留下的一条提示"]
    state.pop("notes_by_stage", None)
    (lesson_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    pipeline.run_lesson(None, "ID42", stages=("render",), models=fake_model_set(), log=lambda *_: None)  # 只重渲染
    assert pipeline.load_state(lesson_dir)["notes"] == ["旧版留下的一条提示"]
    assert "旧版留下的一条提示" in (lesson_dir / "summary.md").read_text(encoding="utf-8")

    pipeline.run_lesson(None, "ID42", stages=("transcribe", "render"), models=fake_model_set(), log=lambda *_: None)
    assert "旧版留下的一条提示" not in pipeline.load_state(lesson_dir)["notes"]
