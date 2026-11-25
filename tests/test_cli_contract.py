"""CLI 契约：全局 --json 路由、退出码、非交互登录、status/summary/doctor。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import shutil

import requests
from typer.testing import CliRunner

from njupt_very_smart_class import client, pipeline
from njupt_very_smart_class.cli import app
from tests.fakes import fake_model_set
from tests.test_pipeline import lesson_dir  # noqa: F401
from tests.util import plain

runner = CliRunner()


def _invoke(args, **kw):
    result = runner.invoke(app, args, **kw)
    return result


def _why(result):
    return f"exit={result.exit_code} exc={result.exception!r}" + chr(10) + result.output[-600:]


def test_run_json_puts_result_on_stdout_and_logs_on_stderr(lesson_dir, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    result = _invoke(
        ["--json", "run", "--local", str(lesson_dir), "--stages", "prepare,transcribe,vision,summarize,render"]
    )
    assert result.exit_code == 0, _why(result)
    payload = json.loads(result.stdout)  # stdout 只有 JSON
    assert payload["ok"] is True and payload["mode"] == "local" and payload["count"] == 1
    assert payload["results"][0]["summary"].endswith("summary.md")
    assert "[prepare]" not in result.stdout  # 日志没混进 stdout


def _single_mode(monkeypatch):
    """单节模式需要会话；课时已下载且不跑 fetch 时客户端不会被真正使用。"""
    monkeypatch.setattr(client, "make_client", lambda: object())


def test_run_single_json_reports_stage_errors(lesson_dir, monkeypatch):  # noqa: F811
    from tests.fakes import FailingVideoModel

    _single_mode(monkeypatch)
    ms = fake_model_set()
    ms.video = FailingVideoModel("模拟故障")
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: ms)
    result = _invoke(["--json", "run", lesson_dir.name, "--stages", "prepare,omni,render"])
    assert result.exit_code == 5, _why(result)  # 有阶段失败 → 部分完成
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["mode"] == "single" and payload["errors"] == {"omni": "模拟故障"}


def test_rerun_clears_stale_stage_errors(lesson_dir, monkeypatch):  # noqa: F811
    """失败阶段重跑成功后 state.errors 必须清空，否则退出码 5 永远收敛不到 0。"""
    from tests.fakes import FailingVideoModel

    _single_mode(monkeypatch)
    ms = fake_model_set()
    ms.video = FailingVideoModel("模拟故障")
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: ms)
    r = _invoke(["--json", "run", lesson_dir.name, "--stages", "omni"])
    assert r.exit_code == 5, _why(r)
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    r = _invoke(["--json", "run", lesson_dir.name, "--stages", "omni,render"])
    assert r.exit_code == 0, _why(r)
    payload = json.loads(r.stdout)
    assert payload["errors"] == {} and "omni" in payload["stages_done"]


def test_run_local_rejects_positional_id(lesson_dir):  # noqa: F811
    r = _invoke(["run", lesson_dir.name, "--local", str(lesson_dir)])
    assert r.exit_code == 2


def test_batch_aborts_with_auth_error(lesson_dir, monkeypatch):  # noqa: F811
    from njupt_very_smart_class.auth import AuthError

    def boom(*a, **kw):
        raise AuthError("会话无效")

    monkeypatch.setattr(pipeline, "run_lesson", boom)
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())  # 不依赖本机 .env
    r = _invoke(["--json", "run", "--local", str(lesson_dir), "--stages", "render"])
    assert r.exit_code == 3, _why(r)  # 会话失效不是"部分完成"，agent 应去 login 而不是重跑
    payload = json.loads(r.stdout)
    assert payload["ok"] is False and payload["exit_code"] == 3 and "会话无效" in payload["results"][0]["error"]


def test_download_retry_exhaustion_maps_to_upstream(lesson_dir, monkeypatch):  # noqa: F811
    def boom(*a, **kw):
        try:
            raise requests.ConnectionError("refused")
        except requests.ConnectionError as cause:
            raise RuntimeError("下载失败（10 次重试）") from cause

    _single_mode(monkeypatch)
    monkeypatch.setattr(pipeline, "run_lesson", boom)
    r = _invoke(["--json", "run", lesson_dir.name, "--stages", "render"])
    assert r.exit_code == 4, _why(r)
    assert "网络错误" in json.loads(r.stdout)["error"]


def test_exit_codes(lesson_dir, monkeypatch):  # noqa: F811
    # 课时不存在 → 6
    r = _invoke(["--json", "status", "NOPE"])
    assert r.exit_code == 6 and json.loads(r.stdout)["ok"] is False
    # 未登录 → 3
    monkeypatch.setattr(client, "make_client", lambda: (_ for _ in ()).throw(client.AuthError("会话无效")))
    r = _invoke(["--json", "list", "--limit", "1"])
    assert r.exit_code == 3 and "会话无效" in json.loads(r.stdout)["error"]
    # 网络 / 上游错误 → 4（各命令一致）
    monkeypatch.setattr(client, "make_client", lambda: (_ for _ in ()).throw(requests.ConnectionError("refused")))
    for args in (["--json", "list", "--limit", "1"], ["--json", "semesters"], ["--json", "run", "X"]):
        r = _invoke(args)
        assert r.exit_code == 4, args
        assert "网络错误" in json.loads(r.stdout)["error"]
    # 用法错误 → 2
    r = _invoke(["run", "X", "--omni-input", "Bad"])
    assert r.exit_code == 2
    r = _invoke(["run"])
    assert r.exit_code == 2 and "筛选条件" in r.output


def test_login_non_interactive_needs_password(monkeypatch):
    import njupt_very_smart_class.cli_auth as ca

    monkeypatch.delenv("NJUPT_PASSWORD", raising=False)
    monkeypatch.setattr(ca.sys.stdin, "isatty", lambda: False, raising=False)
    r = _invoke(["login", "--username", "B00000000"])
    assert r.exit_code == 2 and "--password-stdin" in plain(r.output)


def test_local_runs_on_the_given_directory_not_on_data_dir(lesson_dir, tmp_path, monkeypatch):  # noqa: F811
    import shutil

    elsewhere = tmp_path / "elsewhere" / "课程" / lesson_dir.name  # 数据目录之外的一份拷贝
    stages = ("prepare", "transcribe", "summarize")
    pipeline.run_lesson(None, "ID42", stages=stages, models=fake_model_set(), log=lambda *_: None)
    shutil.copytree(
        lesson_dir,
        elsewhere,
        ignore=shutil.ignore_patterns("*.mp4"),  # 模拟 --prune-video 之后只剩 wav 的课时
    )
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    r = CliRunner().invoke(app, ["--json", "run", "--local", str(elsewhere.parent), "--stages", "render"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["results"][0]["summary"].replace("\\", "/").startswith(str(elsewhere).replace("\\", "/"))
    assert (elsewhere / "summary.md").exists() and not (lesson_dir / "summary.md").exists()


def test_local_render_needs_no_api_key(lesson_dir, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    real_models = pipeline.dashscope_model_set

    def no_models(**kw):  # 没有请求用模型的阶段，就不该去构造模型客户端
        raise AssertionError("不该构造模型客户端")

    monkeypatch.setattr(pipeline, "dashscope_model_set", no_models)
    monkeypatch.setattr(client, "make_client", lambda: object())
    stages = ("prepare", "transcribe", "vision", "summarize", "render")
    pipeline.run_lesson(None, "ID42", stages=stages, models=fake_model_set(), log=lambda *_: None)
    r = _invoke(["--json", "run", "--local", str(lesson_dir), "--stages", "render"])
    assert r.exit_code == 0, _why(r)

    monkeypatch.setattr(pipeline, "dashscope_model_set", real_models)
    r = _invoke(["--json", "run", "--local", str(lesson_dir), "--stages", "summarize"])
    assert r.exit_code == 1 and "DASHSCOPE_API_KEY" in json.loads(r.stdout)["error"]


def test_status_and_summary_json(lesson_dir, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    r = _invoke(["--json", "run", "--local", str(lesson_dir), "--stages", "prepare,transcribe,vision,summarize,render"])
    assert r.exit_code == 0, r.output
    r = _invoke(["--json", "status", lesson_dir.name])
    payload = json.loads(r.stdout)
    assert payload["ok"] and payload["verdict"] == "c_only" and "render" in payload["stages"]  # 没跑视频模式
    assert payload["products"][lesson_dir.name]["transcript_segments"] == 2
    r = _invoke(["--json", "summary", lesson_dir.name, "--print"])
    payload = json.loads(r.stdout)
    assert payload["fused"] is False and "## 章节时间轴" in payload["markdown"]


def test_doctor_json_with_mocks(monkeypatch, tmp_path):
    import njupt_very_smart_class.cli_auth as ca

    monkeypatch.setattr(ca.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ca, "auth_dashscope_key_present", lambda: True)
    monkeypatch.setattr(client, "make_client", lambda: object())
    monkeypatch.setattr(client, "verify", lambda sc: 10)
    monkeypatch.setattr(ca.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100 << 30, 50 << 30, 50 << 30))
    r = _invoke(["--json", "doctor"])
    payload = json.loads(r.stdout)
    assert r.exit_code == 0 and payload["ok"] is True
    assert [c["name"] for c in payload["checks"]] == ["ffmpeg", "api_key", "session", "disk", "models"]


def test_version():
    r = _invoke(["--version"])
    assert r.exit_code == 0 and r.output.startswith("vsc ")
