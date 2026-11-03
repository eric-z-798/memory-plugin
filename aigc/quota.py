"""Read-only official CLI quota queries, with no model generation fallback.

Codex app-server's account endpoints expose allowance windows. Other adapters
remain explicit unsupported results until a generation-free interface is wired.
The percentages are observations, never a promise that the next call will work.
"""

from __future__ import annotations

import copy
import json
import math
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from typing import Optional

from .members import find_command, member_env


SOURCE = "Codex app-server account/rateLimits/read"
MAX_FRAME_BYTES = 256 * 1024
LIMIT_REASONS = {
    "rate_limit_reached", "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted", "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
}


class _QueryError(Exception):
    def __init__(self, message, unsupported=False):
        super().__init__(message)
        self.unsupported = unsupported


def _result(status, source, message, **fields):
    result = {"status": status, "checked_at": time.time(), "source": source,
              "windows": [], "message": message}
    result.update(fields)
    return result


def _number(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value < minimum or (maximum is not None and value > maximum):
        return None
    return value


def _label(value, default=""):
    if not isinstance(value, str):
        return default
    value = value.strip()
    if not value or len(value) > 120 or any(not c.isprintable() for c in value) or "<" in value or ">" in value:
        return default
    return value


def _normalize(payload):
    if not isinstance(payload, dict):
        raise _QueryError("额度接口返回了无法识别的结果，请升级 Codex 后重试")
    windows = []
    reasons = []
    spend_control_reached = False
    buckets = []
    seen = set()
    keyed = payload.get("rateLimitsByLimitId")
    if isinstance(keyed, dict):
        for key, value in list(keyed.items())[:32]:
            if isinstance(value, dict):
                buckets.append((key, value))
    primary = payload.get("rateLimits")
    if isinstance(primary, dict):
        buckets.append((primary.get("limitId") or "codex", primary))
    for fallback_id, bucket in buckets:
        limit_id = _label(bucket.get("limitId"), _label(fallback_id, "unknown"))
        if limit_id in seen:
            continue
        seen.add(limit_id)
        bucket_name = _label(bucket.get("limitName"), limit_id)
        reason = bucket.get("rateLimitReachedType")
        if isinstance(reason, str) and reason in LIMIT_REASONS and reason not in reasons:
            reasons.append(reason)
        if bucket.get("spendControlReached") is True:
            spend_control_reached = True
        for field, title in (("primary", "主要窗口"), ("secondary", "次要窗口")):
            value = bucket.get(field)
            if not isinstance(value, dict):
                continue
            used = _number(value.get("usedPercent"), maximum=100)
            if used is None:
                continue
            windows.append({
                "limit_id": limit_id,
                "label": "%s · %s" % (bucket_name, title),
                "window": field,
                "used_percent": used,
                "remaining_percent": round(100 - used, 6),
                "resets_at": _number(value.get("resetsAt")),
                "window_minutes": _number(value.get("windowDurationMins")),
            })
    allowed = payload.get("ordinaryUsageAllowed")
    result = _result("available", SOURCE,
                     "官方额度快照；不同额度桶分别显示，不代表后续请求一定可用" if windows
                     else "查询成功，但官方本次未返回可显示的百分比窗口；不能据此推断剩余额度",
                     windows=windows,
                     ordinary_usage_allowed=allowed if isinstance(allowed, bool) else None,
                     limit_reached_types=reasons,
                     spend_control_reached=spend_control_reached)
    if isinstance(keyed, dict) and len(keyed) > 32:
        result["truncated"] = True
    return result


def _stop_process(proc, deadline):
    """Close the read-only session, then terminate its process group if needed."""
    try:
        proc.stdin.close()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        proc.wait(timeout=max(0.01, min(0.10, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=max(0.05, min(0.20, deadline - time.monotonic())),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=max(0.01, min(0.10, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except (AttributeError, OSError, ValueError):
            pass


def _codex_query(cmd, env, timeout):
    deadline = time.monotonic() + timeout
    io_deadline = deadline - min(0.4, timeout / 4)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    events = queue.Queue(maxsize=64)
    overflow = threading.Event()
    with tempfile.TemporaryDirectory(prefix="aigc-quota-") as cwd:
        try:
            proc = subprocess.Popen([cmd, "app-server", "--listen", "stdio://"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=cwd, env=env, bufsize=0, **kwargs)
        except OSError:
            raise _QueryError("无法启动 Codex 额度查询，请检查命令路径和安装")

        def publish(value):
            try:
                events.put(value, timeout=0.02)
            except queue.Full:
                overflow.set()

        def read_stdout():
            try:
                while True:
                    line = proc.stdout.readline(MAX_FRAME_BYTES + 1)
                    if not line:
                        publish(None)
                        return
                    if len(line) > MAX_FRAME_BYTES:
                        overflow.set()
                        return
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(message, dict):
                        publish(message)
            except (OSError, ValueError):
                publish(None)

        def drain_stderr():
            # Diagnostics can contain account details. Never retain or expose it.
            try:
                while proc.stderr.read(65536):
                    pass
            except (OSError, ValueError):
                pass

        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=drain_stderr, daemon=True)]
        for reader in readers:
            reader.start()

        def send(message):
            if time.monotonic() >= io_deadline:
                raise _QueryError("额度查询超时；未发起模型请求，请稍后重试")
            try:
                proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (OSError, ValueError):
                raise _QueryError("Codex 额度查询进程已退出，请检查 CLI 版本和登录状态")

        def response(request_id):
            while time.monotonic() < io_deadline:
                if overflow.is_set():
                    raise _QueryError("Codex 额度响应过大，已停止查询")
                try:
                    message = events.get(timeout=max(0.001, min(0.1, io_deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if message is None:
                    raise _QueryError("Codex 未返回额度结果；请检查 CLI 版本和登录状态")
                if message.get("id") != request_id or "method" in message:
                    continue
                if isinstance(message.get("error"), dict):
                    if message["error"].get("code") == -32601:
                        raise _QueryError("当前 Codex 版本不支持该只读额度接口，请升级后重试", unsupported=True)
                    raise _QueryError("官方额度查询失败，请检查登录、网络或稍后重试")
                if "result" in message:
                    return message["result"]
            raise _QueryError("额度查询超时；未发起模型请求，请稍后重试")

        try:
            send({"id": 0, "method": "initialize", "params": {"clientInfo": {
                "name": "ai_group_chat_quota", "title": "XINGSHU quota", "version": "0.3.0"}}})
            response(0)
            send({"method": "initialized"})
            send({"id": 1, "method": "account/read", "params": {"refreshToken": False}})
            account = response(1)
            account = account.get("account") if isinstance(account, dict) else None
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise _QueryError("只读额度查询需要已登录的 ChatGPT 账号；当前未确认该登录方式", unsupported=True)
            send({"id": 2, "method": "account/rateLimits/read"})
            return _normalize(response(2))
        finally:
            _stop_process(proc, deadline)
            for reader in readers:
                reader.join(timeout=max(0, min(0.05, deadline - time.monotonic())))


def query_quota(member, settings, timeout=8.0):
    """Fetch safe allowance fields only, bounded to 8 seconds by default (max 20).

    No login flow, model turn, generated estimate, API key or browser scraping is
    used. Unsupported providers return immediately without spawning a process.
    """
    if member == "claude":
        return _result("unsupported", "Claude Code",
                       "尚未接入无需生成请求的独立配额查询；可在官方 Claude Code 的 /usage 查看")
    if member == "gemini":
        return _result("unsupported", "Antigravity CLI",
                       "尚无已接入的稳定只读机器额度接口；可在官方 agy 交互界面的 /usage 查看")
    if member != "gpt":
        return _result("unsupported", "", "此成员没有已接入的只读额度接口")
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
            timeout = 8.0
        timeout = max(0.1, min(20.0, timeout))
        conf = settings.get("members", {}).get(member, {})
        cmd = find_command(member, conf)
        if not cmd:
            return _result("error", SOURCE, "未找到 Codex CLI，请先检查成员连接")
        env, _ = member_env(member, settings)
        return _codex_query(cmd, env, timeout)
    except _QueryError as exc:
        return _result("unsupported" if exc.unsupported else "error", SOURCE, str(exc))
    except (OSError, ValueError, TypeError):
        return _result("error", SOURCE, "额度查询未完成，请检查 CLI 设置或稍后重试")


class QuotaMonitor:
    """Per-provider serialized TTL cache. A refresh is an observation, not resume."""

    def __init__(self, demo=False, ttl=60):
        self.demo = bool(demo)
        self.ttl = max(0, float(ttl))
        self._locks = {member: threading.Lock() for member in ("claude", "gpt", "gemini")}
        self._guard = threading.Lock()
        self._cache = {}

    def get(self, member, settings, refresh=False):
        if member not in self._locks:
            return _result("unsupported", "", "未知成员")
        conf = settings.get("members", {}).get(member, {})
        key = (str(conf.get("command") or ""), bool(settings.get("membership_only", True)), self.demo,
               json.dumps(settings.get("proxy", {}), sort_keys=True))
        with self._locks[member]:
            with self._guard:
                entry = self._cache.get(member)
            current = time.monotonic()
            if not refresh and entry and entry["key"] == key and current - entry["saved"] < self.ttl:
                result = copy.deepcopy(entry["result"])
                result["cached"] = True
                return result
            if self.demo:
                result = _result("unsupported", "演示模式", "演示模式没有真实账号额度，也不会查询官方账号", simulation=True)
            else:
                result = query_quota(member, settings)
            result["cached"] = False
            with self._guard:
                self._cache[member] = {"key": key, "saved": time.monotonic(), "result": copy.deepcopy(result)}
            return result

    def snapshot(self):
        """Return the last observations without running any commands."""
        with self._guard:
            return {member: copy.deepcopy(entry["result"]) for member, entry in self._cache.items()}
