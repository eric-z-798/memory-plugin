"""Persistent account-level pauses, independent of model and connection settings.

One instance is shared by the engine and HTTP handlers in a process. A pause
requires an explicit resume; this store does not infer quota reset times or
retry requests. Callers should pass a generic message, never raw CLI output or
account credentials.
"""

import copy
import math
import threading
from pathlib import Path

from .common import MEMBER_ORDER, atomic_write_json, now, read_json, CorruptJSON


_PAUSE_REASONS = ("quota", "rate_limit", "manual")


def _default_state():
    return {"paused": False, "reason": "", "message": "", "since": 0.0}


def _validate_member(member):
    if not isinstance(member, str) or member not in MEMBER_ORDER:
        raise ValueError("未知成员")


def _message(value):
    if not isinstance(value, str):
        raise ValueError("暂停说明必须是字符串")
    return value.strip()[:500]


class AvailabilityStore:
    def __init__(self, root_path):
        self.root = Path(root_path)
        self.path = self.root / "model-availability.json"
        self._lock = threading.RLock()
        self._states = {member: _default_state() for member in MEMBER_ORDER}
        try:
            saved = read_json(self.path, {}, strict=True)
            if not isinstance(saved, dict):
                raise CorruptJSON("暂停状态不是对象")
        except (CorruptJSON, OSError):
            self._states = {member: {"paused": True, "reason": "manual",
                "message": "暂停状态文件损坏或不可读；请核对备份后手动恢复。", "since": now()}
                for member in MEMBER_ORDER}
            # Persist fail-closed state across subsequent restarts.
            atomic_write_json(self.path, self._states)
            saved = {}
        if isinstance(saved, dict):
            for member in MEMBER_ORDER:
                entry = saved.get(member)
                if not isinstance(entry, dict) or entry.get("paused") is not True:
                    continue
                # Keep an existing pause even if its explanation is malformed.
                reason = entry.get("reason")
                if not isinstance(reason, str) or reason not in _PAUSE_REASONS:
                    reason = "manual"
                message = entry.get("message", "")
                since = entry.get("since", 0.0)
                if (isinstance(since, bool) or not isinstance(since, (int, float))
                        or not math.isfinite(since) or since < 0):
                    since = 0.0
                self._states[member] = {
                    "paused": True, "reason": reason,
                    "message": _message(message) if isinstance(message, str) else "",
                    "since": float(since),
                }

    def snapshot(self):
        """Return detached state for all three members."""
        with self._lock:
            return copy.deepcopy(self._states)

    def pause(self, member, reason, message=""):
        _validate_member(member)
        if not isinstance(reason, str) or reason not in _PAUSE_REASONS:
            raise ValueError("暂停原因只能是 quota、rate_limit 或 manual")
        message = _message(message)
        with self._lock:
            states = copy.deepcopy(self._states)
            previous = states[member]
            states[member] = {
                "paused": True, "reason": reason, "message": message,
                "since": previous["since"] if previous["paused"] else float(now()),
            }
            # Commit in-memory state only after the atomic write succeeds.
            atomic_write_json(self.path, states)
            self._states = states
            return dict(states[member])

    def resume(self, member):
        _validate_member(member)
        with self._lock:
            states = copy.deepcopy(self._states)
            states[member] = _default_state()
            atomic_write_json(self.path, states)
            self._states = states
            return dict(states[member])
