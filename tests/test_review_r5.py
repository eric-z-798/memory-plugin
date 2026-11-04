"""Readback is evidence of visibility, not fsync success or crash durability."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_engine
from aigc.store import Store, MessageAppendError
from aigc.engine import Engine

class AppendOutcomes(unittest.TestCase):
    setUp = test_engine.EngineTest.setUp
    tearDown = test_engine.EngineTest.tearDown
    wait = test_engine.EngineTest.wait

    def fsync_failure(self, kind):
        real = os.fsync
        fired = []
        def fail(fd):
            # fstat rather than /proc makes the synthetic injection portable.
            path = self.store.group_dir(self.gid) / 'messages.jsonl'
            if path.exists() and os.fstat(fd).st_ino == path.stat().st_ino and not fired:
                if ('"kind": "%s"' % kind).encode() in path.read_bytes():
                    fired.append(True)
                    raise OSError('synthetic full-write fsync failure')
            return real(fd)
        return patch('aigc.store.os.fsync', side_effect=fail), fired

    def test_complete_reply_fsync_error_blocks_duplicate_retry_and_restart(self):
        self.engine.update_group(self.gid, {'members':['claude'], 'auto_rounds':0})
        fault, fired = self.fsync_failure('chat')
        with fault:
            self.engine.post_user_message(self.gid, 'synthetic'); self.wait()
        self.assertTrue(fired)
        self.assertEqual(len(self.reg.prompts), 1)
        self.assertNotIn('seen', self.store.member_context(self.gid)['claude'])
        rows = Store(Path(self.tmp)).messages(self.gid)
        self.assertEqual(sum(m['kind']=='chat' for m in rows), 1)
        self.assertTrue(any('耐久性未知' in m['text'] and 'synthetic full-write fsync failure' in m['text'] for m in rows))
        self.assertEqual(self.engine.incomplete_members(self.gid), {})
        for _ in range(2):
            with self.assertRaises(ValueError): self.engine.retry_member(self.gid, 'claude')
        restarted = Engine(Store(Path(self.tmp)), self.reg)
        try:
            self.assertEqual(restarted.incomplete_members(self.gid), {})
            with self.assertRaises(ValueError): restarted.retry_member(self.gid, 'claude')
        finally: restarted.shutdown()
        self.assertEqual(len(self.reg.prompts), 1)
        self.engine.post_user_message(self.gid, 'next'); self.wait()
        self.assertEqual(sum(m['kind']=='chat' for m in self.store.messages(self.gid)), 2)

    def test_complete_user_fsync_error_acknowledges_visibility_without_starting(self):
        self.engine.update_group(self.gid, {'members':['claude'], 'auto_rounds':0})
        fault, fired = self.fsync_failure('user')
        with fault: result = self.engine.post_user_message(self.gid, 'synthetic')
        self.assertTrue(fired)
        self.assertFalse(result['discussion_started'])
        self.assertIn('耐久性未知', result['write_warning'])
        self.assertEqual(self.reg.prompts, [])
        self.assertEqual(sum(m['kind']=='user' for m in Store(Path(self.tmp)).messages(self.gid)), 1)
        self.engine.continue_discussion(self.gid); self.wait()
        self.assertEqual(sum(m['kind']=='user' for m in self.store.messages(self.gid)), 1)
        self.assertEqual(sum(m['kind']=='chat' for m in self.store.messages(self.gid)), 1)

    def test_readback_preserves_exception_identity_and_no_recovery(self):
        failure = OSError('synthetic sync')
        with patch('aigc.store.os.fsync', side_effect=failure):
            with self.assertRaises(MessageAppendError) as caught:
                self.store.append_message(self.gid, {'kind':'user','text':'synthetic'})
        self.assertIs(caught.exception.__cause__, failure)
        self.assertTrue(caught.exception.complete)
        self.assertEqual(caught.exception.message['seq'], 1)
        self.assertEqual(list(self.store.group_dir(self.gid).glob('messages.corrupt-*')), [])

    def test_failed_retry_full_write_does_not_leave_old_retry_pending(self):
        from aigc.members import MemberError
        self.engine.update_group(self.gid, {'members':['claude'], 'auto_rounds':0})
        self.reg.behaviors['claude'] = lambda prompt: (_ for _ in ()).throw(MemberError('synthetic first failure'))
        self.engine.post_user_message(self.gid, 'synthetic'); self.wait()
        self.assertIn('claude', self.engine.incomplete_members(self.gid))
        self.reg.behaviors['claude'] = lambda prompt: 'synthetic reply' + test_engine.status()
        fault, fired = self.fsync_failure('chat')
        with fault:
            self.engine.retry_member(self.gid, 'claude'); self.wait()
        self.assertTrue(fired)
        self.assertEqual(self.engine.incomplete_members(self.gid), {})
        with self.assertRaises(ValueError): self.engine.retry_member(self.gid, 'claude')
        self.assertEqual(sum(m['kind']=='chat' for m in Store(Path(self.tmp)).messages(self.gid)), 1)

    def test_failed_readback_is_unknown_not_absent(self):
        real_open = open
        def opened(path, mode='r', *args, **kwargs):
            if mode == 'rb' and str(path).endswith('messages.jsonl'):
                raise OSError('synthetic readback unavailable')
            return real_open(path, mode, *args, **kwargs)
        self.store.messages(self.gid)  # Only fault the readback, not initial load.
        with patch('aigc.store.open', side_effect=opened), patch('aigc.store.os.fsync', side_effect=OSError('synthetic sync')):
            with self.assertRaises(MessageAppendError) as caught:
                self.store.append_message(self.gid, {'kind':'chat','author':'claude','text':'synthetic'})
        self.assertIsNone(caught.exception.complete)
        self.assertIn('synthetic sync', str(caught.exception))
        self.assertIn('synthetic readback unavailable', str(caught.exception))
        self.assertEqual(sum(m['kind']=='chat' for m in Store(Path(self.tmp)).messages(self.gid)), 1)

    def test_partial_write_is_not_reported_complete_and_recovers(self):
        real_open = open
        class Partial:
            def __init__(self, f): self.f=f
            def __enter__(self): return self
            def __exit__(self, *args): self.f.close()
            def __getattr__(self, key): return getattr(self.f, key)
            def write(self, raw):
                self.f.write(raw[:24]); self.f.flush()
                raise OSError('synthetic partial append')
        def opened(path, mode='r', *args, **kwargs):
            f=real_open(path, mode, *args, **kwargs)
            return Partial(f) if mode=='ab+' else f
        with patch('aigc.store.open', side_effect=opened):
            with self.assertRaises(MessageAppendError) as caught:
                self.store.append_message(self.gid, {'kind':'chat','author':'claude','text':'synthetic'})
        self.assertFalse(caught.exception.complete)
        path=self.store.group_dir(self.gid)/'messages.jsonl'; raw=path.read_bytes()
        restarted=Store(Path(self.tmp))
        added=restarted.append_message(self.gid, {'kind':'user','text':'next'})
        self.assertGreater(added['seq'], 1)
        self.assertTrue(any(p.read_bytes()==raw for p in path.parent.glob('messages.corrupt-*')))
        self.assertEqual(restarted.messages(self.gid), Store(Path(self.tmp)).messages(self.gid))

if __name__=='__main__': unittest.main(verbosity=2)
