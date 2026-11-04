"""Independent QA's two 6bd1f713 repros, plus restart/reset boundaries."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_engine
from aigc.members import MemberError
from aigc.store import Store


class IndependentProbes(unittest.TestCase):
    def test_summary_failure_not_repeated_in_auto_chain(self):
        t = test_engine.EngineTest()
        t.setUp()
        try:
            t.store.save_settings({"recent_window": 4, "summary_batch": 2})
            t.engine.update_group(t.gid, {"auto_rounds": 2})
            for i in range(6):
                t.store.append_message(t.gid, {
                    "kind": "user", "author": "user",
                    "text": "synthetic history " + str(i),
                })

            def respond(prompt):
                if "书记员" in prompt:
                    raise MemberError("synthetic summary network failure")
                return "synthetic" + test_engine.status(
                    propose_decisions=["synthetic decision"])

            def other(prompt):
                if "书记员" in prompt:
                    raise MemberError("synthetic summary network failure")
                return "synthetic" + test_engine.status()

            t.reg.behaviors["claude"] = respond
            for member in ("gpt", "gemini"):
                t.reg.behaviors[member] = other

            t.engine.post_user_message(t.gid, "synthetic trigger")
            t.wait()
            counts = {
                member: sum("书记员" in p for p in t.reg.prompts_of(member))
                for member in ("claude", "gpt", "gemini")
            }
            print("SUMMARY_COUNTS", counts, flush=True)
            self.assertTrue(all(n <= 1 for n in counts.values()), counts)
        finally:
            t.tearDown()

    def test_large_corrupt_seq_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            gid = store.create_group("synthetic")["id"]
            path = store.group_dir(gid) / "messages.jsonl"
            raw = b'{"seq":' + b"9" * 5000 + b',"text":"broken"}\n'
            path.write_bytes(raw)
            Store(Path(tmp)).messages(gid)
            self.assertTrue(any(
                p.read_bytes() == raw
                for p in path.parent.glob("messages.corrupt-*")
            ))


class FollowupBoundaries(unittest.TestCase):
    def test_large_corrupt_seq_preserves_neighbors_append_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            gid = store.create_group("synthetic")["id"]
            first = store.append_message(gid, {"kind": "user", "author": "user", "text": "before"})
            path = store.group_dir(gid) / "messages.jsonl"
            bad = b'{"seq":' + b'9' * 5000 + b',"text":"broken"}\n'
            with path.open('ab') as f:
                f.write(bad)
                f.write(b'{"seq":2,"kind":"user","author":"user","text":"after","ts":0}\n')
            store = Store(Path(tmp))
            recovered = store.messages(gid)
            self.assertEqual(recovered[0], first)
            self.assertEqual(recovered[1]['text'], 'after')
            self.assertTrue(recovered[-1]['subtle'])
            added = store.append_message(gid, {'kind':'user','author':'user','text':'new'})
            for _ in range(2):
                again = Store(Path(tmp))
                self.assertEqual(again.messages(gid), recovered + [added])
                self.assertFalse(again.list_groups()[0].get('error'))
            self.assertTrue(any(p.read_bytes() == bad for p in path.parent.glob('messages.corrupt-*')))
            seqs = [m['seq'] for m in recovered + [added]]
            self.assertEqual(seqs, sorted(set(seqs)))

    def test_summary_failure_set_resets_for_new_user_job(self):
        t = test_engine.EngineTest()
        t.setUp()
        try:
            t.store.save_settings({'recent_window':4,'summary_batch':2})
            t.engine.update_group(t.gid, {'auto_rounds':2})
            for i in range(6):
                t.store.append_message(t.gid, {'kind':'user','author':'user','text':'history '+str(i)})
            def respond(prompt):
                if '书记员' in prompt:
                    raise MemberError('synthetic summary failure')
                return 'synthetic' + test_engine.status(propose_decisions=['synthetic decision'])
            for member in ('claude','gpt','gemini'):
                t.reg.behaviors[member] = respond
            for turn in (1,2):
                t.engine.post_user_message(t.gid, 'new user job '+str(turn)); t.wait()
                for member in ('claude','gpt','gemini'):
                    self.assertEqual(sum('书记员' in p for p in t.reg.prompts_of(member)), turn)
                self.assertEqual(t.engine.runner(t.gid)._failed_members, {'claude','gpt','gemini'})
        finally:
            t.tearDown()


if __name__ == '__main__':
    unittest.main(verbosity=2)
