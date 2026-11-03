"""Proxy environment and homepage probe tests; no external network requests."""

import copy
import io
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.network import (DEFAULT_PROXY, _ExplicitProxyHandler, _NoRedirect,
                          apply_proxy, clean_proxy, network_probe)


class ProxyConfigTest(unittest.TestCase):
    def test_defaults_and_independent_normalized_result(self):
        self.assertEqual(clean_proxy({}), DEFAULT_PROXY)
        value = {"mode": "custom", "url": "http://LOCALHOST:7890/"}
        self.assertEqual(clean_proxy(value), {"mode": "custom", "url": "http://localhost:7890"})
        self.assertEqual(value["url"], "http://LOCALHOST:7890/")
        self.assertEqual(clean_proxy({"mode": "custom", "url": "https://[::1]:8080"})["url"],
                         "https://[::1]:8080")

    def test_custom_url_requires_supported_scheme_host_and_explicit_port(self):
        invalid = ["", "127.0.0.1:7890", "socks5://127.0.0.1:7890", "http://localhost",
                   "http://localhost:0", "http://localhost:65536", "http://localhost:not-port",
                   "http://:7890", "http://[not-ip]:7890", "http://bad_host:7890",
                   "http://a..example:7890", "http://localhost:7890/path", "http://localhost:7890?",
                   "http://localhost:7890#", "http://localhost:7890/?token=secret", "http://localhost:7890/#secret",
                   "http://u:p@localhost:7890", "http://user@localhost:7890", "http://@localhost:7890",
                   " http://localhost:7890", "http://localhost:7890\n", "http://local\thost:7890",
                   "http://localhost:7890/$(echo secret)", "http://local\\host:7890"]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                clean_proxy({"mode": "custom", "url": url})

    def test_invalid_config_types_and_unknown_fields_are_rejected(self):
        for value in (None, [], "system", {"mode": "bad"}, {"mode": "custom", "url": 123},
                      {"mode": "system", "token": "secret"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_proxy(value)

    def test_non_custom_mode_may_remember_valid_url_but_not_credentials(self):
        for mode in ("system", "direct"):
            self.assertEqual(clean_proxy({"mode": mode, "url": "http://localhost:7890/"})["url"],
                             "http://localhost:7890")
            with self.assertRaises(ValueError):
                clean_proxy({"mode": mode, "url": "http://secret:password@localhost:7890"})

    def test_system_preserves_proxies_and_merges_bypass_without_mutating_environment(self):
        original = {"PATH": "/example", "HTTP_PROXY": "http://upper:8888", "http_proxy": "http://lower:8888",
                    "HTTPS_PROXY": "http://secure:8888", "ALL_PROXY": "socks5://other:8888",
                    "NO_PROXY": "example.com,localhost", "no_proxy": "internal.test,EXAMPLE.COM"}
        saved = copy.deepcopy(original)
        result = apply_proxy(original, DEFAULT_PROXY)
        self.assertEqual(original, saved)
        self.assertIsNot(original, result)
        for key in ("PATH", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "ALL_PROXY"):
            self.assertEqual(result[key], original[key])
        self.assertEqual(result["NO_PROXY"], result["no_proxy"])
        self.assertEqual(result["NO_PROXY"].split(","), ["example.com", "localhost", "internal.test", "127.0.0.1", "::1"])

    def test_direct_removes_every_proxy_casing_and_keeps_loopback_bypass(self):
        env = {"HTTP_PROXY": "secret", "https_proxy": "secret", "aLl_PrOxY": "secret",
               "Http_Proxy": "secret", "NO_PROXY": "internal.test", "No_Proxy": "other.internal", "UNRELATED": "keep"}
        result = apply_proxy(env, {"mode": "direct"})
        self.assertFalse(any(k.lower() in ("http_proxy", "https_proxy", "all_proxy") for k in result))
        self.assertEqual(result["UNRELATED"], "keep")
        self.assertEqual(result["NO_PROXY"], "internal.test,other.internal,localhost,127.0.0.1,::1")

    def test_custom_sets_both_casings_removes_all_proxy_and_preserves_tls(self):
        env = {"HTTP_PROXY": "http://old:123", "https_proxy": "http://old:123", "ALL_PROXY": "secret",
               "SSL_CERT_FILE": "/trusted/ca.pem", "NODE_EXTRA_CA_CERTS": "/trusted/node.pem"}
        result = apply_proxy(env, {"mode": "custom", "url": "http://localhost:7890"})
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            self.assertEqual(result[key], "http://localhost:7890")
        self.assertNotIn("ALL_PROXY", result)
        self.assertNotIn("NODE_TLS_REJECT_UNAUTHORIZED", result)
        self.assertNotIn("PYTHONHTTPSVERIFY", result)
        self.assertEqual(result["SSL_CERT_FILE"], env["SSL_CERT_FILE"])
        self.assertEqual(result["NODE_EXTRA_CA_CERTS"], env["NODE_EXTRA_CA_CERTS"])

    def test_probe_handler_uses_supplied_proxy_and_bypass_not_ambient_environment(self):
        configured = {"HTTPS_PROXY": "http://upper:1111", "https_proxy": "http://chosen:2222", "no_proxy": "localhost"}
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://ambient:3333", "NO_PROXY": "*"}, clear=True):
            handler = _ExplicitProxyHandler(configured)
            request = urllib.request.Request("https://chatgpt.com/")
            self.assertIsNone(handler.proxy_open(request, handler.proxies["https"], "https"))
            self.assertEqual(request.host, "chosen:2222")
            self.assertEqual(request._tunnel_host, "chatgpt.com")
            local = urllib.request.Request("https://localhost:1455/")
            handler.proxy_open(local, handler.proxies["https"], "https")
            self.assertEqual(local.host, "localhost:1455")
            self.assertIsNone(local._tunnel_host)


class NetworkProbeTest(unittest.TestCase):
    def response(self, code):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = code
        return response

    def test_only_fixed_homepages_and_head_requests_are_used(self):
        expected = {"claude": "https://claude.ai/", "gpt": "https://chatgpt.com/",
                    "gemini": "https://antigravity.google/"}
        for member, endpoint in expected.items():
            response = self.response(200)
            opener = mock.Mock()
            opener.open.return_value = response
            with self.subTest(member=member), mock.patch("aigc.network.urllib.request.build_opener", return_value=opener):
                result = network_probe(member, {"proxy": {"mode": "direct"}})
            self.assertTrue(result["ok"])
            self.assertEqual(result["endpoint"], endpoint)
            self.assertEqual(result["proxy_mode"], "direct")
            self.assertIsInstance(result["checked_at"], float)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, endpoint)
            self.assertEqual(request.get_method(), "HEAD")
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)
            self.assertFalse(response.read.called)
            self.assertIn("不代表登录或模型调用可用", result["message"])
            self.assertEqual(set(result), {"ok", "checked_at", "endpoint", "proxy_mode", "message"})

    def test_http_rejection_proves_response_not_authentication(self):
        for code in (301, 302, 401, 403, 405, 429, 503):
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError("https://chatgpt.com/", code,
                "private diagnostic secret", {"Secret": "do-not-expose"}, io.BytesIO(b"private body"))
            with self.subTest(code=code), mock.patch("aigc.network.urllib.request.build_opener", return_value=opener):
                result = network_probe("gpt", {})
            self.assertTrue(result["ok"])
            self.assertIn(str(code), result["message"])
            self.assertNotIn("private", str(result))
            self.assertNotIn("Secret", str(result))

    def test_proxy_auth_requirement_is_not_reported_as_target_success(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://chatgpt.com/", 407, "secret", {}, io.BytesIO())
        with mock.patch("aigc.network.urllib.request.build_opener", return_value=opener):
            result = network_probe("gpt", {})
        self.assertFalse(result["ok"])
        self.assertIn("代理要求身份认证", result["message"])

    def test_redirects_are_not_followed(self):
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(urllib.request.Request("https://claude.ai/"), None,
            302, "Found", {}, "http://untrusted.invalid/private"))

    def test_failures_never_leak_proxy_credentials_or_raw_diagnostics(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("proxy user:secret@localhost:7890 private account")
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:secret@localhost:7890"}, clear=True), \
                mock.patch("aigc.network.urllib.request.build_opener", return_value=opener):
            result = network_probe("claude", {})
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", str(result))
        self.assertNotIn("private account", str(result))

    def test_worker_timeout_is_bounded_even_if_network_call_ignores_socket_timeout(self):
        release = threading.Event()
        response = self.response(200)
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: (release.wait(1), response)[1]
        try:
            with mock.patch("aigc.network._PROBE_TIMEOUT", 0.02), \
                    mock.patch("aigc.network.urllib.request.build_opener", return_value=opener):
                start = time.monotonic()
                result = network_probe("gpt", {})
                self.assertLess(time.monotonic() - start, 0.3)
            self.assertFalse(result["ok"])
            self.assertIn("超过", result["message"])
        finally:
            release.set()

    def test_busy_workers_reject_more_checks_without_network(self):
        with mock.patch("aigc.network._PROBE_SLOTS") as slots, \
                mock.patch("aigc.network.urllib.request.build_opener") as opener:
            slots.acquire.return_value = False
            result = network_probe("gpt", {})
        self.assertFalse(result["ok"])
        opener.assert_not_called()

    def test_unknown_member_cannot_be_used_as_an_arbitrary_url(self):
        with mock.patch("aigc.network.urllib.request.build_opener") as opener:
            with self.assertRaises(ValueError):
                network_probe("https://internal.invalid/", {})
            with self.assertRaises(ValueError):
                network_probe([], {})
            with self.assertRaises(ValueError):
                network_probe("gpt", {"proxy": {"mode": "custom", "url": "file:///secret"}})
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
