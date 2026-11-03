"""Dry-run model previews and atomic model configuration over HTTP."""

import unittest

import test_server as server_tests
from test_model_routing_integration import RecordingRegistry, automatic


class RoutingHttpIntegrationTest(server_tests.Base):
    def setUp(self):
        super().setUp()
        self.reg = RecordingRegistry()
        self.engine.registry = self.reg
        self.server.app.registry = self.reg
        self.group = self.store.create_group("路由预览")
        self.gid = self.group["id"]

    def test_preview_is_side_effect_free_and_uses_host_overrides(self):
        self.store.save_settings(automatic())
        code, data, _ = self.request("PATCH", "/api/groups/" + self.gid, {"model_overrides": {"claude": {"mode": "manual", "model": "host-preview"}}})
        self.assertEqual(code, 200, data)
        self.store.memory.add(self.group["project_id"], "本项目需核验算法", kind="decision")
        paths = [self.store.root / "settings.json", self.store.group_dir(self.gid) / "group.json",
                 self.store.group_dir(self.gid) / "board.json", self.store.group_dir(self.gid) / "messages.jsonl"]
        before = {str(p): p.read_bytes() for p in paths}
        memory_before = self.store.memory.export()
        runners_before = set(self.engine._runners)
        code, data, _ = self.request("POST", "/api/groups/%s/model-preview" % self.gid, {"text": "请给出定理证明"})
        self.assertEqual(code, 200, data)
        self.assertTrue(data["estimate"])
        self.assertEqual(data["errors"], {})
        self.assertEqual(data["routes"]["claude"]["requested_model"], "host-preview")
        self.assertEqual(data["routes"]["gpt"]["requested_model"], "old-gpt-deep")
        self.assertEqual(data["routes"]["gemini"]["requested_model"], "old-gemini-deep")
        self.assertEqual(self.reg.calls, [])
        self.assertEqual(set(self.engine._runners), runners_before)
        self.assertEqual({str(p): p.read_bytes() for p in paths}, before)
        memory_after = self.store.memory.export()
        memory_before.pop("exported_at")
        memory_after.pop("exported_at")
        self.assertEqual(memory_after, memory_before)

    def test_invalid_routing_settings_save_is_atomic(self):
        self.store.save_settings(automatic())
        before = (self.store.root / "settings.json").read_bytes()
        code, data, _ = self.request("PUT", "/api/settings", {"members": {
            "claude": {"model": "must-not-persist", "routing": {"mode": "manual"}},
            "gpt": {"routing": {"mode": "unknown"}}}})
        self.assertEqual(code, 400, data)
        self.assertEqual((self.store.root / "settings.json").read_bytes(), before)
        self.assertEqual(self.reg.calls, [])

    def test_missing_gemini_profiles_reject_auto_override_without_group_mutation(self):
        path = self.store.group_dir(self.gid) / "group.json"
        before = path.read_bytes()
        code, data, _ = self.request("PATCH", "/api/groups/" + self.gid, {
            "name": "must-not-persist", "model_overrides": {"gemini": {"mode": "auto"}}})
        self.assertEqual(code, 400, data)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.reg.calls, [])

    def test_valid_override_roundtrip_and_clearing_restores_inheritance(self):
        self.store.save_settings(automatic())
        url = "/api/groups/" + self.gid
        override = {"gpt": {"mode": "manual", "model": "host-model"}}
        code, data, _ = self.request("PATCH", url, {"model_overrides": override})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["group"]["model_overrides"], override)
        _, readback, _ = self.request("GET", url)
        self.assertEqual(readback["group"]["model_overrides"], override)
        code, data, _ = self.request("PATCH", url, {"model_overrides": {}})
        self.assertEqual(code, 200, data)
        _, preview, _ = self.request("POST", url + "/model-preview", {"text": "你好"})
        self.assertEqual(preview["routes"]["gpt"]["requested_model"], "old-gpt-fast")
        self.assertEqual(preview["routes"]["gpt"]["source"], "member_setting")

    def test_preview_rejects_invalid_input_without_calls_or_messages(self):
        url = "/api/groups/%s/model-preview" % self.gid
        for body in ({"text": []}, {"text": "x" * 20001}, {"task": []}, {"task": "bad-stage"}):
            with self.subTest(body=str(body)[:100]):
                self.assertEqual(self.request("POST", url, body)[0], 400)
        self.assertEqual(self.request("POST", url, {"text": "你好"}, headers={"X-AIGC": ""})[0], 403)
        self.assertEqual(self.reg.calls, [])
        self.assertEqual(self.store.messages(self.gid), [])


if __name__ == "__main__":
    unittest.main()
