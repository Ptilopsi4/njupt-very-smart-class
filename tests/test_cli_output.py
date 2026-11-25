# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI 输出行为：命令级 --json、summary --print 原样输出、多段录制的课时级摘要、入口加载 .env。"""

import json
import shutil

from typer.testing import CliRunner

from njupt_very_smart_class import client, config, pipeline
from njupt_very_smart_class.cli import app
from tests.fakes import fake_model_set
from tests.test_pipeline import lesson_dir  # noqa: F401

runner = CliRunner()


def _why(result):
    return f"exit={result.exit_code} exc={result.exception!r}" + chr(10) + result.output[-600:]


def test_json_flag_works_after_every_command(lesson_dir, monkeypatch):  # noqa: F811
    r = runner.invoke(app, ["prepare", lesson_dir.name, "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["segments"], _why(r)
    monkeypatch.setattr(client, "make_client", lambda: (_ for _ in ()).throw(client.AuthError("会话无效")))
    for args in (["semesters", "--json"], ["fetch", lesson_dir.name, "--json"]):
        r = runner.invoke(app, args)
        assert r.exit_code == 3 and json.loads(r.stdout)["exit_code"] == 3, (args, _why(r))


def test_summary_print_is_verbatim(lesson_dir, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pipeline, "dashscope_model_set", lambda **kw: fake_model_set())
    r = runner.invoke(
        app, ["run", "--local", str(lesson_dir), "--stages", "prepare,transcribe,vision,summarize,render"]
    )
    assert r.exit_code == 0, _why(r)
    text = (lesson_dir / "summary.md").read_text(encoding="utf-8")
    r = runner.invoke(app, ["summary", lesson_dir.name, "--print"], env={"COLUMNS": "40"})
    assert r.exit_code == 0, _why(r)
    assert r.stdout == (text if text.endswith("\n") else text + "\n")  # rich 会按终端宽度折行，这里必须逐字节一致


def test_multi_segment_lesson_gets_merged_summary(lesson_dir):  # noqa: F811
    for name in ("Seg1", "Seg2"):
        seg = lesson_dir / name
        seg.mkdir()
        (seg / "summary.md").write_text(
            f"# 编译原理 2026-11-23 09:50–10:35\n\n## 章节时间轴\n\n### 00:10–05:00 第 {name[-1]} 段内容\n- 要点\n",
            encoding="utf-8",
        )
    (lesson_dir / "state.json").write_text(json.dumps({"stages": {"render": {}}}), encoding="utf-8")
    meta = json.loads((lesson_dir / "meta.json").read_text(encoding="utf-8"))
    out = pipeline.compose_lesson_summary(lesson_dir, meta)
    text = out.read_text(encoding="utf-8")
    assert "## 第 1 段（Seg1）" in text and "## 第 2 段（Seg2）" in text
    assert "### 章节时间轴" in text and "#### 00:10–05:00" in text  # 分段内标题整体降一级
    assert pipeline.lesson_done(lesson_dir, ("render",))
    r = runner.invoke(app, ["summary", lesson_dir.name, "--print"])
    assert r.exit_code == 0 and "第 1 段" in r.stdout and "第 2 段" in r.stdout, _why(r)
    out.unlink()
    r = runner.invoke(app, ["--json", "summary", lesson_dir.name])  # 旧课时只有分段摘要也能定位
    found = json.loads(r.stdout)["summary"]
    assert r.exit_code == 0 and found.replace("\\", "/").endswith("Seg1/summary.md"), _why(r)
    shutil.rmtree(lesson_dir / "Seg1")
    shutil.rmtree(lesson_dir / "Seg2")


def test_dotenv_is_loaded_for_every_command(monkeypatch, tmp_path):
    import njupt_very_smart_class.auth as auth
    import njupt_very_smart_class.cli_auth as ca

    (tmp_path / ".env").write_text("NJUPT_USERNAME=u\nNJUPT_PASSWORD=p\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("NJUPT_USERNAME", raising=False)
    monkeypatch.delenv("NJUPT_PASSWORD", raising=False)
    monkeypatch.setattr(ca.sys.stdin, "isatty", lambda: False, raising=False)

    def sso(session, username, password):
        raise auth.AuthError("模拟 SSO 失败")

    monkeypatch.setattr(auth, "login_with_password", sso)
    r = runner.invoke(app, ["login"])  # 没有 --username/--password-stdin：账号密码只能来自 .env
    assert r.exit_code == 3 and "SSO" in r.output, _why(r)  # 走到了 SSO，而不是用法错误 2
