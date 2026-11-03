"""Model selection is local, explainable and subordinate to the host."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.routing import clean_overrides, clean_routing, default_routing, resolve_route, route_settings


class RoutingTests(unittest.TestCase):
    def settings(self, member="claude", mode="auto"):
        conf = default_routing(member)
        conf["mode"] = mode
        conf["profiles"] = {"fast": "test-fast", "balanced": "test-balanced", "deep": "test-deep"}
        return {"members": {member: {"model": "fixed-model", "routing": conf, "web": True}}}

    def route(self, query="你好", task="reply", context=0):
        return resolve_route("claude", self.settings(), {}, task, query, context)

    def test_simple_balanced_and_deep_questions(self):
        self.assertEqual(self.route()["requested_model"], "test-fast")
        self.assertEqual(self.route("分析这个架构的权衡")["tier"], "balanced")
        self.assertEqual(self.route("请给出定理证明")["tier"], "deep")
        self.assertEqual(self.route("证明黎曼猜想")["tier"], "deep")
        self.assertEqual(self.route("审查这个并发架构\n```python\ndef f(): pass\n```")["tier"], "deep")

    def test_lengths_and_structure_affect_selection_without_keywords(self):
        self.assertEqual(self.route("字" * 160)["tier"], "fast")
        self.assertEqual(self.route("字" * 161)["tier"], "balanced")
        self.assertEqual(self.route("字" * 2000, context=50000)["tier"], "deep")
        self.assertEqual(self.route("1. 甲\n2. 乙\n3. 丙\n4. 丁")["tier"], "balanced")
        self.assertEqual(self.route("必须红色，必须小，不能重")["tier"], "balanced")

    def test_task_and_context_do_not_force_cheap_model_for_hard_summary(self):
        self.assertEqual(self.route(task="summary")["tier"], "fast")
        self.assertEqual(self.route(task="vote")["tier"], "fast")
        self.assertEqual(self.route(task="review")["tier"], "balanced")
        self.assertEqual(self.route(task="debate")["tier"], "balanced")
        self.assertEqual(self.route("安全审计并分析架构", task="summary")["tier"], "deep")
        self.assertEqual(self.route(context=19999)["tier"], "fast")
        self.assertEqual(self.route(context=20000)["tier"], "balanced")

    def test_manual_host_takes_priority_over_global_auto(self):
        group = {"model_overrides": {"claude": {"mode": "manual", "model": "host-model"}}}
        result = resolve_route("claude", self.settings(), group, "reply", "形式化证明")
        self.assertEqual((result["mode"], result["tier"], result["source"], result["requested_model"]),
                         ("manual", "manual", "group_override", "host-model"))

    def test_host_auto_overrides_fixed_model_and_inherit_preserves_it(self):
        settings = self.settings(mode="manual")
        group = {"model_overrides": {"claude": {"mode": "auto"}}}
        self.assertEqual(resolve_route("claude", settings, group, "reply", "你好")["requested_model"], "test-fast")
        self.assertEqual(resolve_route("claude", settings, {}, "reply", "你好")["requested_model"], "fixed-model")

    def test_old_settings_and_empty_host_model_mean_cli_default(self):
        route = resolve_route("gpt", {"members": {"gpt": {"model": "old"}}}, {}, "reply", "你好")
        self.assertEqual(route["requested_model"], "old")
        route = resolve_route("gpt", {}, {"model_overrides": {"gpt": {"mode": "manual"}}}, "reply", "你好")
        self.assertEqual(route["requested_model"], "")
        self.assertIn("CLI 默认", route["reason"])

    def test_missing_auto_profile_fails_without_silent_fallback(self):
        with self.assertRaises(ValueError):
            clean_routing("gemini", {"mode": "auto"})
        settings = self.settings("gemini")
        settings["members"]["gemini"]["routing"]["profiles"]["deep"] = ""
        with self.assertRaises(ValueError):
            resolve_route("gemini", settings, {}, "reply", "你好")
        # A fixed host selection can recover from incomplete auto profiles.
        group = {"model_overrides": {"gemini": {"mode": "manual", "model": "known-model"}}}
        self.assertEqual(resolve_route("gemini", settings, group, "reply", "你好")["requested_model"], "known-model")

    def test_each_provider_uses_its_own_profiles(self):
        for member in ("claude", "gpt", "gemini"):
            settings = self.settings(member)
            settings["members"][member]["routing"]["profiles"]["fast"] = member + "/quick"
            self.assertEqual(resolve_route(member, settings, {}, "reply", "你好")["requested_model"], member + "/quick")
        self.assertEqual(default_routing("gemini")["profiles"]["fast"], "")

    def test_route_and_snapshot_are_immutable(self):
        settings = self.settings()
        group = {"model_overrides": {}}
        originals = copy.deepcopy((settings, group))
        route = resolve_route("claude", settings, group, "reply", "你好")
        snapshot = route_settings(settings, "claude", route)
        self.assertEqual((settings, group), originals)
        self.assertEqual(snapshot["members"]["claude"]["model"], "test-fast")
        snapshot["members"]["claude"]["routing"]["profiles"]["fast"] = "different"
        self.assertEqual(settings["members"]["claude"]["routing"]["profiles"]["fast"], "test-fast")
        defaults = default_routing("claude")
        defaults["profiles"]["fast"] = "different"
        self.assertEqual(default_routing("claude")["profiles"]["fast"], "haiku")

    def test_route_has_explainable_metadata(self):
        result = self.route("分析架构", context=20000)
        self.assertEqual(result["complexity"]["method"], "heuristic-v1")
        self.assertEqual(result["complexity"]["query_chars"], 4)
        self.assertEqual(result["complexity"]["context_chars"], 20000)
        self.assertEqual(result["complexity"]["score"], 3)
        self.assertTrue(result["complexity"]["signals"])
        self.assertIn("均衡", result["reason"])
        self.assertNotIn("actual_model", result)

    def test_configuration_validation(self):
        invalid = [None, [], {"mode": "maybe"}, {"profiles": []}, {"profiles": {"cheap": "x"}},
                   {"profiles": {"fast": None}}, {"profiles": {"fast": "x\ny"}},
                   {"profiles": {"fast": "--override"}}, {"profiles": {"fast": "a" * 201}},
                   {"unknown": 1}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_routing("claude", value)
        invalid_overrides = [None, [], {"other": {"mode": "auto"}}, {"claude": []},
                             {"claude": {"mode": "inherit"}}, {"claude": {"mode": "manual", "model": 2}},
                             {"claude": {"mode": "manual", "typo": 1}}]
        for value in invalid_overrides:
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_overrides(value)
        self.assertEqual(clean_routing("claude", {"profiles": {"fast": " haiku "}})["profiles"]["fast"], "haiku")

    def test_official_context_window_aliases_survive_routing(self):
        for model in ("sonnet[1m]", "opus[1m]", "claude-sonnet-4-6[1m]"):
            with self.subTest(model=model):
                routing = clean_routing("claude", {"mode": "auto", "profiles": {"fast": model}})
                settings = {"members": {"claude": {"routing": routing}}}
                route = resolve_route("claude", settings, {}, "reply", "你好")
                self.assertEqual(route["requested_model"], model)
                self.assertEqual(route_settings(settings, "claude", route)["members"]["claude"]["model"], model)
                self.assertEqual(clean_overrides({"claude": {"mode": "manual", "model": model}})["claude"]["model"], model)
        for model in ("sonnet[1m];echo", "sonnet[$(id)]", "sonnet[1m][1m]", "sonnet[]", "sonnet[1 m]"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                clean_routing("claude", {"profiles": {"fast": model}})

    def test_invalid_route_inputs_are_rejected(self):
        for member, query, chars in (("other", "x", 0), ("claude", None, 0),
                                     ("claude", "x", -1), ("claude", "x", True), ("claude", "x", 1.2)):
            with self.subTest(member=member, query=query, chars=chars), self.assertRaises(ValueError):
                resolve_route(member, self.settings(), {}, "reply", query, chars)


if __name__ == "__main__":
    unittest.main()
