"""各模块共用的常量和小工具。"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

# 群里的三位 AI 成员，顺序就是默认发言顺序
MEMBER_ORDER = ["claude", "gpt", "gemini"]

NAMES = {
    "claude": "Claude",
    "gpt": "GPT",
    "gemini": "Gemini",
    "user": "群主",
    "system": "系统",
    "unassigned": "未指定",
}

_MEMBER_ALIASES = {
    "claude": "claude",
    "anthropic": "claude",
    "gpt": "gpt",
    "chatgpt": "gpt",
    "openai": "gpt",
    "codex": "gpt",
    "gemini": "gemini",
    "google": "gemini",
    "agy": "gemini",
    "antigravity": "gemini",
    "user": "user",
    "群主": "user",
    "用户": "user",
    "我": "user",
}


def now() -> float:
    return time.time()


def fmt_time(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def name_of(member: str) -> str:
    return NAMES.get(member, member)


def normalize_member(value: Any) -> Optional[str]:
    """把各种写法（Claude、ChatGPT、群主……）统一成成员编号。"""
    if not isinstance(value, str):
        return None
    key = value.strip().lstrip("@＠").strip().lower()
    return _MEMBER_ALIASES.get(key)


def atomic_write_json(path: Path, data: Any) -> None:
    """先写临时文件再替换，避免写到一半断电或崩溃导致文件损坏。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Windows 上目标文件正被别的程序（例如杀毒软件）打开时会短暂替换失败，稍等重试
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 文件损坏时保留一份备份，再用默认值继续，不让整个程序起不来
        try:
            os.replace(path, str(path) + ".broken")
        except OSError:
            pass
        return default


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……（后略）"


_GID_RE = re.compile(r"^g-[a-z0-9]{6,32}$")


def valid_gid(gid: str) -> bool:
    return bool(_GID_RE.match(gid or ""))
