"""Account pauses survive restart without mutating model settings."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.availability import AvailabilityStore
from aigc.common import MEMBER_ORDER, atomic_write_json


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aigc-availability-")
        self.root = Path(self.temp.name)
        self.store = AvailabilityStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_are_unpaused_and_read_does_not_create_file(self):
        self.assertEqual(set(self.store.snapshot()), set(MEMBER_ORDER))
        for state in self.store.snapshot().values():
            self.assertEqual(state, {"paused": False, "reason": "", "message": "", "since": 0.0})
        self.assertFalse(self.store.path.exists())

    def test_pause_restart_resume_restart(self):
        with patch("aigc.availability.now", return_value=123.5):
            state = self.store.pause("claude", "quota", "额度用尽，请稍后恢复。")
        self.assertEqual(state, {"paused": True, "reason": "quota", "message": "额度用尽，请稍后恢复。", "since": 123.5})
        restarted = AvailabilityStore(self.root)
        self.assertEqual(restarted.snapshot()["claude"], state)
        self.assertFalse(restarted.snapshot()["gpt"]["paused"])
        resumed = restarted.resume("claude")
        self.assertEqual(resumed, {"paused": False, "reason": "", "message": "", "since": 0.0})
        self.assertEqual(AvailabilityStore(self.root).snapshot()["claude"], resumed)

    def test_repeat_pause_preserves_start_and_new_pause_resets_it(self):
        with patch("aigc.availability.now", return_value=10.0):
            self.store.pause("gpt", "rate_limit")
        with patch("aigc.availability.now", return_value=20.0):
            state = self.store.pause("gpt", "manual", "群主暂停")
            self.assertEqual(state["since"], 10.0)
            self.assertEqual(state["reason"], "manual")
            self.store.resume("gpt")
            self.assertEqual(self.store.pause("gpt", "quota")["since"], 20.0)

    def test_outputs_are_detached_and_messages_are_bounded(self):
        result = self.store.pause("gemini", "manual", " " + "字" * 700 + " ")
        self.assertEqual(len(result["message"]), 500)
        result["paused"] = False
        snapshot = self.store.snapshot()
        snapshot["gemini"]["message"] = "changed"
        self.assertTrue(self.store.snapshot()["gemini"]["paused"])
        self.assertEqual(self.store.snapshot()["gemini"]["message"], "字" * 500)

    def test_concurrent_member_updates_preserve_every_pause(self):
        barrier = threading.Barrier(3)
        errors = []

        def pause_member(member):
            try:
                barrier.wait(timeout=5)
                for _ in range(10):
                    self.store.pause(member, "quota", member + " 额度用尽")
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=pause_member, args=(member,)) for member in MEMBER_ORDER]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        persisted = AvailabilityStore(self.root).snapshot()
        self.assertEqual(persisted, self.store.snapshot())
        for member in MEMBER_ORDER:
            self.assertTrue(persisted[member]["paused"])
            self.assertEqual(persisted[member]["message"], member + " 额度用尽")
        self.assertFalse(self.store.path.with_name(self.store.path.name + ".tmp").exists())

    def test_separate_settings_file_is_not_overwritten(self):
        settings_path = self.root / "settings.json"
        settings = {"members": {"gpt": {"model": "custom-model"}}}
        atomic_write_json(settings_path, settings)
        self.store.pause("gpt", "quota")
        self.assertEqual(json.loads(settings_path.read_text()), settings)
        atomic_write_json(settings_path, {"updated": True})
        self.assertTrue(AvailabilityStore(self.root).snapshot()["gpt"]["paused"])

    def test_validation_rejects_unknown_members_reasons_and_nontext_messages(self):
        for member in ("unknown", "ChatGPT", None, [], 7):
            with self.subTest(member=member), self.assertRaises(ValueError):
                self.store.pause(member, "quota")
            with self.subTest(member=member), self.assertRaises(ValueError):
                self.store.resume(member)
        for reason in ("", "exhausted", None, [], 7):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.store.pause("gpt", reason)
        for message in (None, {}, 7):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.store.pause("gpt", "quota", message)
        self.assertFalse(self.store.path.exists())

    def test_failed_write_does_not_commit_memory_state(self):
        before = self.store.snapshot()
        with patch("aigc.availability.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.pause("gpt", "quota")
        self.assertEqual(self.store.snapshot(), before)
        self.store.pause("gpt", "quota")
        with patch("aigc.availability.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.resume("gpt")
        self.assertTrue(self.store.snapshot()["gpt"]["paused"])

    def test_load_projects_only_known_state_fields_and_preserves_malformed_pause(self):
        atomic_write_json(self.store.path, {
            "claude": {"paused": True, "reason": "quota", "message": "x", "since": 1, "api_key": "not copied"},
            "gpt": {"paused": True, "reason": "future_reason", "message": {}, "since": -2},
            "gemini": {"paused": False, "reason": "quota", "message": "stale", "since": 1},
            "other": {"paused": True},
        })
        snapshot = AvailabilityStore(self.root).snapshot()
        self.assertNotIn("other", snapshot)
        self.assertNotIn("api_key", snapshot["claude"])
        self.assertEqual(snapshot["gpt"], {"paused": True, "reason": "manual", "message": "", "since": 0.0})
        self.assertEqual(snapshot["gemini"], {"paused": False, "reason": "", "message": "", "since": 0.0})


if __name__ == "__main__":
    unittest.main()
