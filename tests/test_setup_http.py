"""Local-only setup endpoints never run real login/install/network operations."""

import copy
import time
from unittest.mock import MagicMock, patch

import test_server as server_tests
from aigc.common import atomic_write_json


JOB_ID = "a" * 32


class SetupHttpTest(server_tests.Base):
    def setUp(self):
        super().setUp()
        self.app = self.server.app
        self.real_setup = self.app.setup
        self.job = {"id": JOB_ID, "member": "gpt", "action": "login", "status": "running",
                    "message": "请在官方终端完成授权", "created_at": time.time(), "updated_at": time.time()}
        self.fake = MagicMock(spec=("overview", "start", "job", "cancel"))
        self.fake.start.return_value = copy.deepcopy(self.job)
        self.fake.job.return_value = copy.deepcopy(self.job)
        self.fake.cancel.return_value = dict(self.job, cancel_requested=True)
        self.app.setup = self.fake
        launcher = patch("aigc.setup._launch_terminal", side_effect=AssertionError("real terminal forbidden"))
        self.launcher = launcher.start()
        self.addCleanup(launcher.stop)
        network = patch("aigc.server.network_probe", side_effect=AssertionError("real network forbidden"))
        self.network = network.start()
        self.addCleanup(network.stop)

    def mutations(self):
        return [
            ("POST", "/api/setup/gpt/start", {"action": "login"}),
            ("POST", "/api/setup/jobs/%s/cancel" % JOB_ID, {}),
            ("POST", "/api/setup/proxy", {"proxy": {"mode": "direct", "url": ""}}),
            ("POST", "/api/setup/gpt/network", {}),
            ("PUT", "/api/settings", {"proxy": {"mode": "direct", "url": ""}}),
        ]

    def assert_no_effects(self):
        self.fake.start.assert_not_called()
        self.fake.cancel.assert_not_called()
        self.launcher.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(self.store.load_settings()["proxy"], {"mode": "system", "url": ""})

    def test_overview_and_completed_job_reads_do_not_launch_or_claim_login(self):
        self.app.setup = self.real_setup
        complete = dict(self.job, status="completed", message="流程已结束，请重新检查 CLI 登录状态。")
        directory = self.real_setup.root / JOB_ID
        directory.mkdir(parents=True)
        atomic_write_json(directory / "state.json", complete)
        with patch("aigc.setup.find_command", return_value=None), \
                patch.object(self.app.registry, "detect") as detect, \
                patch("subprocess.Popen", side_effect=AssertionError("read spawned process")), \
                patch("subprocess.run", side_effect=AssertionError("read spawned process")):
            status, overview, _ = self.request("GET", "/api/setup")
            self.assertEqual(status, 200)
            self.assertEqual(set(overview["members"]), {"claude", "gpt", "gemini"})
            self.assertFalse(overview["can_launch"])
            status, data, _ = self.request("GET", "/api/setup/jobs/" + JOB_ID)
            self.assertEqual(status, 200)
            self.assertEqual(data, {"job": complete})
            self.assertNotIn("logged_in", data["job"])
            self.assertNotIn("auth_ok", data["job"])
            detect.assert_not_called()
        self.assert_no_effects()

    def test_start_and_cancel_dispatch_only_fixed_action_and_saved_settings(self):
        self.app.demo = False
        proxy = {"mode": "custom", "url": "http://localhost:7890"}
        self.store.save_settings({"proxy": proxy})
        body = {"action": "login", "command": "UNTRUSTED COMMAND", "path": "/untrusted/path",
                "proxy": {"mode": "direct", "url": ""}}
        status, data, _ = self.request("POST", "/api/setup/gpt/start", body)
        self.assertEqual(status, 202)
        self.assertEqual(data, {"job": self.job})
        args = self.fake.start.call_args.args
        self.assertEqual(args[:2], ("gpt", "login"))
        self.assertEqual(args[2], self.store.load_settings())
        self.assertEqual(args[2]["proxy"], proxy)
        self.assertNotIn("path", args[2])
        status, data, _ = self.request("POST", "/api/setup/jobs/%s/cancel" % JOB_ID, {})
        self.assertEqual(status, 200)
        self.assertTrue(data["job"]["cancel_requested"])
        self.fake.cancel.assert_called_once_with(JOB_ID)
        self.launcher.assert_not_called()

    def test_demo_rejects_all_start_actions_and_simulates_network(self):
        self.app.setup = self.real_setup
        for member in ("claude", "gpt", "gemini"):
            for action in ("install", "login", "setup"):
                with self.subTest(member=member, action=action):
                    status, data, _ = self.request("POST", "/api/setup/%s/start" % member, {"action": action})
                    self.assertEqual(status, 400)
                    self.assertIn("演示", data["error"])
            status, data, _ = self.request("POST", "/api/setup/%s/network" % member, {})
            self.assertEqual(status, 200)
            self.assertIsNone(data["ok"])
            self.assertTrue(data["simulation"])
        self.assertFalse(self.real_setup.root.exists())
        self.assert_no_effects()

    def test_invalid_member_job_action_and_body_cannot_reach_launcher(self):
        for path in ("/api/setup/unknown/start", "/api/setup/gpt/../start", "/api/setup/gpt%2fstart",
                     "/api/setup/jobs/../cancel", "/api/setup/jobs/" + "z" * 32 + "/cancel",
                     "/api/setup/jobs/" + "a" * 31 + "/cancel"):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, {"action": "login"})[0], 404)
        self.app.setup = self.real_setup
        for action in (None, "", "execute", "login;anything", ["login"], {"login": True}):
            with self.subTest(action=action):
                self.assertEqual(self.request("POST", "/api/setup/gpt/start", {"action": action})[0], 400)
        for raw in (b"[1]", b"not-json"):
            self.assertEqual(self.request("POST", "/api/setup/gpt/start", raw=raw)[0], 400)
        self.assert_no_effects()

    def test_mutations_require_csrf_header_before_any_side_effect(self):
        for method, path, body in self.mutations():
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, body, headers={"X-AIGC": ""})[0], 403)
        self.assert_no_effects()

    def test_remote_server_mode_rejects_setup_even_with_valid_password(self):
        self.app.local_only = False
        self.app.password = "local-password"
        for method, path, body in self.mutations():
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, body, auth="owner:local-password")[0], 403)
        self.assert_no_effects()
        status, data, _ = self.request("PUT", "/api/settings", {
            "members": {"gpt": {"model": "remote-model-setting"}}}, auth="owner:local-password")
        self.assertEqual(status, 200)
        self.assertEqual(data["members"]["gpt"]["model"], "remote-model-setting")

    def test_non_loopback_peer_is_rejected_even_with_localhost_host_header(self):
        original = self.server.get_request

        def remote_peer():
            sock, address = original()
            return sock, ("198.51.100.10", address[1])

        with patch.object(self.server, "get_request", side_effect=remote_peer):
            for method, path, body in self.mutations():
                with self.subTest(path=path):
                    self.assertEqual(self.request(method, path, body)[0], 403)
        self.assert_no_effects()

    def test_proxy_endpoint_validates_atomically_and_preserves_model_settings(self):
        self.store.save_settings({"members": {"gpt": {"model": "saved-model"}}})
        config = {"mode": "custom", "url": "http://localhost:7890"}
        status, data, _ = self.request("POST", "/api/setup/proxy", {"proxy": config})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"proxy": config})
        path = self.store.root / "settings.json"
        before = path.read_bytes()
        for body in ({}, {"proxy": None}, {"proxy": "direct"},
                     {"proxy": {"mode": "custom", "url": "http://user:secret@localhost:7890"}},
                     {"proxy": {"mode": "custom", "url": "file:///tmp/private"}}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/setup/proxy", body)[0], 400)
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.load_settings()["members"]["gpt"]["model"], "saved-model")
        self.launcher.assert_not_called()
        self.network.assert_not_called()

    def test_network_uses_saved_proxy_and_fixed_member_without_saving_request_fields(self):
        self.app.demo = False
        self.store.save_settings({"proxy": {"mode": "direct", "url": ""}})
        before = (self.store.root / "settings.json").read_bytes()
        result = {"ok": False, "member": "claude", "message": "stub network failure"}
        self.network.side_effect = None
        self.network.return_value = result
        status, data, _ = self.request("POST", "/api/setup/claude/network", {
            "url": "http://untrusted.example", "command": "arbitrary command", "proxy": {"mode": "system"}})
        self.assertEqual(status, 200)
        self.assertEqual(data, result)
        self.network.assert_called_once_with("claude", self.store.load_settings())
        self.assertEqual((self.store.root / "settings.json").read_bytes(), before)
        self.fake.start.assert_not_called()
        self.launcher.assert_not_called()

    def test_setup_reads_respect_existing_password_guard(self):
        self.app.password = "local-password"
        for path in ("/api/setup", "/api/setup/jobs/" + JOB_ID):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 401)
                self.assertEqual(self.request("GET", path, auth="owner:wrong")[0], 401)
        self.fake.overview.assert_not_called()
        self.fake.job.assert_not_called()
        self.assert_no_effects()
