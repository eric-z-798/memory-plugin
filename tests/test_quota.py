"""Synthetic stdio app-server fixtures; never query real accounts or models."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aigc.quota import QuotaMonitor, _normalize, query_quota
from aigc.store import DEFAULT_SETTINGS


FIXTURE = r'''
import json, sys, time
mode, log = sys.argv[1:]
for line in sys.stdin:
    message = json.loads(line)
    with open(log, 'a', encoding='utf-8') as out:
        out.write(json.dumps(message) + '\n')
    method = message.get('method')
    if method == 'initialize':
        if mode == 'timeout':
            time.sleep(60)
        if mode == 'oversized':
            print('x' * (300 * 1024), flush=True)
            continue
        print(json.dumps({'id': message['id'], 'result': {'userAgent': 'fixture'}}), flush=True)
    elif method == 'initialized':
        continue
    elif method == 'account/read':
        assert message['params'] == {'refreshToken': False}
        account = {'type': 'apiKey' if mode == 'api' else 'chatgpt',
                   'email': 'private-account@example.invalid', 'accessToken': 'do-not-expose'}
        print(json.dumps({'id': message['id'], 'result': {'account': account}}), flush=True)
    elif method == 'account/rateLimits/read':
        if mode == 'unsupported':
            result = {'id': message['id'], 'error': {'code': -32601, 'message': 'private raw debug'}}
        elif mode == 'error':
            result = {'id': message['id'], 'error': {'code': -32000, 'message': 'private raw debug do-not-expose'}}
        else:
            bucket = {'limitId': 'codex', 'limitName': 'Codex',
                      'primary': {'usedPercent': 25, 'windowDurationMins': 300, 'resetsAt': 1900000000},
                      'secondary': {'usedPercent': 80.5, 'windowDurationMins': 10080, 'resetsAt': None},
                      'credits': {'balance': 'private-balance', 'hasCredits': True}}
            other = {'limitId': 'model-special', 'limitName': 'Special model',
                     'primary': {'usedPercent': 100, 'windowDurationMins': None, 'resetsAt': 1900000001}}
            payload = {'rateLimits': bucket, 'rateLimitsByLimitId': {'codex': bucket, 'special': other},
                       'ordinaryUsageAllowed': False, 'tokens': 'do-not-expose'}
            result = {'id': message['id'], 'result': payload}
        print(json.dumps({'method': 'notification', 'params': {'secret': 'do-not-expose'}}), flush=True)
        print(json.dumps(result), flush=True)
    else:
        raise RuntimeError('Must not call login, thread/start, turn/start or models: ' + str(method))
'''


class QuotaQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aigc-quota-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "fake_app_server.py"
        self.fixture.write_text(FIXTURE, encoding="utf-8")
        self.log = self.root / "requests.jsonl"
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.spawn = subprocess.Popen
        self.processes = []

    def query(self, mode="ok", timeout=2):
        def spawn(args, **kwargs):
            if os.name == "nt" and args[0] == "taskkill":
                return self.spawn(args, **kwargs)
            self.assertEqual(args, ["fixture-codex", "app-server", "--listen", "stdio://"])
            proc = self.spawn([sys.executable, str(self.fixture), mode, str(self.log)], **kwargs)
            self.processes.append(proc)
            return proc
        with mock.patch("aigc.quota.find_command", return_value="fixture-codex"), \
                mock.patch("aigc.quota.subprocess.Popen", side_effect=spawn):
            return query_quota("gpt", self.settings, timeout=timeout)

    def requests(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_safe_windows_and_only_readonly_rpc_sequence(self):
        result = self.query()
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["windows"]), 3, "默认桶和按ID桶不应重复")
        primary, secondary, special = result["windows"]
        self.assertEqual(primary["remaining_percent"], 75)
        self.assertEqual(primary["resets_at"], 1900000000)
        self.assertEqual(secondary["remaining_percent"], 19.5)
        self.assertIsNone(secondary["resets_at"])
        self.assertEqual(special["limit_id"], "model-special")
        self.assertIs(result["ordinary_usage_allowed"], False)
        rendered = json.dumps(result)
        for secret in ("private-account", "do-not-expose", "private-balance", "accessToken", "credits"):
            self.assertNotIn(secret, rendered)
        self.assertEqual([request["method"] for request in self.requests()],
                         ["initialize", "initialized", "account/read", "account/rateLimits/read"])
        self.assertTrue(all(proc.poll() is not None for proc in self.processes))

    def test_api_login_does_not_query_or_change_auth(self):
        result = self.query("api")
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual([request["method"] for request in self.requests()],
                         ["initialize", "initialized", "account/read"])

    def test_unknown_method_is_unsupported_without_raw_error(self):
        result = self.query("unsupported")
        self.assertEqual(result["status"], "unsupported")
        self.assertNotIn("private raw debug", json.dumps(result))

    def test_query_error_does_not_expose_server_error_body(self):
        result = self.query("error")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("do-not-expose", json.dumps(result))

    def test_timeout_is_bounded_and_process_is_stopped(self):
        started = time.monotonic()
        result = self.query("timeout", timeout=0.4)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(result["status"], "error")
        self.assertIn("超时", result["message"])
        self.assertTrue(all(proc.poll() is not None for proc in self.processes))

    def test_oversized_response_is_bounded(self):
        result = self.query("oversized")
        self.assertEqual(result["status"], "error")
        self.assertTrue(all(proc.poll() is not None for proc in self.processes))

    def test_unsupported_providers_never_spawn(self):
        with mock.patch("aigc.quota.subprocess.Popen") as spawn:
            for member in ("claude", "gemini"):
                result = query_quota(member, self.settings)
                self.assertEqual(result["status"], "unsupported")
                self.assertEqual(result["windows"], [])
            spawn.assert_not_called()

    def test_missing_cli_returns_error(self):
        with mock.patch("aigc.quota.find_command", return_value=None):
            result = query_quota("gpt", self.settings)
        self.assertEqual(result["status"], "error")


class QuotaNormalizationTest(unittest.TestCase):
    def test_percentages_never_imply_permission_or_recovery(self):
        result = _normalize({"rateLimits": {"primary": {"usedPercent": 0}}})
        self.assertIsNone(result["ordinary_usage_allowed"])
        self.assertNotIn("can_resume", result)
        self.assertNotIn("remaining_tokens", result)

    def test_unknown_values_do_not_become_zero_or_100_percent(self):
        result = _normalize({"rateLimits": {
            "primary": {"usedPercent": None}, "secondary": {"usedPercent": True}}})
        self.assertEqual(result["windows"], [])
        self.assertIn("不能据此推断", result["message"])
        result = _normalize({"rateLimits": {
            "primary": {"usedPercent": -1}, "secondary": {"usedPercent": float("inf")}}})
        self.assertEqual(result["windows"], [])

    def test_explicit_reasons_are_whitelisted(self):
        result = _normalize({"rateLimits": {"rateLimitReachedType": "rate_limit_reached", "spendControlReached": True}})
        self.assertEqual(result["limit_reached_types"], ["rate_limit_reached"])
        self.assertTrue(result["spend_control_reached"])
        result = _normalize({"rateLimits": {"rateLimitReachedType": "private-debug"}})
        self.assertEqual(result["limit_reached_types"], [])


class QuotaMonitorTest(unittest.TestCase):
    def setUp(self):
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.fixture = {"status": "available", "checked_at": 1, "source": "fixture", "windows": [], "message": "fixture"}

    def test_ttl_refresh_key_change_and_snapshot(self):
        monitor = QuotaMonitor(ttl=60)
        with mock.patch("aigc.quota.query_quota", return_value=self.fixture) as query:
            self.assertEqual(monitor.snapshot(), {})
            first = monitor.get("gpt", self.settings)
            self.assertFalse(first["cached"])
            second = monitor.get("gpt", self.settings)
            self.assertTrue(second["cached"])
            self.assertEqual(query.call_count, 1)
            monitor.get("gpt", self.settings, refresh=True)
            self.assertEqual(query.call_count, 2)
            self.settings["members"]["gpt"]["command"] = "other-codex"
            monitor.get("gpt", self.settings)
            self.assertEqual(query.call_count, 3)
            snapshot = monitor.snapshot()
            snapshot["gpt"]["message"] = "mutated"
            self.assertNotEqual(monitor.snapshot()["gpt"]["message"], "mutated")
            self.assertEqual(query.call_count, 3)

    def test_zero_ttl_expires_and_demo_never_queries(self):
        monitor = QuotaMonitor(ttl=0)
        with mock.patch("aigc.quota.query_quota", return_value=self.fixture) as query:
            monitor.get("gpt", self.settings)
            monitor.get("gpt", self.settings)
            self.assertEqual(query.call_count, 2)
        with mock.patch("aigc.quota.query_quota") as query:
            result = QuotaMonitor(demo=True).get("gpt", self.settings, refresh=True)
            self.assertEqual(result["status"], "unsupported")
            self.assertTrue(result["simulation"])
            query.assert_not_called()

    def test_concurrent_reads_share_one_lookup(self):
        monitor = QuotaMonitor(ttl=60)
        entered = threading.Event()
        release = threading.Event()
        results = []

        def query(*args):
            entered.set()
            release.wait(timeout=1)
            return copy.deepcopy(self.fixture)

        with mock.patch("aigc.quota.query_quota", side_effect=query) as lookup:
            threads = [threading.Thread(target=lambda: results.append(monitor.get("gpt", self.settings))) for _ in range(3)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(timeout=1))
            release.set()
            for thread in threads:
                thread.join(timeout=1)
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
