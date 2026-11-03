"""网页服务：界面 + JSON 接口，只用 Python 标准库。

安全措施：
- 默认只监听本机（127.0.0.1），并校验 Host 头，防止其他网页借浏览器访问；
- 监听其他地址（例如云服务器上的 0.0.0.0）时必须设置密码（HTTP Basic 认证），连续输错会被暂时拒绝；
- 所有修改类请求都要带自定义请求头 X-AIGC，防止跨站请求伪造；
- 请求体大小有上限，静态文件不能越出目录。
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import mimetypes
import re
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from . import board as board_mod
from .common import MEMBER_ORDER, name_of
from .store import StoreError
from .memory import MemoryError, MemoryConflict

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 2 * 1024 * 1024
MAX_MESSAGE = 20000
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

ROUTES = [
    ("GET", r"/api/info", "info"),
    ("GET", r"/api/members", "members"),
    ("POST", r"/api/members/(claude|gpt|gemini)/ping", "ping"),
    ("POST", r"/api/members/(claude|gpt|gemini)/pause", "pause_member"),
    ("POST", r"/api/members/(claude|gpt|gemini)/resume", "resume_member"),
    ("GET", r"/api/members/(claude|gpt|gemini)/quota", "member_quota"),
    ("GET", r"/api/settings", "get_settings"),
    ("PUT", r"/api/settings", "put_settings"),
    ("GET", r"/api/projects", "projects"),
    ("POST", r"/api/projects", "create_project"),
    ("GET", r"/api/memories/export", "export_memories"),
    ("GET", r"/api/groups", "list_groups"),
    ("POST", r"/api/groups", "create_group"),
    ("GET", r"/api/groups/(g-[a-z0-9]{6,32})", "get_group"),
    ("PATCH", r"/api/groups/(g-[a-z0-9]{6,32})", "patch_group"),
    ("DELETE", r"/api/groups/(g-[a-z0-9]{6,32})", "delete_group"),
    ("GET", r"/api/groups/(g-[a-z0-9]{6,32})/poll", "poll"),
    ("POST", r"/api/groups/(g-[a-z0-9]{6,32})/messages", "post_message"),
    ("POST", r"/api/groups/(g-[a-z0-9]{6,32})/model-preview", "model_preview"),
    ("POST", r"/api/groups/(g-[a-z0-9]{6,32})/retry/(claude|gpt|gemini)", "retry_member"),
    ("POST", r"/api/groups/(g-[a-z0-9]{6,32})/continue", "continue_discussion"),
    ("POST", r"/api/groups/(g-[a-z0-9]{6,32})/stop", "stop"),
    ("POST", r"/api/groups/(g-[a-z0-9]{6,32})/board", "board"),
    ("GET", r"/api/groups/(g-[a-z0-9]{6,32})/export", "export"),
    ("GET", r"/api/groups/(g-[a-z0-9]{6,32})/memories", "memories"),
    ("POST", r"/api/groups/(g-[a-z0-9]{6,32})/memories", "add_memory"),
    ("PATCH", r"/api/groups/(g-[a-z0-9]{6,32})/memories/([a-zA-Z0-9_-]{3,80})", "update_memory"),
    ("DELETE", r"/api/groups/(g-[a-z0-9]{6,32})/memories/([a-zA-Z0-9_-]{3,80})", "delete_memory"),
    ("GET", r"/api/groups/(g-[a-z0-9]{6,32})/memories/([a-zA-Z0-9_-]{3,80})/history", "memory_history"),
    ("GET", r"/api/groups/(g-[a-z0-9]{6,32})/memory-context", "memory_context"),
]
ROUTES = [(m, re.compile("^" + p + "$"), h) for m, p, h in ROUTES]


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    def __init__(self, store, engine, registry, password: Optional[str] = None,
                 local_only: bool = True, demo: bool = False):
        self.store = store
        self.engine = engine
        self.registry = registry
        self.password = password or None
        self.local_only = local_only
        self.demo = demo
        self._failures: dict = {}
        self._fail_lock = threading.Lock()

    # ----- 登录失败限流：15 分钟内同一地址失败 20 次，暂停 15 分钟
    def too_many_failures(self, ip: str) -> bool:
        with self._fail_lock:
            recent = [t for t in self._failures.get(ip, []) if time.time() - t < 900]
            self._failures[ip] = recent
            return len(recent) >= 20

    def record_failure(self, ip: str) -> None:
        with self._fail_lock:
            self._failures.setdefault(ip, []).append(time.time())

    def check_password(self, header: Optional[str]) -> bool:
        if not self.password:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        _, _, given = decoded.partition(":")
        return hmac.compare_digest(given.encode("utf-8"), self.password.encode("utf-8"))


def export_markdown(group: dict, board: dict, messages: list) -> str:
    lines = ["# " + group.get("name", "群聊"), "",
             "导出时间：" + time.strftime("%Y-%m-%d %H:%M"), "",
             "在群成员：" + ("、".join(name_of(m) for m in group.get("members", [])) or "（无）"), "",
             "## 群目标", "", board.get("goal") or "（未填写）", "", "## 决议", ""]
    if not board["decisions"]:
        lines.append("（无）")
    for d in board["decisions"]:
        label = "已撤回" if d.get("withdrawn") else board_mod.DECISION_STATUS.get(d["status"], d["status"])
        lines.append("- **%s【%s】** %s（提议：%s）" % (d["id"], label, d["text"], name_of(d.get("proposer", ""))))
    lines += ["", "## 任务", ""]
    if not board["tasks"]:
        lines.append("（无）")
    for t in board["tasks"]:
        lines.append("- **%s【%s】** %s｜负责人：%s%s" % (
            t["id"], board_mod.TASK_STATUS.get(t["status"], t["status"]), t["title"], name_of(t.get("owner", "")),
            ("｜交付：" + t["deliverable"]) if t.get("deliverable") else ""))
        for note in t.get("notes", []):
            lines.append("  - %s：%s" % (name_of(note.get("who", "")), note.get("text", "")))
    lines += ["", "## 前情摘要", "", board.get("summary") or "（暂无）", "", "## 聊天记录", ""]
    for m in messages:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(m.get("ts", 0)))
        if m.get("kind") == "system":
            lines += ["> 系统（%s）：%s" % (stamp, m.get("text", "")), ""]
        else:
            lines += ["### #%s %s · %s" % (m.get("seq"), name_of(m.get("author", "")), stamp), "", m.get("text", ""), ""]
        route = m.get("model_route")
        if route:
            lines += ["> 模型选择：%s / %s；请求：%s；%s" % (
                route.get("mode", ""), route.get("tier", ""), route.get("requested_model") or "CLI 默认",
                route.get("reason", ""))]
            reported = route.get("reported_models") or ([route["reported_model"]] if route.get("reported_model") else [])
            label = "CLI 配置回显" if route.get("report_kind") == "configuration" else "CLI 报告使用模型（可能多模型）"
            lines += ["> " + ("模拟调用" if route.get("simulation") else
                       (label + "：" + "、".join(reported) if reported else "CLI 未报告实际模型")), ""]
    return "\n".join(lines).rstrip() + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "AIGroupChat/" + __version__
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 只在出错时打印，保持控制台清爽
        return

    # ----- 响应
    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")

    def send_bytes(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, data, extra: Optional[dict] = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8", extra)

    # ----- 入口
    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str):
        try:
            self._guard(method)
            path = urlparse(self.path).path
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            for m, pattern, name in ROUTES:
                match = pattern.match(path)
                if match and m == method:
                    body = self._body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
                    return getattr(self, "api_" + name)(*match.groups(), body=body)
            if any(p.match(path) for _, p, _ in ROUTES):
                raise HttpError(405, "不支持的请求方法")
            raise HttpError(404, "找不到这个地址")
        except HttpError as exc:
            extra = {"WWW-Authenticate": 'Basic realm="AI Group Chat", charset="UTF-8"'} if exc.status == 401 else None
            # 请求体可能还没读完，回完错误就断开，免得剩下的内容被当成下一个请求
            self.close_connection = True
            extra = dict(extra or {}, Connection="close")
            self.send_json(exc.status, {"error": exc.message}, extra)
        except MemoryConflict as exc:
            self.send_json(409, {"error": str(exc)})
        except MemoryError as exc:
            self.send_json(400, {"error": str(exc)})
        except StoreError as exc:
            self.send_json(404, {"error": str(exc)})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            try:
                self.send_json(500, {"error": "服务器内部错误：%s" % exc})
            except Exception:
                pass

    def _guard(self, method: str):
        app = self.app
        ip = self.client_address[0]
        if app.local_only:
            host = (self.headers.get("Host") or "").strip().lower()
            hostname = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if ":" in host else host
            if hostname not in LOCAL_HOSTS:
                raise HttpError(403, "不允许的访问地址。请用 http://localhost 或 http://127.0.0.1 打开")
        if app.password:
            if app.too_many_failures(ip):
                raise HttpError(429, "密码错误次数太多，请 15 分钟后再试")
            if not app.check_password(self.headers.get("Authorization")):
                if self.headers.get("Authorization"):
                    app.record_failure(ip)
                raise HttpError(401, "需要登录")
        if method not in ("GET", "HEAD") and self.headers.get("X-AIGC") != "1":
            raise HttpError(403, "请求缺少校验头")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "请求长度不合法")
        if length < 0:
            raise HttpError(400, "请求长度不合法")
        if length > MAX_BODY:
            if length <= 16 * MAX_BODY:  # 读完丢掉，让对方能正常收到错误提示
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            raise HttpError(413, "内容太大了")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HttpError(400, "请求内容不是合法的 JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "请求内容必须是 JSON 对象")
        return data

    def _static(self, path: str):
        if path in ("/", "/index.html"):
            target = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            target = (STATIC_DIR / path[len("/static/"):]).resolve()
            try:
                target.relative_to(STATIC_DIR)
            except ValueError:
                raise HttpError(404, "找不到这个文件")
        elif path == "/favicon.ico":
            target = STATIC_DIR / "favicon.svg"
        else:
            raise HttpError(404, "找不到这个地址")
        if not target.is_file():
            raise HttpError(404, "找不到这个文件")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        self.send_bytes(200, target.read_bytes(), ctype)

    # ----- 接口
    def _query(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _need_group(self, gid: str) -> None:
        if not self.app.store.exists(gid):
            raise HttpError(404, "找不到这个群，可能已经被删除")

    def api_info(self, body):
        self.send_json(200, {
            "version": __version__, "demo": self.app.demo, "password": bool(self.app.password),
            "local_only": self.app.local_only, "platform": sys.platform,
        })

    def api_members(self, body):
        settings = self.app.store.load_settings()
        found = self.app.registry.detect(settings, refresh=self._query().get("refresh") == "1")
        availability = self.app.engine.availability.snapshot()
        self.send_json(200, {"members": [dict(found[m], availability=availability[m]) for m in MEMBER_ORDER]})

    def api_pause_member(self, member, body):
        state = self.app.engine.availability.pause(member, "manual", "主持人已暂停此成员的新调用，适用于所有群聊。")
        self.send_json(200, {"availability": state})

    def api_resume_member(self, member, body):
        self.send_json(200, {"availability": self.app.engine.availability.resume(member)})

    def api_member_quota(self, member, body):
        quota = self.app.engine.quota.get(member, self.app.store.load_settings(), refresh=self._query().get("refresh") == "1")
        self.send_json(200, {"quota": quota})

    def api_ping(self, member, body):
        if self.app.engine.availability.snapshot()[member]["paused"]:
            self.send_json(200, {"ok": False, "error": "此成员已暂停，请先恢复成员再测试连通。"})
            return
        settings = self.app.store.load_settings()
        cwd = self.app.store.root / "ping-workspace"
        cwd.mkdir(parents=True, exist_ok=True)
        result = self.app.registry.ping(member, settings, cwd)
        if not result.get("ok"):
            self.app.engine.record_member_failure(member, result.get("error", ""))
        self.send_json(200, result)

    def api_get_settings(self, body):
        self.send_json(200, self.app.store.load_settings())

    def api_put_settings(self, body):
        self.send_json(200, self.app.store.save_settings(body))

    def api_list_groups(self, body):
        groups = self.app.store.list_groups()
        for g in groups:
            g["running"] = self.app.engine.state(g["id"])["running"]
        self.send_json(200, {"groups": groups})

    def api_create_group(self, body):
        name = str(body.get("name") or "").strip()
        goal = str(body.get("goal") or "").strip()
        members = body.get("members") if isinstance(body.get("members"), list) else None
        project_id = body.get("project_id") or None
        if project_id is not None and not isinstance(project_id, str):
            raise HttpError(400, "项目编号不合法")
        group = self.app.store.create_group(name, goal, members, str(body.get("mode") or "sequential"), project_id)
        extra = {k: body[k] for k in ("auto_rounds", "reply_length", "parallel_review") if k in body}
        if extra:
            group = self.app.engine.update_group(group["id"], extra)
        first = str(body.get("first_message") or "").strip()
        if first:
            self.app.engine.post_user_message(group["id"], first[:MAX_MESSAGE])
        self.send_json(201, {"group": group})

    def api_get_group(self, gid, body):
        self._need_group(gid)
        store = self.app.store
        self.send_json(200, {
            "group": store.load_group(gid), "board": store.load_board(gid),
            "messages": store.messages(gid), "state": self.app.engine.state(gid), "now": time.time(),
        })

    def api_patch_group(self, gid, body):
        self._need_group(gid)
        self.send_json(200, {"group": self.app.engine.update_group(gid, body)})

    def api_delete_group(self, gid, body):
        self._need_group(gid)
        self.app.engine.runner(gid).stop()
        self.app.store.archive_group(gid)
        self.send_json(200, {"ok": True})

    def api_poll(self, gid, body):
        self._need_group(gid)
        q = self._query()
        try:
            after = int(q.get("after", 0))
            rev = int(q.get("rev", -1))
        except ValueError:
            raise HttpError(400, "参数不合法")
        store = self.app.store
        board = store.load_board(gid)
        self.send_json(200, {
            "messages": [m for m in store.messages(gid) if m.get("seq", 0) > after],
            "board": board if board.get("rev", 0) != rev else None,
            "rev": board.get("rev", 0),
            "state": self.app.engine.state(gid),
            "group": store.load_group(gid),
            "now": time.time(),
        })

    def api_post_message(self, gid, body):
        self._need_group(gid)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HttpError(400, "消息不能为空")
        if len(text) > MAX_MESSAGE:
            raise HttpError(400, "消息太长了（最多 %d 字）" % MAX_MESSAGE)
        msg = self.app.engine.post_user_message(gid, text)
        self.send_json(201, {"message": msg})

    def api_model_preview(self, gid, body):
        self._need_group(gid)
        text = body.get("text", "")
        task = body.get("task", "reply")
        if not isinstance(text, str) or len(text) > MAX_MESSAGE or not isinstance(task, str):
            raise HttpError(400, "预览参数不合法或消息太长")
        self.send_json(200, self.app.engine.model_preview(gid, text, task))

    def api_retry_member(self, gid, member, body):
        self._need_group(gid)
        self.app.engine.retry_member(gid, member)
        self.send_json(200, {"ok": True})

    def api_continue_discussion(self, gid, body):
        self._need_group(gid)
        self.app.engine.continue_discussion(gid)
        self.send_json(200, {"ok": True})

    def api_stop(self, gid, body):
        self._need_group(gid)
        self.send_json(200, {"ok": True, "stopped": self.app.engine.stop(gid)})

    def api_board(self, gid, body):
        self._need_group(gid)
        action = str(body.get("action") or "")
        result = self.app.engine.board_action(gid, action, body, bool(body.get("respond")))
        self.send_json(200, {"ok": True, "result": result, "board": self.app.store.load_board(gid)})

    def api_export(self, gid, body):
        self._need_group(gid)
        store = self.app.store
        group = store.load_group(gid)
        text = export_markdown(group, store.load_board(gid), store.messages(gid))
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", group.get("name") or "群聊")[:60] or "group"
        disposition = "attachment; filename=\"group-chat.md\"; filename*=UTF-8''%s.md" % quote(safe)
        self.send_bytes(200, text.encode("utf-8"), "text/markdown; charset=utf-8",
                        {"Content-Disposition": disposition})

    def api_projects(self, body):
        # Register legacy groups lazily without rewriting their chat files.
        for group in self.app.store.list_groups():
            self.app.store.load_group(group["id"])
        self.send_json(200, {"projects": self.app.store.memory.projects()})

    def api_create_project(self, body):
        self.send_json(201, {"project": self.app.store.memory.create_project(body.get("name", ""))})

    def _memory_group(self, gid):
        self._need_group(gid)
        return self.app.store.load_group(gid)

    def _scoped_memory(self, gid, mid):
        group = self._memory_group(gid)
        item = self.app.store.memory.get(mid)
        if item["project_id"] not in (group["project_id"], "*"):
            raise HttpError(404, "该记忆不属于当前项目")
        return item

    def api_memories(self, gid, body):
        group = self._memory_group(gid)
        memory = self.app.store.memory
        query = self._query()
        project = next(p for p in memory.projects() if p["id"] == group["project_id"])
        items = memory.list_memories(group["project_id"], query.get("q", ""), query.get("status", "all"))
        self.send_json(200, {"memories": items, "project": project,
                             "revision": memory.context(group["project_id"], budget=1)["revision"]})

    def api_add_memory(self, gid, body):
        group = self._memory_group(gid)
        scope = body.get("scope", "project")
        if scope not in ("project", "global"):
            raise HttpError(400, "记忆范围不合法")
        source = {"group_id": gid, "author": "user", "method": "manual"}
        if "source_seq" in body:
            seq = body["source_seq"]
            if type(seq) is not int:
                raise HttpError(400, "来源消息编号不合法")
            original = next((m for m in self.app.store.messages(gid)
                             if m["seq"] == seq and m.get("kind") in ("user", "chat")), None)
            if not original:
                raise HttpError(400, "来源消息不存在")
            source = {"group_id": gid, "seq": seq, "author": original["author"], "method": "selected"}
        item = self.app.store.memory.add("*" if scope == "global" else group["project_id"],
                    body.get("content", ""), kind=body.get("kind", "note"),
                    status=body.get("status", "confirmed"), source=source)
        self.send_json(201, {"memory": item})

    def api_update_memory(self, gid, mid, body):
        self._scoped_memory(gid, mid)
        changes = {k: body[k] for k in ("content", "kind", "status") if k in body}
        item = self.app.store.memory.update(mid, changes, body.get("version"))
        self.send_json(200, {"memory": item})

    def api_delete_memory(self, gid, mid, body):
        item = self._scoped_memory(gid, mid)
        # Delete atomically first so a new round cannot load the old memory while we cancel.
        self.app.store.memory.delete(mid, body.get("version"))
        # Already transmitted prompts cannot be recalled.
        for group in self.app.store.list_groups():
            if item["project_id"] in ("*", group["project_id"]):
                self.app.engine.stop(group["id"])
        self.send_json(200, {"ok": True})

    def api_memory_history(self, gid, mid, body):
        self._scoped_memory(gid, mid)
        self.send_json(200, {"history": self.app.store.memory.history(mid)})

    def api_memory_context(self, gid, body):
        group = self._memory_group(gid)
        self.send_json(200, self.app.store.memory.context(group["project_id"], self._query().get("q", ""), budget=6000))

    def api_export_memories(self, body):
        self.send_json(200, self.app.store.memory.export(),
                       {"Content-Disposition": 'attachment; filename="shared-memory.json"'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, addr, app: App):
        super().__init__(addr, Handler)
        self.app = app

    def handle_error(self, request, client_address):
        # 浏览器中途断开连接很常见，不打印吓人的错误
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Server6(Server):
    address_family = socket.AF_INET6


def make_server(host: str, port: int, app: App) -> Server:
    cls = Server6 if ":" in host else Server
    return cls((host, port), app)
