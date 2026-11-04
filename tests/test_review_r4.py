"""Synthetic persistence failures and byte-preserving corrupt-group archival."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_engine
import test_server

class ProgressFailures(unittest.TestCase):
    setUp = test_engine.EngineTest.setUp
    tearDown = test_engine.EngineTest.tearDown
    wait = test_engine.EngineTest.wait

    def test_completed_reply_progress_failure_does_not_offer_retry(self):
        self.engine.update_group(self.gid, {'members':['claude'], 'auto_rounds':0})
        self.engine.post_user_message(self.gid, 'first'); self.wait()
        old = self.store.member_context(self.gid)['claude']['seen']
        with patch.object(self.store, 'complete_member_context', side_effect=OSError('synthetic progress failure')):
            self.engine.post_user_message(self.gid, 'second'); self.wait()
        self.assertEqual(self.store.member_context(self.gid)['claude']['seen'], old)
        self.assertEqual(self.engine.runner(self.gid).seen['claude'], old)
        self.assertEqual(self.engine.incomplete_members(self.gid), {})
        messages = self.store.messages(self.gid)
        self.assertEqual(sum(m['kind']=='chat' for m in messages), 2)
        self.assertTrue(any('进度未保存' in m['text'] for m in messages))
        for _ in range(2):
            with self.assertRaises(ValueError): self.engine.retry_member(self.gid, 'claude')
        self.engine.post_user_message(self.gid, 'third'); self.wait()
        self.assertEqual(sum(m['kind']=='chat' for m in self.store.messages(self.gid)), 3)

    def test_initial_progress_failure_prevents_message_commit(self):
        self.engine.update_group(self.gid, {'members':['claude'], 'auto_rounds':0})
        original = self.store.messages(self.gid)
        save = self.store.save_group
        def fail(group):
            if group.get('member_context'): raise OSError('synthetic initial progress failure')
            return save(group)
        with patch.object(self.store, 'save_group', side_effect=fail):
            for _ in range(2):
                with self.assertRaises(OSError): self.engine.post_user_message(self.gid, 'request')
                self.assertEqual(self.store.messages(self.gid), original)
                self.assertFalse(self.engine.state(self.gid)['running'])
        self.engine.post_user_message(self.gid, 'request'); self.wait()
        self.assertEqual(sum(m['kind']=='user' for m in self.store.messages(self.gid)), 1)
        self.assertEqual(sum(m['kind']=='chat' for m in self.store.messages(self.gid)), 1)

    def test_message_commit_failure_does_not_submit_or_acknowledge(self):
        self.engine.update_group(self.gid, {'members':['claude'], 'auto_rounds':0})
        original = self.store.messages(self.gid)
        with patch.object(self.store, 'append_message', side_effect=OSError('synthetic precommit')):
            with self.assertRaises(OSError): self.engine.post_user_message(self.gid, 'request')
        self.assertEqual(self.store.messages(self.gid), original)
        self.assertEqual(self.reg.prompts, [])
        self.assertNotIn('seen', self.store.member_context(self.gid).get('claude', {}))
        self.engine.post_user_message(self.gid, 'request'); self.wait()
        self.assertEqual(sum(m['kind']=='user' for m in self.store.messages(self.gid)), 1)

    def test_completed_reply_survives_warning_write_failure(self):
        self.engine.update_group(self.gid, {'members':['claude'], 'auto_rounds':0})
        append = self.store.append_message
        def fail_warning(gid, msg):
            if msg.get('kind') == 'system': raise OSError('synthetic warning write failure')
            return append(gid, msg)
        with patch.object(self.store, 'complete_member_context', side_effect=OSError('synthetic progress failure')), patch.object(self.store, 'append_message', side_effect=fail_warning):
            self.engine.post_user_message(self.gid, 'request'); self.wait()
        self.assertEqual(sum(m['kind']=='chat' for m in self.store.messages(self.gid)), 1)
        self.assertEqual(self.engine.incomplete_members(self.gid), {})
        with self.assertRaises(ValueError): self.engine.retry_member(self.gid, 'claude')

    def test_stop_existing_signals_cancel_without_storage_reads(self):
        from aigc.members import CancelToken
        runner = self.engine.runner(self.gid)
        cancel = runner._cancel = CancelToken()
        runner._worker = True
        try:
            with patch.object(self.store, 'load_group', side_effect=AssertionError('no group read')), patch.object(self.store, 'messages', side_effect=AssertionError('no messages read')):
                self.assertTrue(self.engine.stop_existing(self.gid))
                self.assertTrue(cancel.cancelled)
        finally:
            runner._worker = False

class ArchiveFailures(test_server.Base):
    def test_corrupt_group_archive_does_not_create_runner_or_read_data(self):
        gid = self.store.create_group('synthetic')['id']
        raw = b'{synthetic broken group\xff'
        path = self.store.group_dir(gid)/'group.json'; path.write_bytes(raw)
        with patch.object(self.engine, 'runner', side_effect=AssertionError('must not create runner')), patch.object(self.store, 'member_context', side_effect=AssertionError('must not read context')):
            self.assertEqual(self.request('POST', '/api/groups/'+gid+'/stop', {})[0], 200)
            self.assertEqual(self.request('DELETE', '/api/groups/'+gid)[0], 200)
        self.assertEqual((self.store.archive_dir/gid/'group.json').read_bytes(), raw)
        self.assertFalse(self.store.exists(gid))

    def test_archive_stops_existing_runner_without_loading_corrupt_group(self):
        gid = self.store.create_group('synthetic')['id']
        runner = self.engine.runner(gid)
        raw=b'{synthetic broken group'
        (self.store.group_dir(gid)/'group.json').write_bytes(raw)
        with patch.object(runner, 'stop', return_value=True) as stop, patch.object(self.store, 'load_group', side_effect=AssertionError('must not load group')):
            self.assertEqual(self.request('DELETE', '/api/groups/'+gid)[0], 200)
            stop.assert_called_once_with()
        self.assertEqual((self.store.archive_dir/gid/'group.json').read_bytes(),raw)

if __name__=='__main__': unittest.main(verbosity=2)
