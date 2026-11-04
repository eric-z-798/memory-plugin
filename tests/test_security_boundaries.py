"""Synthetic HTTP authorization, directory ownership and memory withdrawal."""
import sys
import socket
import os
import threading
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from secure_fixtures import secure_temporary_directory
import test_server
import test_engine

class AccessBoundary(test_server.Base):
    def test_missing_token_rejected_before_sensitive_operations(self):
        with patch.object(self.engine.registry,'ping',side_effect=AssertionError('must not invoke CLI')):
            for method,path,body in [('GET','/api/settings',None),('GET','/api/groups',None),('PUT','/api/settings',{'membership_only':False}),('POST','/api/members/claude/ping',{})]:
                self.assertEqual(self.request(method,path,body,headers={'X-Xingshu-Token':''})[0],401)

    def test_all_api_reads_need_token_but_static_page_is_public(self):
        for token in ('', 'incorrect-synthetic-token'):
            for method, path in [('GET','/api/groups'),('HEAD','/api/settings'),('GET','/api/members?refresh=1'),('GET','/api/members/gpt/quota?refresh=1'),('GET','/api/memories/export')]:
                self.assertEqual(self.request(method,path,headers={'X-Xingshu-Token':token})[0],401)
        self.assertEqual(self.request('GET','/',headers={'X-Xingshu-Token':''})[0],200)
        self.assertEqual(self.request('GET','/api/settings')[0],200)
        self.assertNotIn(self.access_token, self.request('GET','/api/info')[1].values())

    def test_command_path_validation_and_execution_revalidation(self):
        from aigc.members import find_command
        with secure_temporary_directory() as directory:
            path=Path(directory)/'synthetic-cli';path.write_text('#!/bin/sh\nexit 0\n');path.chmod(0o700)
            body={'members':{'claude':{'command':str(path)}}}
            self.assertEqual(self.request('PUT','/api/settings',body)[0],200)
            self.assertEqual(find_command('claude',{'command':str(path)}),str(path))
            path.chmod(0o777)
            self.assertIsNone(find_command('claude',{'command':str(path)}))
            self.assertEqual(self.request('PUT','/api/settings',{'members':{'gpt':{'command':str(path)}}})[0],400)
        self.assertEqual(self.request('PUT','/api/settings',{'members':{'gpt':{'command':'relative-cli'}}})[0],400)

    def test_capability_rotation_and_no_persistence(self):
        from aigc.server import App
        fresh=App(self.store,self.engine,self.engine.registry,demo=True)
        self.assertNotEqual(fresh.access_token,self.access_token)
        for path in self.store.root.rglob('*'):
            if path.is_file():self.assertNotIn(self.access_token.encode(),path.read_bytes())
        app=self.server.app
        previous=app.access_token
        app.access_token=fresh.access_token
        try:
            self.assertEqual(self.request('GET','/api/groups')[0],401)
            self.assertEqual(self.request('GET','/api/groups',headers={'X-Xingshu-Token':fresh.access_token})[0],200)
        finally:app.access_token=previous

class RemoteBoundary(test_server.Base):
    local_only=False
    password='synthetic-test-password'
    def test_password_and_token_are_both_required_and_sensitive_changes_are_local(self):
        self.assertEqual(self.request('GET','/api/settings',auth='x:'+self.password,headers={'X-Xingshu-Token':''})[0],401)
        self.assertEqual(self.request('GET','/api/settings')[0],401)
        for body in ({'membership_only':False},{'members':{'gpt':{'command':'/synthetic/cli'}}},{'members':{'gpt':{'extra_args':'--synthetic'}}}):
            self.assertEqual(self.request('PUT','/api/settings',body,auth='x:'+self.password)[0],403)
        self.assertEqual(self.request('PUT','/api/settings',{'members':{'gpt':{'model':'synthetic-model'}}},auth='x:'+self.password)[0],200)

