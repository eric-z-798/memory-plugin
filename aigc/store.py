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

import hashlib
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
from .common import preserve_bytes
from .common import MEMBER_ORDER, atomic_write_json, name_of, now, read_json, valid_gid
from .memory import MemoryStore
from .routing import default_routing, clean_routing, resolve_route
from .network import DEFAULT_PROXY, clean_proxy

DEFAULT_SETTINGS = {
    "members": {
        "claude": {"command": "", "model": "", "extra_args": "", "timeout": 600, "web": True},
        "gpt": {"command": "", "model": "", "extra_args": "", "timeout": 600, "web": True},
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


class MessageAppendError(OSError):
    """An append failed; readback describes visibility, never durability."""
    def __init__(self, original, message, complete, read_error=None):
        self.original = original
        self.message = message
        self.complete = complete
        self.read_error = read_error
        super().__init__("消息写入异常（耐久性未知）：%s%s" %
                         (original, "; 读回失败：%s" % read_error if read_error else ""))


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
            path = self.root / "settings.json"
            data = read_json(path, {})
            merged = copy.deepcopy(DEFAULT_SETTINGS)
            for key, value in data.items():
                if key == "members" and isinstance(value, dict):
                    for mid, conf in value.items():
                        if mid in merged["members"] and isinstance(conf, dict):
                            merged["members"][mid].update(conf)
                else:
                    merged[key] = value
            if "settings_schema" not in data:
                # Old saves wrote defaults too: false has UNKNOWN provenance.
                # Apply the approved open-network policy once, disclose the
                # uncertainty, and never reinterpret a subsequent saved choice.
                changed = [mid for mid in MEMBER_ORDER if merged["members"][mid].get("web") is False]
                for mid in changed:
                    merged["members"][mid]["web"] = True
                merged["settings_schema"] = 1
                merged["web_explicit"] = []
                if changed:
                    merged["web_migration"] = {"source": "legacy_unknown", "members": changed, "acknowledged": False}
                if path.exists() and changed:
                    atomic_write_json(path, merged)
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
                clean["web_explicit"] = sorted(set(clean.get("web_explicit", [])) | {mid})
        for field, low, high in (("recent_window", 4, 60), ("summary_batch", 2, 40)):
            if field in settings:
                try:
                    clean[field] = max(low, min(high, int(settings[field])))
                except (TypeError, ValueError):
                    pass
        if "membership_only" in settings:
            clean["membership_only"] = bool(settings["membership_only"])
        if settings.get("acknowledge_web_migration") is True and clean.get("web_migration"):
            clean["web_migration"]["acknowledged"] = True
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

    def member_context(self, gid: str) -> dict:
        """Successful boundary and explicitly deferred full-text start per member."""
        raw = self.load_group(gid).get("member_context", {})
        result = {}
        high = max((m["seq"] for m in self.messages(gid)), default=0) if raw else 0
        for member in ("claude", "gpt", "gemini"):
            entry = raw.get(member, {}) if isinstance(raw, dict) else {}
            if not isinstance(entry, dict):
                continue
            values = {k: v for k, v in entry.items() if k in ("seen", "unread_from")
                      and type(v) is int and 0 < v <= min(MAX_MESSAGE_SEQ, high)}
            if values:
                result[member] = values
        return result

    def begin_member_context(self, gid: str, member: str, trigger: int) -> dict:
        # A first-request anchor is NOT a successful acknowledgement.
        with self.lock(gid):
            progress = self.member_context(gid)
            entry = progress.setdefault(member, {})
            if not entry and trigger > 0:
                entry["unread_from"] = trigger
                group = self.load_group(gid)
                group["member_context"] = progress
                self.save_group(group)
            return dict(entry)

    def complete_member_context(self, gid: str, member: str, seen: int, deferred: Optional[int]) -> dict:
        with self.lock(gid):
            progress = self.member_context(gid)
            entry = progress.setdefault(member, {})
            entry["seen"] = max(entry.get("seen", 0), seen)
            entry.pop("unread_from", None)
            if deferred is not None:
                entry["unread_from"] = deferred
            group = self.load_group(gid)
            group["member_context"] = progress
            self.save_group(group)
            return dict(entry)

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
            except (StoreError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
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
                    except (ValueError, UnicodeDecodeError, RecursionError, OverflowError) as exc:
                        candidates, ignored = _recover_top_level_sequences(line)
                        high = max([high] + candidates)
                        ignored_sequences.extend(dict(item, line=line_number) for item in ignored)
                        quarantined_records.append({"line": line_number, "reason": str(exc)[:200],
                                                    "reserved_sequences": candidates})
                        bad.append(line)
                if bad:
                    token = hashlib.sha256(raw).hexdigest()
                    backup = path.with_name("messages.backup-" + token + ".jsonl")
                    quarantine = path.with_name("messages.corrupt-" + token + ".jsonl")
                    for target, data in ((backup, raw), (quarantine, b"".join(bad))):
                        preserve_bytes(target, data)
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

    def append_user_message(self, gid: str, text: str, members: List[str]) -> dict:
        """Persist first-request anchors before accepting the user message.

        A failed append may leave a future anchor, never a successful seen value.
        member_context ignores such anchors until a message with that seq exists.
        """
        with self.lock(gid):
            rows = self.messages(gid)
            trigger = _next_message_seq(max((m["seq"] for m in rows), default=0))
            progress = self.member_context(gid)
            changed = False
            for member in members:
                if not progress.get(member):
                    progress[member] = {"unread_from": trigger}
                    changed = True
            if changed:
                group = self.load_group(gid)
                group["member_context"] = progress
                self.save_group(group)
            return self.append_message(gid, {"kind": "user", "author": "user", "text": text})

    def append_message(self, gid: str, msg: dict) -> dict:
        with self.lock(gid):
            msgs = self.messages(gid)
            msg = {"seq": _next_message_seq(max((m["seq"] for m in msgs), default=0)),
                   "id": msg.get("id", "m-" + uuid.uuid4().hex[:10]),
                   **{k: v for k, v in msg.items() if k not in ("seq", "id")}}
            msg.setdefault("ts", now())
            encoded = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                with open(self._existing_dir(gid) / "messages.jsonl", "ab+") as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell():
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":
                            f.write(b"\n")
                    if f.write(encoded) != len(encoded):
                        raise OSError("消息写入不完整")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                self._messages.pop(gid, None)
                # Read only: do not invoke recovery or retry fsync to disguise the
                # original failure. Match the entire record including id and seq.
                complete, read_error = None, None
                try:
                    with open(self._existing_dir(gid) / "messages.jsonl", "rb") as check:
                        complete = any(line.rstrip(b"\n") == encoded[:-1] for line in check)
                except OSError as read_exc:
                    read_error = read_exc
                raise MessageAppendError(exc, msg, complete, read_error) from exc
            self._messages[gid].append(msg)
            return msg
