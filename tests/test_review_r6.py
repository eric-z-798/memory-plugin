"""Synthetic startup capabilities, strict permissions and explicit migration."""
import copy
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from aigc.security import DataDirectoryLock, PrivateStartupFiles, validate_command
from aigc.store import Store, DEFAULT_SETTINGS
from secure_fixtures import secure_temporary_directory
import run

class StartupFiles(unittest.TestCase):
    def test_private_files_cleanup_and_escaped_url(self):
        with secure_temporary_directory() as tmp:
            url='http://localhost:1234/#token=synthetic&group=example'
            with PrivateStartupFiles(tmp,url) as startup:
                path=startup.path
                self.assertEqual(path.stat().st_mode & 0o777,0o700)
                for file in (startup.page,startup.link):
                    self.assertEqual(file.stat().st_mode & 0o777,0o600)
                self.assertEqual(startup.link.read_text(),url+'\n')
                self.assertIn('&amp;group=',startup.page.read_text())
                self.assertNotIn('synthetic',startup.page.as_uri())
            self.assertFalse(path.exists())

    def test_preexisting_file_or_symlink_never_overwritten(self):
        with secure_temporary_directory() as tmp:
            root=Path(tmp); target=root/'untouched';target.write_text('synthetic original')
            for symlink in (False, True):
                made=root/('private-'+str(symlink));made.mkdir(mode=0o700)
                path=made/'startup-url.txt'
                if symlink:path.symlink_to(target)
                else:path.write_text('existing')
                with patch('tempfile.mkdtemp',return_value=str(made)):
                    with self.assertRaises(FileExistsError):
                        with PrivateStartupFiles(root,'synthetic'):pass
                self.assertEqual(target.read_text(),'synthetic original')
                self.assertTrue(path.is_symlink() if symlink else path.read_text()=='existing')

    def test_new_directory_0700_under_umask_002_existing_0775_rejected(self):
        with secure_temporary_directory() as tmp:
            root=Path(tmp); old=os.umask(0o002)
            try:
                with DataDirectoryLock(root/'new'/'nested'):
                    self.assertEqual((root/'new').stat().st_mode & 0o777,0o700)
                    self.assertEqual((root/'new'/'nested').stat().st_mode & 0o777,0o700)
            finally:os.umask(old)
            (root/'old').mkdir();(root/'old').chmod(0o775)
            with self.assertRaisesRegex(RuntimeError,'不会自动修改'):
                with DataDirectoryLock(root/'old'):pass
            self.assertEqual((root/'old').stat().st_mode & 0o777,0o775)
            # Same primary group is NOT evidence that group write is safe.
            self.assertEqual((root/'old').stat().st_gid,os.getgid())

    def test_startup_failure_is_friendly_and_does_not_chmod(self):
        with secure_temporary_directory() as tmp:
            path=Path(tmp);path.chmod(0o777)
            with self.assertRaises(SystemExit) as caught:run.main(['--demo','--data-dir',tmp,'--no-browser'])
            self.assertIn('无法启动',str(caught.exception))
            self.assertNotIn('Traceback',str(caught.exception))
            self.assertEqual(path.stat().st_mode & 0o777,0o777)

    def test_redirected_output_and_browser_argument_exclude_token(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with secure_temporary_directory() as tmp:
            server=Mock();server.serve_forever.side_effect=KeyboardInterrupt()
            output=io.StringIO();captured=[]
            class ImmediateTimer:
                def __init__(self, delay, callback):self.callback=callback
                def start(self):self.callback()
                def cancel(self):pass
                def join(self,timeout=None):pass
            def opened(url):
                captured.append(url)
                self.assertTrue(url.startswith('file://'))
                self.assertNotIn('synthetic-token',url)
            args=SimpleNamespace(demo=True,password='',host='127.0.0.1',port=8765,no_browser=False)
            with patch('aigc.server.secrets.token_urlsafe',return_value='synthetic-token'), patch.object(run,'bind',return_value=(server,8765)), patch.object(run,'has_display',return_value=True), patch.object(run.threading,'Timer',ImmediateTimer), patch.object(run.webbrowser,'open',side_effect=opened), redirect_stdout(output):
                run.serve(args,True,Path(tmp))
            self.assertTrue(captured)
            self.assertNotIn('synthetic-token',output.getvalue())
            self.assertIn('startup-url.txt',output.getvalue())
            self.assertEqual(list(Path(tmp).glob('.startup-*')),[])

    def test_interactive_terminal_gets_link_without_browser_token_argument(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with secure_temporary_directory() as tmp:
            output=io.StringIO();output.isatty=lambda:True
            server=Mock();server.serve_forever.side_effect=KeyboardInterrupt()
            args=SimpleNamespace(demo=True,password='',host='127.0.0.1',port=8765,no_browser=True)
            with patch('aigc.server.secrets.token_urlsafe',return_value='synthetic-interactive'), patch.object(run,'bind',return_value=(server,8765)), patch.object(run.webbrowser,'open') as opened, redirect_stdout(output):
                run.serve(args,True,Path(tmp))
            self.assertIn('#token=synthetic-interactive',output.getvalue())
            opened.assert_not_called()
            self.assertEqual(list(Path(tmp).glob('.startup-*')),[])


class WebMigration(unittest.TestCase):
    def test_unknown_legacy_defaults_open_once_then_explicit_false_survives(self):
        with secure_temporary_directory() as tmp:
            store=Store(Path(tmp));old=copy.deepcopy(DEFAULT_SETTINGS);old['members']['gpt']['web']=False
            path=Path(tmp)/'settings.json';path.write_text(json.dumps(old))
            migrated=store.load_settings()
            self.assertTrue(migrated['members']['gpt']['web'])
            self.assertEqual(migrated['web_migration']['source'],'legacy_unknown')
            self.assertFalse(migrated['web_migration']['acknowledged'])
            self.assertNotIn('gpt',migrated['web_explicit'])
            original=path.read_bytes();store.load_settings();self.assertEqual(path.read_bytes(),original)
            store.save_settings({'members':{'gpt':{'web':False}},'acknowledge_web_migration':True})
            restarted=Store(Path(tmp)).load_settings()
            self.assertFalse(restarted['members']['gpt']['web'])
            self.assertIn('gpt',restarted['web_explicit'])
            self.assertTrue(restarted['web_migration']['acknowledged'])

    def test_new_install_and_known_versioned_choice(self):
        with secure_temporary_directory() as tmp:
            store=Store(Path(tmp));settings=store.load_settings()
            self.assertTrue(all(c['web'] for c in settings['members'].values()))
            self.assertNotIn('web_migration',settings)
            store.save_settings({'members':{'gpt':{'web':False}}})
            self.assertFalse(Store(Path(tmp)).load_settings()['members']['gpt']['web'])

if __name__=='__main__':unittest.main(verbosity=2)
