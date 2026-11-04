"""数据存储。

每个群一个文件夹，全部是普通的 JSON / JSONL 文件，方便备份、查看和迁移：

    data/
      settings.json              全局设置（三家命令行的路径、模型、超时等）
      groups/<群编号>/
        group.json               群设置
        board.json               共享看板（共同记忆）
        messages.jsonl           聊天记录，一行一条
        workspace/               成员运行时的工作目录
      archive/                   删除的群挪到这里，不真正删除
"""

from __future__ import annotations

import copy
import json
import os
import math
import tempfile
import shutil
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from . import board as board_mod
from .common import MEMBER_ORDER, atomic_write_json, name_of, now, read_json, valid_gid
from .memory import MemoryStore
from .routing import default_routing, clean_routing, resolve_route
from .network import DEFAULT_PROXY, clean_proxy

DEFAULT_SETTINGS = {
    "members": {
        "claude": {"command": "", "model": "", "extra_args": "", "timeout": 600, "web": True},
        "gpt": {"command": "", "model": "", "extra_args": "", "timeout": 600, "web": False},
        "gemini": {"command": "", "model": "", "extra_args": "", "timeout": 600, "web": True},
    },
    # 每次发言时原样附上的最近消息条数；更早的内容由书记员压缩成摘要
    "recent_window": 16,
    # 窗口外积累到这么多条未摘要的消息，就请书记员更新一次摘要
    "summary_batch": 8,
    # 调用时忽略 ANTHROPIC_API_KEY 等环境变量，保证只用会员额度
    "membership_only": True,
    "proxy": copy.deepcopy(DEFAULT_PROXY),
}
for _member in MEMBER_ORDER:
    DEFAULT_SETTINGS["members"][_member]["routing"] = default_routing(_member)

# JSON message IDs are also consumed as JavaScript Numbers by the native UI.
# Larger integers cannot be used as exact source/after/retry identifiers there.
MAX_MESSAGE_SEQ = 2 ** 53 - 1
_MAX_SEQ_BYTES = str(MAX_MESSAGE_SEQ).encode("ascii")


class StoreError(Exception):
    pass


def _next_message_seq(high: int) -> int:
    if high >= MAX_MESSAGE_SEQ:
        raise StoreError("消息序号已耗尽；保留原数据，请创建新群，不能复用或重新编号")
    return high + 1


