"""测试辅助：CLI 输出断言用。"""

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(text: str) -> str:
    """去掉 ANSI 转义与所有空白：rich 在不同平台/宽度下换行位置不同，断言不该依赖排版。"""
    return re.sub(r"\s+", "", _ANSI.sub("", text))
