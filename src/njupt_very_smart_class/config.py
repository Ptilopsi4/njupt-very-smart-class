"""运行配置：密钥与模型名。密钥只从环境变量或项目根 .env 读取，绝不落日志。

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import os
from pathlib import Path

from dotenv import dotenv_values

from .paths import PROJECT_ROOT

MODELS = {
    "asr": "qwen-audio-3.1-asr-flash-filetrans",
    "vision": "qwen3.7-plus",
    "text": "qwen3.7-plus",
    "omni": "qwen3.8-omni-flash",
}

COMPAT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
API_BASE = "https://dashscope.aliyuncs.com/api/v1"


class ConfigError(RuntimeError):
    pass


def load_dotenv(path: Path | None = None) -> int:
    """把 .env 的键补进 os.environ（不覆盖已有环境变量）。返回写入的键数。"""
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return 0
    written = 0
    for key, value in dotenv_values(path).items():
        if value is not None and key not in os.environ:
            os.environ[key] = value
            written += 1
    return written


def model_overrides() -> dict[str, str]:
    """开发测试配置：.env 里的 VSC_MODEL_ASR / VSC_MODEL_VISION / VSC_MODEL_TEXT / VSC_MODEL_OMNI 覆盖默认模型名。

    只影响本次运行，不改变代码里的默认值；CLI 的 --model-* 选项优先级更高。
    """
    load_dotenv()
    out = {}
    for role in MODELS:
        value = os.environ.get(f"VSC_MODEL_{role.upper()}", "").strip()
        if value:
            out[role] = value
    return out


def effective_models() -> dict[str, str]:
    return {**MODELS, **model_overrides()}


def dashscope_api_key() -> str:
    load_dotenv()
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise ConfigError("缺少 DASHSCOPE_API_KEY：放到环境变量或项目根 .env（已 gitignore）")
    return key