class Withdrawal(unittest.TestCase):
    setUp=test_engine.EngineTest.setUp
    tearDown=test_engine.EngineTest.tearDown
    wait=test_engine.EngineTest.wait

    def test_withdrawn_memory_not_sent_to_later_members(self):
        self.engine.update_group(self.gid,{'auto_rounds':0})
        item=self.store.memory.add(self.gid,'SYNTHETIC_PRIVATE_BUDGET',kind='decision')
        def answer(prompt):
            self.store.memory.update(item['id'],{'status':'pending'},item['version'])
            return 'synthetic'+test_engine.status()
        self.reg.behaviors['claude']=answer
        self.engine.post_user_message(self.gid,'budget');self.wait()
        self.assertIn('SYNTHETIC_PRIVATE_BUDGET',self.reg.prompts_of('claude')[0])
        for member in ('gpt','gemini'):
            self.assertNotIn('SYNTHETIC_PRIVATE_BUDGET',self.reg.prompts_of(member)[0])
            msg=next(m for m in self.store.messages(self.gid) if m.get('author')==member)
            self.assertNotIn(item['id'],msg['memory_ids'])
            self.assertIn(item['id'],msg['memory_withdrawn_ids'])

    def test_global_withdrawal_during_parallel_calls_removes_review_context(self):
        self.engine.update_group(self.gid,{'auto_rounds':0,'mode':'parallel','parallel_review':True})
        item=self.store.memory.add('*','GLOBAL_SYNTHETIC_PRIVATE',kind='decision')
        barrier=threading.Barrier(3)
        def answer(prompt):
            if 'GLOBAL_SYNTHETIC_PRIVATE' in prompt:
                leader=barrier.wait(timeout=3)
                if leader==0:self.store.memory.update(item['id'],{'status':'pending'},item['version'])
                barrier.wait(timeout=3)
            return 'synthetic'+test_engine.status()
        for member in ('claude','gpt','gemini'):self.reg.behaviors[member]=answer
        self.engine.post_user_message(self.gid,'synthetic trigger');self.wait()
        for member in ('claude','gpt','gemini'):
            calls=self.reg.prompts_of(member)
            self.assertEqual(len(calls),2)
            self.assertIn('GLOBAL_SYNTHETIC_PRIVATE',calls[0])
            self.assertNotIn('GLOBAL_SYNTHETIC_PRIVATE',calls[1])

    def test_deleted_memory_is_filtered_and_read_errors_fail_closed(self):
        item=self.store.memory.add(self.gid,'SYNTHETIC_DELETE',kind='decision')
        snapshot=self.store.memory.context(self.gid)
        self.store.memory.delete(item['id'],item['version'])
        context=self.store.memory.revalidate_context(snapshot)
        self.assertEqual(context['items'],[])
        self.assertEqual(context['withdrawn_ids'],[item['id']])
        self.engine.update_group(self.gid,{'auto_rounds':0})
        with patch.object(self.store.memory,'revalidate_context',side_effect=OSError('synthetic database unavailable')):
            self.engine.post_user_message(self.gid,'@claude test');self.wait()
        self.assertEqual(self.reg.prompts,[])

    def test_queued_calls_and_other_group_observe_global_withdrawal(self):
        import threading
        other=self.store.create_group('synthetic other',members=['claude','gpt'])['id']
        for gid in (self.gid,other):self.engine.update_group(gid,{'members':['claude','gpt'],'auto_rounds':0})
        item=self.store.memory.add('*','SHARED_SYNTHETIC_SECRET',kind='decision')
        ready=threading.Barrier(3);release=threading.Event()
        def answer(prompt):
            if 'SHARED_SYNTHETIC_SECRET' in prompt:
                ready.wait(timeout=3)
                if not release.wait(3):raise AssertionError('release timed out')
            return 'synthetic'+test_engine.status()
        self.reg.behaviors['claude']=answer
        try:
            for gid in (self.gid,other):self.engine.post_user_message(gid,'initial')
            ready.wait(timeout=3)
            self.engine.post_user_message(self.gid,'queued request')
            self.store.memory.update(item['id'],{'status':'pending'},item['version'])
        finally:release.set()
        self.wait()
        deadline=time.time()+5
        while self.engine.state(other)['running'] and time.time()<deadline:time.sleep(.01)
        self.assertFalse(self.engine.state(other)['running'])
        for prompt in self.reg.prompts_of('gpt'):self.assertNotIn('SHARED_SYNTHETIC_SECRET',prompt)
        self.assertGreaterEqual(len(self.reg.prompts_of('claude')),3)
        self.assertNotIn('SHARED_SYNTHETIC_SECRET',self.reg.prompts_of('claude')[-1])

