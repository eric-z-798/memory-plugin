"""Proxy settings reach existing CLI/query paths without changing global state."""

import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aigc.common import atomic_write_json
from aigc.members import Registry, member_env
from aigc.quota import QuotaMonitor
from aigc.store import Store


class ProxyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aigc-proxy-integration-")
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_settings_inherit_system_proxy_without_rewriting_file(self):
        legacy = {"members": {"gpt": {"model": "existing-model", "command": "/existing/codex"}}}
        path = self.store.root / "settings.json"
        atomic_write_json(path, legacy)
        before = path.read_bytes()
        settings = self.store.load_settings()
        self.assertEqual(settings["proxy"], {"mode": "system", "url": ""})
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://inherited:7890"}, clear=True):
            env, _ = member_env("gpt", settings)
            self.assertEqual(env["HTTPS_PROXY"], "http://inherited:7890")
        self.assertEqual(path.read_bytes(), before)

    def test_custom_proxy_reaches_all_member_environments_without_keys_or_tls_bypass(self):
        self.store.save_settings({"proxy": {"mode": "custom", "url": "http://localhost:7890"}})
        original = {"PATH": "/test", "HTTPS_PROXY": "http://old:7777", "ALL_PROXY": "socks5://old:8888",
                    "OPENAI_API_KEY": "fake-openai", "ANTHROPIC_API_KEY": "fake-anthropic",
                    "GEMINI_API_KEY": "fake-gemini", "NO_PROXY": "internal.example",
                    "SSL_CERT_FILE": "/trusted/ca.pem", "NODE_EXTRA_CA_CERTS": "/trusted/node.pem"}
        with patch.dict(os.environ, original, clear=True):
            for member, secret in (("claude", "ANTHROPIC_API_KEY"), ("gpt", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY")):
                env, removed = member_env(member, self.store.load_settings())
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                    self.assertEqual(env[key], "http://localhost:7890")
                self.assertNotIn("ALL_PROXY", env)
                self.assertNotIn(secret, env)
                self.assertIn(secret, removed)
                self.assertEqual(env["NO_PROXY"], env["no_proxy"])
                self.assertTrue({"localhost", "127.0.0.1", "::1", "internal.example"}.issubset(set(env["NO_PROXY"].split(","))))
                self.assertNotIn("NODE_TLS_REJECT_UNAUTHORIZED", env)
                self.assertNotIn("PYTHONHTTPSVERIFY", env)
                self.assertEqual(env["SSL_CERT_FILE"], original["SSL_CERT_FILE"])
                self.assertEqual(env["NODE_EXTRA_CA_CERTS"], original["NODE_EXTRA_CA_CERTS"])
            self.assertEqual(dict(os.environ), original)

    def test_direct_proxy_removes_inherited_routes_but_not_model_configuration(self):
        self.store.save_settings({"members": {"gpt": {"model": "keep-model"}}, "proxy": {"mode": "direct", "url": ""}})
        settings = self.store.load_settings()
        with patch.dict(os.environ, {"HTTP_PROXY": "old", "https_proxy": "old", "all_proxy": "old"}, clear=True):
            env, _ = member_env("gpt", settings)
        self.assertFalse(any(key.lower() in ("http_proxy", "https_proxy", "all_proxy") for key in env))
        self.assertEqual(settings["members"]["gpt"]["model"], "keep-model")

    def test_invalid_proxy_or_routing_rejects_entire_settings_update(self):
        self.store.save_settings({"proxy": {"mode": "direct", "url": ""}, "members": {"gpt": {"model": "original"}}})
        path = self.store.root / "settings.json"
        before = path.read_bytes()
        for update in (
            {"proxy": {"mode": "custom", "url": "http://user:password@localhost:7890"}, "members": {"gpt": {"model": "changed"}}},
            {"proxy": {"mode": "custom", "url": "http://localhost:7890"}, "members": {"gpt": {"routing": {"mode": "invalid"}}}},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.store.save_settings(update)
            self.assertEqual(path.read_bytes(), before)

    def test_concurrent_proxy_and_model_updates_merge_without_lost_settings(self):
        self.store.save_settings({"members": {"claude": {"model": "untouched"}}})
        gate = threading.Barrier(2)
        errors = []

        def proxy_updates():
            try:
                gate.wait(3)
                for index in range(12):
                    self.store.save_settings({"proxy": {"mode": "custom", "url": "http://localhost:%d" % (7890 + index)}})
            except Exception as exc:
                errors.append(exc)

        def model_updates():
            try:
                gate.wait(3)
                for index in range(12):
                    self.store.save_settings({"members": {"gpt": {"model": "model-%d" % index}}})
            except Exception as exc:
                errors.append(exc)

        original_write = atomic_write_json

        def slow_write(path, data):
            time.sleep(0.001)
            original_write(path, data)

        with patch("aigc.store.atomic_write_json", side_effect=slow_write):
            threads = [threading.Thread(target=proxy_updates), threading.Thread(target=model_updates)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        settings = Store(self.store.root).load_settings()
        self.assertEqual(settings["proxy"], {"mode": "custom", "url": "http://localhost:7901"})
        self.assertEqual(settings["members"]["gpt"]["model"], "model-11")
        self.assertEqual(settings["members"]["claude"]["model"], "untouched")

    def test_quota_cache_invalidates_when_only_proxy_changes(self):
        monitor = QuotaMonitor(demo=False, ttl=60)
        settings = self.store.load_settings()
        observation = {"status": "error", "source": "stub", "windows": [], "message": "stub"}
        with patch("aigc.quota.query_quota", return_value=observation) as query:
            first = monitor.get("gpt", settings)
            cached = monitor.get("gpt", settings)
            self.assertFalse(first["cached"])
            self.assertTrue(cached["cached"])
            changed = copy.deepcopy(settings)
            changed["proxy"] = {"mode": "custom", "url": "http://localhost:7890"}
            refreshed = monitor.get("gpt", changed)
            self.assertFalse(refreshed["cached"])
            self.assertEqual(query.call_count, 2)
            self.assertEqual(query.call_args.args[1]["proxy"], changed["proxy"])

    def test_connection_detection_invalidates_cache_and_passes_new_proxy_environment(self):
        registry = Registry(demo=False)
        settings = self.store.load_settings()
        captured = []

        def fake_process(cmd, text, cwd, timeout, cancel, env):
            captured.append(copy.deepcopy(env))
            if cmd[-1] == "--version":
                return 0, "fake-cli 1.0", ""
            return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), ""

        with patch("aigc.members.find_command", return_value="/fake/claude"), patch("aigc.members.run_process", side_effect=fake_process):
            registry._detect_one("claude", settings, False)
            registry._detect_one("claude", settings, False)
            self.assertEqual(len(captured), 2)
            settings["proxy"] = {"mode": "custom", "url": "http://localhost:7890"}
            registry._detect_one("claude", settings, False)
            self.assertEqual(len(captured), 4)
            self.assertEqual(captured[-1]["HTTPS_PROXY"], "http://localhost:7890")
            self.assertIn("127.0.0.1", captured[-1]["NO_PROXY"])


if __name__ == "__main__":
    unittest.main()
