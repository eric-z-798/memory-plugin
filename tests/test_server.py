"""网页服务：接口、登录密码、跨站防护、静态文件。"""

import base64
import http.client
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.engine import Engine  # noqa: E402
from aigc.members import Registry  # noqa: E402
from aigc.server import App, make_server  # noqa: E402
from aigc.store import Store  # noqa: E402


class Base(unittest.TestCase):
    password = None
    local_only = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aigc-srv-")
        self.store = Store(Path(self.tmp))
        registry = Registry(demo=True, demo_delay=0.01)
        self.engine = Engine(self.store, registry)
        app = App(self.store, self.engine, registry, password=self.password, local_only=self.local_only, demo=True)
        self.access_token = app.access_token
        self.server = make_server("127.0.0.1", 0, app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def tearDown(self):
        self.engine.shutdown()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, method, path, body=None, headers=None, raw=None, auth=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "localhost:%d" % self.port, "X-Xingshu-Token": self.access_token}
        if method not in ("GET", "HEAD"):
            h["X-AIGC"] = "1"
        if auth is not None:
            h["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        try:
            parsed = json.loads(payload.decode()) if payload else None
        except ValueError:
            parsed = payload.decode(errors="replace")
        return res.status, parsed, res


class ApiTest(Base):
    def wait_idle(self, gid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data, _ = self.request("GET", "/api/groups/%s/poll?after=0&rev=-1" % gid)
            if not data["state"]["running"]:
                return data
            time.sleep(0.05)
        self.fail("没有在规定时间内结束")

    def test_static_files(self):
        status, body, res = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("星枢 XINGSHU", body)
        self.assertIn("default-src 'self'", res.getheader("Content-Security-Policy"))
        for path in ("/static/app.js", "/static/style.css", "/static/favicon.svg"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        self.assertEqual(self.request("GET", "/static/../server.py")[0], 404)
        self.assertEqual(self.request("GET", "/static/%2e%2e/server.py")[0], 404)
        self.assertEqual(self.request("GET", "/nope")[0], 404)

    def test_full_flow(self):
        status, data, _ = self.request("POST", "/api/groups", {
            "name": "接口测试", "goal": "测试目标", "members": ["claude", "gpt", "gemini"],
            "first_message": "大家好"})
        self.assertEqual(status, 201)
        gid = data["group"]["id"]
        final = self.wait_idle(gid)
        authors = [m["author"] for m in final["messages"]]
        self.assertEqual(authors[:4], ["user", "claude", "gpt", "gemini"])
        board = final["board"]
        self.assertEqual(board["decisions"][0]["status"], "effective")

        # 看板没变时不重复发送
        status, data, _ = self.request("GET", "/api/groups/%s/poll?after=%d&rev=%d" % (gid, final["messages"][-1]["seq"], final["rev"]))
        self.assertIsNone(data["board"])
        self.assertEqual(data["messages"], [])

        status, data, _ = self.request("POST", "/api/groups/%s/board" % gid,
                                       {"action": "comment", "id": "D-001", "text": "再收窄范围", "respond": True})
        self.assertEqual(status, 200)
        self.assertIn("退回讨论", data["result"])
        final = self.wait_idle(gid)
        self.assertEqual(final["board"]["decisions"][0]["status"], "effective")

        status, data, _ = self.request("POST", "/api/groups/%s/board" % gid, {"action": "force", "id": "D-404"})
        self.assertEqual(status, 400)

        status, data, _ = self.request("PATCH", "/api/groups/%s" % gid, {"mode": "parallel", "auto_rounds": 1})
        self.assertEqual(data["group"]["mode"], "parallel")

        status, text, res = self.request("GET", "/api/groups/%s/export" % gid)
        self.assertEqual(status, 200)
        self.assertIn("# 接口测试", text)
        self.assertIn("D-001", text)
        self.assertIn("attachment", res.getheader("Content-Disposition"))

        status, data, _ = self.request("GET", "/api/groups")
        self.assertEqual(data["groups"][0]["id"], gid)
        self.assertTrue(data["groups"][0]["preview"])

        self.assertEqual(self.request("DELETE", "/api/groups/%s" % gid)[0], 200)
        self.assertEqual(self.request("GET", "/api/groups/%s" % gid)[0], 404)
        self.assertTrue((Path(self.tmp) / "archive" / gid).exists())

    def test_stop_and_continue(self):
        status, data, _ = self.request("POST", "/api/groups", {"name": "x"})
        gid = data["group"]["id"]
        self.assertEqual(self.request("POST", "/api/groups/%s/continue" % gid, {})[0], 200)
        self.wait_idle(gid)
        status, data, _ = self.request("POST", "/api/groups/%s/stop" % gid, {})
        self.assertEqual(status, 200)
        self.assertFalse(data["stopped"])

    def test_settings_roundtrip(self):
        status, data, _ = self.request("PUT", "/api/settings", {
            "members": {"gpt": {"model": "gpt-5", "timeout": 5}}, "recent_window": 999})
        self.assertEqual(data["members"]["gpt"]["model"], "gpt-5")
        self.assertEqual(data["members"]["gpt"]["timeout"], 30)
        self.assertEqual(data["recent_window"], 60)
        status, data, _ = self.request("GET", "/api/members")
        self.assertEqual([m["id"] for m in data["members"]], ["claude", "gpt", "gemini"])
        status, data, _ = self.request("POST", "/api/members/gpt/ping", {})
        self.assertTrue(data["ok"])

    def test_bad_requests(self):
        self.assertEqual(self.request("POST", "/api/groups", raw=b"{not json")[0], 400)
        self.assertEqual(self.request("POST", "/api/groups", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.request("POST", "/api/groups", raw=b"x" * (3 * 1024 * 1024))[0], 413)
        self.assertEqual(self.request("GET", "/api/groups/g-doesnotexist/poll")[0], 404)
        self.assertEqual(self.request("PUT", "/api/groups")[0], 405)
        status, data, _ = self.request("POST", "/api/groups", {"name": "y"})
        gid = data["group"]["id"]
        self.assertEqual(self.request("POST", "/api/groups/%s/messages" % gid, {"text": "  "})[0], 400)

    def test_csrf_header_required(self):
        status, data, _ = self.request("POST", "/api/groups", {"name": "x"}, headers={"X-AIGC": ""})
        self.assertEqual(status, 403)

    def test_host_header_checked(self):
        status, data, _ = self.request("GET", "/api/groups", headers={"Host": "evil.example:8765"})
        self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", "/api/groups", headers={"Host": "127.0.0.1:1"})[0], 200)
        self.assertEqual(self.request("GET", "/api/groups", headers={"Host": "[::1]:1"})[0], 200)


class PasswordTest(Base):
    password = "s3cret-口令"
    local_only = False

    def test_password_required(self):
        status, data, res = self.request("GET", "/api/groups")
        self.assertEqual(status, 401)
        self.assertIn("Basic", res.getheader("WWW-Authenticate"))
        self.assertEqual(self.request("GET", "/api/groups", auth="any:wrong")[0], 401)
        self.assertEqual(self.request("GET", "/api/groups", auth="whoever:s3cret-口令")[0], 200)
        self.assertEqual(self.request("GET", "/", auth="u:s3cret-口令")[0], 200)
        # 不在本机模式时不检查 Host
        self.assertEqual(self.request("GET", "/api/groups", auth="u:s3cret-口令", headers={"Host": "my-server:8765"})[0], 200)

    def test_lockout_after_many_failures(self):
        for _ in range(20):
            self.request("GET", "/api/groups", auth="u:wrong")
        self.assertEqual(self.request("GET", "/api/groups", auth="u:s3cret-口令")[0], 429)


if __name__ == "__main__":
    unittest.main()