def _unique_object(pairs):
    """Reject ambiguous JSON objects instead of silently using a duplicate's last value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


class _IntegerToken(str):
    """A JSON integer token, without converting untrusted digits to a Python int."""


def _recover_top_level_sequences(raw: bytes):
    """Read complete top-level members of a damaged object's trustworthy prefix.

    raw_decode consumes each whole JSON value (including nested containers and
    escaped strings). Stop at the first structural error; never search through
    malformed content for something that merely looks like a seq field.
    Duplicate top-level seq keys reserve every usable candidate, not first/last.
    Replacement decoding cannot invent ASCII JSON delimiters in bad UTF-8 bytes;
    the original bytes are still quarantined, never used as recovered content.
    """
    text = raw.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder(parse_int=_IntegerToken)
    candidates, ignored = [], []

    def space(pos):
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        return pos

    pos = space(0)
    if pos >= len(text) or text[pos] != "{":
        return candidates, ignored
    pos = space(pos + 1)
    while pos < len(text) and text[pos] != "}":
        try:
            if text[pos] != '"':
                break  # Object member names must be JSON strings, never numeric tokens.
            key, end = decoder.raw_decode(text, pos)
            if not isinstance(key, str):
                break
            pos = space(end)
            if pos >= len(text) or text[pos] != ":":
                break
            value, end = decoder.raw_decode(text, space(pos + 1))
            pos = space(end)
            # raw_decode can parse the numeric prefix of `123xyz` or `123.`.
            # Only a complete member value with a valid boundary is trustworthy.
            if pos < len(text) and text[pos] not in ",}":
                break
            if key == "seq" and isinstance(value, _IntegerToken) and not value.startswith("-") and value != "0":
                digits = value.encode("ascii")
                if (len(digits) > len(_MAX_SEQ_BYTES)
                        or (len(digits) == len(_MAX_SEQ_BYTES) and digits > _MAX_SEQ_BYTES)):
                    ignored.append({"digits": len(digits), "reason": "outside_exact_sequence_range"})
                else:
                    candidates.append(int(value))
            if pos >= len(text) or text[pos] == "}":
                break
            pos = space(pos + 1)
        except (ValueError, RecursionError):
            break
    return candidates, ignored


class Store:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        self.groups_dir = self.root / "groups"
        self.archive_dir = self.root / "archive"
        self.groups_dir.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._settings_lock = threading.RLock()
        self._messages: Dict[str, List[dict]] = {}
        self.memory = MemoryStore(self.root)

    # ---------- 锁 ----------
    def lock(self, gid: str) -> threading.RLock:
        with self._guard:
            if gid not in self._locks:
                self._locks[gid] = threading.RLock()
            return self._locks[gid]

    # ---------- 全局设置 ----------
    def load_settings(self) -> dict:
        with self._settings_lock:
            data = read_json(self.root / "settings.json", {})
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        for key, value in data.items():
            if key == "members" and isinstance(value, dict):
                for mid, conf in value.items():
                    if mid in merged["members"] and isinstance(conf, dict):
                        merged["members"][mid].update(conf)
            else:
                merged[key] = value
        return merged

    def save_settings(self, settings: dict) -> dict:
        with self._settings_lock:
            return self._save_settings(settings)

    def _save_settings(self, settings: dict) -> dict:
        clean = self.load_settings()
        if "proxy" in settings:
            clean["proxy"] = clean_proxy(settings["proxy"])
        members = settings.get("members", {})
        for mid in MEMBER_ORDER:
            conf = members.get(mid, {})
            if not isinstance(conf, dict):
                continue
            target = clean["members"][mid]
            if "routing" in conf:
                target["routing"] = clean_routing(mid, conf["routing"])
            for field in ("command", "model", "extra_args"):
                if field in conf:
                    target[field] = str(conf[field] or "").strip()
            if "timeout" in conf:
                try:
                    target["timeout"] = max(30, min(3600, int(conf["timeout"])))
                except (TypeError, ValueError):
                    pass
            if "web" in conf:
                target["web"] = bool(conf["web"])
        for field, low, high in (("recent_window", 4, 60), ("summary_batch", 2, 40)):
            if field in settings:
                try:
                    clean[field] = max(low, min(high, int(settings[field])))
                except (TypeError, ValueError):
                    pass
        if "membership_only" in settings:
            clean["membership_only"] = bool(settings["membership_only"])
        for mid in MEMBER_ORDER:
            resolve_route(mid, clean, {}, "reply", "")
        atomic_write_json(self.root / "settings.json", clean)
        return clean

    # ---------- 群 ----------
    def group_dir(self, gid: str) -> Path:
        if not valid_gid(gid):
            raise StoreError("群编号不合法")
        return self.groups_dir / gid

    def exists(self, gid: str) -> bool:
        return valid_gid(gid) and (self.groups_dir / gid / "group.json").exists()

    def _existing_dir(self, gid: str) -> Path:
        """写入前确认群还在，避免群被删除后后台线程又把文件夹建回来。"""
        gdir = self.group_dir(gid)
        if not (gdir / "group.json").exists():
            raise StoreError("找不到这个群")
        return gdir

    def workspace(self, gid: str) -> Path:
        path = self._existing_dir(gid) / "workspace"
        path.mkdir(exist_ok=True)
        return path

    def create_group(self, name: str, goal: str = "", members: Optional[List[str]] = None,
                     mode: str = "sequential", project_id: Optional[str] = None) -> dict:
        if len(goal.strip()) > 2000:
            raise ValueError("群目标最多 2000 字")
        gid = "g-" + uuid.uuid4().hex[:10]
        if project_id and project_id not in {p["id"] for p in self.memory.projects()}:
            raise ValueError("找不到所选项目")
        members = [m for m in (members or MEMBER_ORDER) if m in MEMBER_ORDER] or list(MEMBER_ORDER)
        group = {
            "id": gid,
            "project_id": project_id or gid,
            "name": (name or "").strip()[:60] or "新的群聊",
            "created": now(),
            "updated": now(),
            "members": members,
            "mode": mode if mode in ("sequential", "parallel") else "sequential",
            "parallel_review": True,
            "secretary": "rotate",
            "reply_length": "medium",
            "language": "简体中文",
            # 一轮结束后还有人没表态、或有反对意见时，自动再续几轮（0 表示不自动续）
            "auto_rounds": 2,
            "round_counter": 0,
            "secretary_index": 0,
            "model_overrides": {},
        }
        self.memory.ensure_project(group["project_id"], group["name"])
        gdir = self.groups_dir / gid
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / "workspace").mkdir(exist_ok=True)
        atomic_write_json(gdir / "group.json", group)
        board = board_mod.new_board(goal.strip()[:2000])
        board_mod.log(board, "user", "创建了群聊")
        atomic_write_json(gdir / "board.json", board)
        (gdir / "messages.jsonl").touch()
        self._messages[gid] = []
        return group

    def load_group(self, gid: str) -> dict:
        with self.lock(gid):
            group = read_json(self.group_dir(gid) / "group.json", None, strict=True)
        if not group:
            raise StoreError("找不到这个群")
        group.setdefault("project_id", gid)
        group.setdefault("model_overrides", {})
        self.memory.ensure_project(group["project_id"], group.get("name", "项目"))
        return group

    def save_group(self, group: dict) -> None:
        with self.lock(group["id"]):
            gdir = self._existing_dir(group["id"])
            group["updated"] = now()
            atomic_write_json(gdir / "group.json", group)

    def list_groups(self) -> List[dict]:
        result = []
        for gdir in self.groups_dir.iterdir():
            if not gdir.is_dir() or not valid_gid(gdir.name):
                continue
            try:
                group = read_json(gdir / "group.json", None, strict=True)
                if not isinstance(group, dict) or not group:
                    raise ValueError("群设置缺失或损坏")
                msgs = self.messages(gdir.name)
                last = next((m for m in reversed(msgs) if m.get("kind") in ("chat", "user")), None)
                result.append({
                    "id": group["id"],
                    "project_id": group.get("project_id", group["id"]),
                    "name": group["name"],
                    "members": group.get("members", []),
                    "updated": max(group.get("updated", 0), last["ts"] if last else 0),
                    "preview": ((name_of(last.get("author", "")) + "：" + last.get("text", "")) if last else "")[:80],
                })
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                result.append({"id": gdir.name, "project_id": gdir.name, "name": "群数据需要恢复",
                               "members": [], "updated": 0, "preview": str(exc), "error": str(exc)})
        result.sort(key=lambda g: g["updated"], reverse=True)
        return result

    def archive_group(self, gid: str) -> None:
        with self.lock(gid):
            src = self._existing_dir(gid)
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            target = self.archive_dir / gid
            if target.exists():
                target = self.archive_dir / ("%s-%d" % (gid, int(now())))
            shutil.move(str(src), str(target))
            self._messages.pop(gid, None)

    # ---------- 看板 ----------
    def load_board(self, gid: str) -> dict:
        with self.lock(gid):
            board = read_json(self.group_dir(gid) / "board.json", None, strict=True)
        if board is None:
            board = board_mod.new_board()
        return board_mod.upgrade(board)

    def save_board(self, gid: str, board: dict) -> None:
        with self.lock(gid):
            gdir = self._existing_dir(gid)
            # rev 每保存一次加 1，界面据此判断看板有没有变化
            board["rev"] = int(board.get("rev", 0)) + 1
            atomic_write_json(gdir / "board.json", board)

    # ---------- 消息 ----------
    def messages(self, gid: str) -> List[dict]:
        with self.lock(gid):
            if gid not in self._messages:
                msgs, bad, high = [], [], 0
                ignored_sequences = []
                quarantined_records = []
                path = self.group_dir(gid) / "messages.jsonl"
                raw = path.read_bytes() if path.exists() else b""
                for line_number, line in enumerate(raw.splitlines(keepends=True), 1):
                    if not line.strip():
                        continue
                    try:
                        # Valid records are interpreted ONLY by their decoded top-level seq.
                        entry = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
                        if not isinstance(entry, dict) or type(entry.get("seq")) is not int or entry["seq"] <= 0:
                            raise ValueError("invalid seq")
                        if entry["seq"] > MAX_MESSAGE_SEQ:
                            raise ValueError("sequence outside exact range")
                        high = max(high, entry["seq"])
                        if msgs and entry["seq"] <= msgs[-1]["seq"]:
                            raise ValueError("duplicate or unordered seq")
                        if (not isinstance(entry.get("text", ""), str)
                                or not isinstance(entry.get("author", ""), str)
                                or not isinstance(entry.get("ts", 0), (int, float))
                                or not math.isfinite(entry.get("ts", 0))):
                            raise ValueError("invalid message fields")
                        entry.setdefault("ts", 0)
                        msgs.append(entry)
                    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                        candidates, ignored = _recover_top_level_sequences(line)
                        high = max([high] + candidates)
                        ignored_sequences.extend(dict(item, line=line_number) for item in ignored)
                        quarantined_records.append({"line": line_number, "reason": str(exc)[:200],
                                                    "reserved_sequences": candidates})
                        bad.append(line)
                if bad:
                    token = "%d-%s" % (int(now() * 1000000), uuid.uuid4().hex[:8])
                    backup = path.with_name("messages.backup-" + token + ".jsonl")
                    quarantine = path.with_name("messages.corrupt-" + token + ".jsonl")
                    for target, data in ((backup, raw), (quarantine, b"".join(bad))):
                        with open(target, "xb") as f:
                            f.write(data)
                            f.flush()
                            os.fsync(f.fileno())
                    msgs.append({"seq": _next_message_seq(high), "id": "m-" + uuid.uuid4().hex[:10], "ts": now(),
                                 "kind": "system", "author": "system", "subtle": True,
                                 "text": ("恢复聊天记录：已隔离 %d 行到 %s；原文件备份 %s。" % (len(bad), quarantine.name, backup.name)
                                          + ("未采用 %d 个超出精确正整数范围的畸形序号；行号、位数和原因见 recovery.ignored_sequences，原字节保留在隔离文件。" % len(ignored_sequences)
                                             if ignored_sequences else "")),
                                 "recovery": {"ignored_sequences": ignored_sequences,
                                              "quarantined_records": quarantined_records,
                                              "max_exact_sequence": MAX_MESSAGE_SEQ}})
                    # Original file remains intact until the fully flushed replacement is ready.
                    fd, temp = tempfile.mkstemp(prefix="messages-recovery-", dir=str(path.parent))
                    try:
                        with os.fdopen(fd, "wb") as f:
                            for entry in msgs:
                                f.write((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(temp, path)
                    finally:
                        if os.path.exists(temp):
                            os.unlink(temp)
                self._messages[gid] = msgs
            return list(self._messages[gid])

    def append_message(self, gid: str, msg: dict) -> dict:
        with self.lock(gid):
            msgs = self.messages(gid)
            msg = dict(msg)
            msg["seq"] = _next_message_seq(max((m["seq"] for m in msgs), default=0))
            msg.setdefault("id", "m-" + uuid.uuid4().hex[:10])
            msg.setdefault("ts", now())
            encoded = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            with open(self._existing_dir(gid) / "messages.jsonl", "ab+") as f:
                f.seek(0, os.SEEK_END)
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
            self._messages[gid].append(msg)
            return msg
