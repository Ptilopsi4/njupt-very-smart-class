# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import pathlib

import pytest
import requests

from njupt_very_smart_class.bailian import BailianClient, BailianError, parse_json_object
from njupt_very_smart_class.config import ConfigError, load_dotenv


def test_parse_json_object_variants():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('好的，结果如下：\n{"a": [1, 2]}\n以上。') == {"a": [1, 2]}
    with pytest.raises(BailianError):
        parse_json_object("没有 JSON")
    with pytest.raises(BailianError):
        parse_json_object("[1, 2, 3]")


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers = {}

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def _ok(content, reasoning=None, prompt=10, completion=5):
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return _FakeResponse(
        200,
        {
            "model": "m",
            "choices": [{"message": msg}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        },
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("njupt_very_smart_class.bailian.time.sleep", lambda s: None)
    c = BailianClient(api_key="test-key")
    return c


def test_chat_body_and_usage(client):
    client.session = _FakeSession([_ok('{"ok": true}')])
    res = client.chat("m", [{"role": "user", "content": "hi"}], json_mode=True, thinking=False)
    body = client.session.requests[0]["json"]
    assert body["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False
    assert res.content == '{"ok": true}' and client.spent["m"] == 15


def test_chat_retries_on_429_then_succeeds(client):
    client.session = _FakeSession([_FakeResponse(429, {"error": "slow down"}), _ok("second")])
    res = client.chat("m", [{"role": "user", "content": "hi"}])
    assert res.content == "second" and len(client.session.requests) == 2


def test_chat_json_repairs_once(client):
    client.session = _FakeSession([_ok("not json at all"), _ok('{"fixed": 1}')])
    obj, res = client.chat_json("m", "give json")
    assert obj == {"fixed": 1}
    second = client.session.requests[1]["json"]["messages"]
    assert second[0]["role"] == "system" and second[1]["content"][0]["text"] == "give json"  # system 防护消息在最前
    assert second[2]["content"] == "not json at all" and second[3]["role"] == "user"


def test_chat_non_200_raises(client):
    client.session = _FakeSession([_FakeResponse(400, {"error": {"message": "bad"}})])
    with pytest.raises(BailianError, match="400"):
        client.chat("m", [])


def test_upload_temp_flow(client):
    class _Upload:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

    policy = {
        "data": {
            "policy": "p",
            "signature": "s",
            "upload_dir": "d/x",
            "upload_host": "https://oss.example",
            "oss_access_key_id": "ak",
            "x_oss_object_acl": "private",
            "x_oss_forbid_overwrite": "true",
            "max_file_size_mb": 1,
        }
    }

    class _S(_FakeSession):
        def get(self, url, headers=None, params=None, timeout=None):
            self.requests.append({"url": url, "params": params})
            return _FakeResponse(200, policy)

        def post(self, url, files=None, timeout=None, **kw):
            self.requests.append({"url": url, "files": list(files)})
            return _Upload()

    client.session = _S([])
    f = client.__class__  # noqa: F841  仅确认类型
    import pathlib

    path = pathlib.Path(__file__)
    url = client.upload_temp(path, "模型 中文")
    assert url.startswith("oss://d/x/") and "模型" not in url
    assert client.session.requests[0]["params"] == {"action": "getPolicy", "model": "模型 中文"}


def test_missing_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr("njupt_very_smart_class.config.PROJECT_ROOT", tmp_path)
    assert load_dotenv() == 0
    with pytest.raises(ConfigError):
        BailianClient()


def test_load_dotenv_does_not_override(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("DASHSCOPE_API_KEY=from-file\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "from-env")
    monkeypatch.delenv("OTHER", raising=False)
    import os

    assert load_dotenv(env) == 1
    assert os.environ["DASHSCOPE_API_KEY"] == "from-env" and os.environ["OTHER"] == "1"
    assert isinstance(requests.Session(), requests.Session)


def test_close_truncated_json():
    from njupt_very_smart_class.bailian import close_truncated_json

    t = '{\n "chapters": [\n  {"start": "00:00", "points": [\n   "a"\n    ,"b"\n    ,"c被切'
    assert json.loads(close_truncated_json(t)) == {"chapters": [{"start": "00:00", "points": ["a", "b"]}]}
    assert json.loads(close_truncated_json('{"a": [1, 2, {"b": "x')) == {"a": [1, 2]}  # 未闭合的元素被丢弃
    assert parse_json_object('前言\n```json\n{"k": [1,\n') == {"k": [1]}
    with pytest.raises(BailianError):
        close_truncated_json("no json here")


# ---------- 角色适配器 ----------
class _Upload:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None


def _policy_session(transcribe_no_words: bool = False):
    """按顺序响应：getPolicy → OSS 上传 → （ASR）提交任务 → 查询任务；（视频/文本）chat。"""
    policy = {
        "data": {
            "policy": "p",
            "signature": "s",
            "upload_dir": "d/x",
            "upload_host": "https://oss.example",
            "oss_access_key_id": "ak",
            "x_oss_object_acl": "private",
            "x_oss_forbid_overwrite": "true",
            "max_file_size_mb": 1024,
        }
    }

    class _S:
        def __init__(self):
            self.calls: list[dict] = []
            self.headers = {}

        def get(self, url, headers=None, params=None, timeout=None):
            self.calls.append({"m": "GET", "url": url, "params": params, "headers": headers})
            if "/uploads" in url:
                return _FakeResponse(200, policy)
            if "/tasks/" in url:
                out = (
                    {"task_status": "FAILED", "code": "ASR_RESPONSE_HAVE_NO_WORDS"}
                    if transcribe_no_words
                    else {
                        "task_status": "SUCCEEDED",
                        "results": [{"subtask_status": "SUCCEEDED", "transcription_url": "http://fake/t.json"}],
                    }
                )
                return _FakeResponse(200, {"output": out})
            raise AssertionError(url)

        def post(self, url, headers=None, json=None, timeout=None, files=None, **kw):
            self.calls.append({"m": "POST", "url": url, "json": json, "headers": headers, "files": bool(files)})
            if url == "https://oss.example":
                return _Upload()
            if "transcription" in url:
                return _FakeResponse(200, {"output": {"task_status": "PENDING", "task_id": "T1"}})
            if "chat/completions" in url:
                content = json["messages"][-1]["content"]
                has_video = any(p.get("type") == "video_url" for p in content)
                return _ok('{"chapters": []}' if has_video else '{"ok": true}')
            raise AssertionError(url)

    return _S()


RAW_T = {
    "transcripts": [
        {
            "channel_id": 0,
            "content_duration_in_milliseconds": 9000,
            "text": "全文。",
            "sentences": [{"begin_time": 0, "end_time": 4500, "text": "第一句。"}],
        }
    ],
    "usage": {"input_tokens": 1, "output_tokens": 2},
}


def _client_with(session):
    c = BailianClient(api_key="k")
    c.session = session
    return c


def test_dashscope_adapters_conform_to_protocols():
    from njupt_very_smart_class import bailian, models

    ms = bailian.dashscope_model_set(asr="a", vision="v", text="t", omni="o", client=BailianClient(api_key="k"))
    assert isinstance(ms.asr, models.AsrModel) and isinstance(ms.vision, models.VisionModel)
    assert isinstance(ms.text, models.TextModel) and isinstance(ms.video, models.VideoModel)
    assert ms.asr.name == "a" and ms.video.name == "o"
    ms_none = bailian.dashscope_model_set(asr="a", vision="v", text="t", omni=None, client=BailianClient(api_key="k"))
    assert isinstance(ms_none.video, models.NullVideoModel)
    with pytest.raises(models.ModelUnavailable):
        ms_none.video.video_json(pathlib.Path("x.mp4"), "p", fps=0.2)


def test_vision_and_text_adapters_request_bodies():
    from njupt_very_smart_class import bailian

    s = _policy_session()
    c = _client_with(s)
    out = bailian.DashScopeVisionModel(c, "qwen-v").images_json("描述", [b"\xff\xd8img"], thinking=False)
    body = s.calls[-1]["json"]
    assert body["model"] == "qwen-v" and body["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert out.data == {"ok": True} and out.model == "m" and out.truncated is False
    bailian.DashScopeTextModel(c, "qwen-t").complete_json("总结", thinking=True, max_tokens=100, timeout=5)
    body = s.calls[-1]["json"]
    assert body["enable_thinking"] is True and body["max_tokens"] == 100
    assert c.spent == {"qwen-v": 15, "qwen-t": 15}  # 按请求模型名计数，各一次


def test_video_adapter_uploads_then_chats_with_video_url(monkeypatch, tmp_path):
    from njupt_very_smart_class import bailian

    s = _policy_session()
    c = _client_with(s)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    out = bailian.DashScopeVideoModel(c, "omni").video_json(video, "分析", fps=0.2, timeout=7)
    kinds = [(x["m"], x["url"].split("/")[2] if "//" in x["url"] else x["url"]) for x in s.calls]
    assert kinds[0][0] == "GET" and kinds[0][1] == "dashscope.aliyuncs.com"
    upload = s.calls[1]
    assert upload["url"] == "https://oss.example" and upload["files"] is True
    chat = s.calls[-1]
    assert chat["headers"]["X-DashScope-OssResourceResolve"] == "enable"
    part = chat["json"]["messages"][1]["content"][1]
    assert part["type"] == "video_url" and part["video_url"]["url"].startswith("oss://d/x/v.mp4")
    assert part["video_url"]["fps"] == 0.2
    assert out.data == {"chapters": []}


def test_asr_adapter_full_flow_and_no_words(monkeypatch, tmp_path):
    from njupt_very_smart_class import bailian

    monkeypatch.setattr(
        bailian.requests,
        "get",
        lambda url, timeout=None: type(
            "R", (), {"status_code": 200, "raise_for_status": lambda self: None, "json": lambda self: RAW_T}
        )(),
    )
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    s = _policy_session()
    c = _client_with(s)
    out = bailian.DashScopeAsrModel(c, "asr-m").transcribe(audio)
    assert (
        out.segments == [{"begin": 0, "end": 4500, "text": "第一句。"}]
        and out.text == "全文。"
        and out.duration_ms == 9000
    )
    assert out.model == "asr-m" and out.usage == {"input_tokens": 1, "output_tokens": 2} and not out.no_speech
    submit = next(x for x in s.calls if x["m"] == "POST" and "transcription" in x["url"])
    assert submit["headers"]["X-DashScope-Async"] == "enable"
    assert submit["json"]["input"]["file_urls"] == ["oss://d/x/audio.wav"]
    s2 = _policy_session(transcribe_no_words=True)
    out2 = bailian.DashScopeAsrModel(_client_with(s2), "asr-m").transcribe(audio)
    assert out2.no_speech and out2.reason == "asr_no_words" and out2.segments == []


def test_model_set_spent_aggregates():
    from njupt_very_smart_class import bailian

    c = _client_with(_policy_session())
    ms = bailian.dashscope_model_set(asr="a", vision="v", text="t", omni=None, client=c)
    bailian.DashScopeVisionModel(c, "v").images_json("p", [b"i"])
    bailian.DashScopeTextModel(c, "t").complete_json("p")
    assert ms.spent() == {"v": 15, "t": 15}  # 共享客户端的同一份计数只算一次