class ProcessOwnership(unittest.TestCase):
    def test_second_server_same_directory_refused(self):
        root=Path(__file__).resolve().parents[1]
        with secure_temporary_directory() as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            cmd=[sys.executable,'run.py','--demo','--no-browser','--data-dir',directory,'--port',str(port)]
            first=subprocess.Popen(cmd,cwd=root,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            second=None
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=.1).close();break
                    except OSError:time.sleep(.05)
                else:self.fail('first server not ready')
                second=subprocess.Popen(cmd,cwd=root,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
                try:_,err=second.communicate(timeout=2)
                except subprocess.TimeoutExpired:self.fail('second writer was accepted')
                self.assertNotEqual(second.returncode,0)
                self.assertIn('数据目录',err)
            finally:
                for proc in (second,first):
                    if proc and proc.poll() is None:proc.terminate();proc.wait(timeout=5)
                if second and second.stderr:second.stderr.close()

    def test_aliases_independent_directories_and_release_on_exception(self):
        from aigc.security import DataDirectoryLock
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=root/'data';other=root/'other'
            with DataDirectoryLock(data):
                with DataDirectoryLock(other):pass
                for alias in (data,root/'data'/'..'/'data'):
                    with self.assertRaises(RuntimeError):
                        with DataDirectoryLock(alias):pass
                link=root/'alias';link.symlink_to(data,target_is_directory=True)
                with self.assertRaises(RuntimeError):
                    with DataDirectoryLock(link):pass
            try:
                with DataDirectoryLock(data):raise ValueError('synthetic startup failure')
            except ValueError:pass
            with DataDirectoryLock(data):pass

    def test_concurrent_processes_and_crash_release(self):
        root=Path(__file__).resolve().parents[1]
        code = """import sys,time
from pathlib import Path
from aigc.security import DataDirectoryLock
root=Path(sys.argv[1])
while not (root/'go').exists():time.sleep(.01)
try:
 with DataDirectoryLock(root/'data'):
  print('owned',flush=True)
  sys.stdin.read()
except RuntimeError:
 print('busy',flush=True)
 sys.exit(2)
"""
        with tempfile.TemporaryDirectory() as temporary:
            procs=[subprocess.Popen([sys.executable,'-c',code,temporary],cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
            try:
                Path(temporary,'go').touch()
                results=[p.stdout.readline().strip() for p in procs]
                self.assertEqual(results.count('owned'),1,results)
                self.assertEqual(results.count('busy'),3,results)
                owner=procs[results.index('owned')];owner.kill();owner.wait(timeout=3)
                from aigc.security import DataDirectoryLock
                with DataDirectoryLock(Path(temporary)/'data'):pass
            finally:
                for proc in procs:
                    if proc.poll() is None:proc.kill()
                    proc.communicate(timeout=3)

    def test_run_startup_failure_releases_lock(self):
        import run
        from aigc.security import DataDirectoryLock
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(run,'Store',side_effect=OSError('synthetic failed startup')):
                with self.assertRaisesRegex(SystemExit, "synthetic failed startup"):run.main(['--demo','--no-browser','--data-dir',temporary])
            with DataDirectoryLock(temporary):pass

    def test_distinct_directories_run_simultaneously_and_normal_exit_releases(self):
        from aigc.security import DataDirectoryLock
        root=Path(__file__).resolve().parents[1]
        code="from aigc.security import DataDirectoryLock; import sys; lock=DataDirectoryLock(sys.argv[1]); lock.__enter__(); print('owned',flush=True); sys.stdin.read(); lock.__exit__()"
        with tempfile.TemporaryDirectory() as temporary:
            paths=[Path(temporary)/name for name in ('a','b')]
            procs=[subprocess.Popen([sys.executable,'-c',code,str(path)],cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for path in paths]
            try:
                for proc in procs:self.assertEqual(proc.stdout.readline().strip(),'owned')
                self.assertTrue(all(proc.poll() is None for proc in procs))
                for proc in procs:
                    proc.communicate(timeout=3);self.assertEqual(proc.returncode,0)
                for path in paths:
                    with DataDirectoryLock(path):pass
            finally:
                for proc in procs:
                    if proc.poll() is None:proc.kill()
                    proc.communicate(timeout=3)

class ProductConfirmation(unittest.TestCase):
    def test_single_member_decision_and_task_require_user_confirmation(self):
        from aigc import board
        b=board.new_board()
        board.apply_status(b,'claude',{'propose_decisions':['synthetic decision'],'propose_tasks':[{'title':'synthetic task','owner':'claude'}],'confirmed_by':'user'},['claude'])
        self.assertEqual(b['decisions'][0]['status'],'discussing')
        self.assertEqual(b['tasks'][0]['status'],'proposed')
        for item in b['decisions']+b['tasks']:
            self.assertNotEqual(item.get('confirmed_by'),'user')
            board.user_action(b,'force',{'id':item['id']},['claude'])
            self.assertEqual(item['confirmed_by'],'user')
        self.assertIn('群主已确认',board.render_markdown(b,['claude']))

    def test_consensus_is_not_user_confirmation_and_legacy_is_unknown(self):
        from aigc import board
        b=board.new_board();members=['claude','gpt']
        board.apply_status(b,'claude',{'propose_decisions':['synthetic']},members)
        board.apply_status(b,'gpt',{'votes':[{'id':'D-001','vote':'agree'}]},members)
        d=b['decisions'][0]
        self.assertEqual(d['confirmed_by'],'ai_consensus')
        self.assertIn('未经群主确认',board.render_markdown(b,members))
        d.pop('confirmed_by');board.upgrade(b)
        self.assertEqual(d['confirmed_by'],'legacy_unknown')
        self.assertIn('确认来源待核',board.render_markdown(b,members))
        board.user_action(b,'force',{'id':d['id']},members)
        board.user_action(b,'comment',{'id':d['id'],'text':'reconsider'},members)
        self.assertIsNone(d['confirmed_by'])

    def test_new_defaults_keep_network_capabilities_enabled(self):
        from aigc.store import DEFAULT_SETTINGS
        self.assertTrue(all(c['web'] for c in DEFAULT_SETTINGS['members'].values()))

if __name__=='__main__':unittest.main(verbosity=2)
