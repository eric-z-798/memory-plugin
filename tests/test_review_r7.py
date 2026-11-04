"""Synthetic HOME fallback and cleanup identity boundaries; no real HOME writes."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import secure_fixtures
from aigc.security import PrivateStartupFiles

class FixtureHome(unittest.TestCase):
    def test_no_workspace_tmp_checkout_uses_safe_home_without_override(self):
        with secure_fixtures.secure_temporary_directory() as directory, tempfile.TemporaryDirectory() as checkout:
            home=Path(directory)/'synthetic-home';home.mkdir(mode=0o700)
            with patch.object(secure_fixtures,'WORKSPACE',Path(directory)/'absent-workspace'), patch.object(secure_fixtures,'__file__',str(Path(checkout)/'tests/secure_fixtures.py')), patch.dict(os.environ,{'HOME':str(home)}):
                os.environ.pop('XINGSHU_TEST_SECURE_ROOT',None)
                with secure_fixtures.secure_temporary_directory() as fixture:
                    self.assertEqual(Path(fixture).parent,home)
                    self.assertEqual(Path(fixture).stat().st_mode&0o777,0o700)
                self.assertEqual(list(home.iterdir()),[])

    def test_unavailable_home_does_not_break_explicit_safe_root(self):
        with secure_fixtures.secure_temporary_directory() as directory:
            with patch.dict(os.environ,{'XINGSHU_TEST_SECURE_ROOT':directory}), patch.object(Path,'home',side_effect=RuntimeError('synthetic missing HOME')):
                with secure_fixtures.secure_temporary_directory() as fixture:
                    self.assertEqual(Path(fixture).parent,Path(directory))

    def test_unsafe_home_remains_unchanged_and_is_rejected(self):
        with secure_fixtures.secure_temporary_directory() as directory, tempfile.TemporaryDirectory() as checkout:
            home=Path(directory)/'synthetic-home';home.mkdir();home.chmod(0o775)
            with patch.object(secure_fixtures,'WORKSPACE',Path(directory)/'absent-workspace'), patch.object(secure_fixtures,'__file__',str(Path(checkout)/'tests/secure_fixtures.py')), patch.dict(os.environ,{'HOME':str(home)}):
                os.environ.pop('XINGSHU_TEST_SECURE_ROOT',None)
                with self.assertRaisesRegex(RuntimeError,'XINGSHU_TEST_SECURE_ROOT'):
                    secure_fixtures.secure_temporary_directory()
                self.assertEqual(home.stat().st_mode&0o777,0o775)
                self.assertEqual(list(home.iterdir()),[])

class CleanupIdentity(unittest.TestCase):
    def test_replaced_directory_symlink_is_not_followed_or_removed(self):
        with secure_fixtures.secure_temporary_directory() as directory:
            root=Path(directory);startup=PrivateStartupFiles(root,'synthetic');startup.__enter__()
            original=startup.path;held=root/'held';original.rename(held)
            target=root/'other';target.mkdir();marker=target/'keep';marker.write_text('synthetic untouched')
            original.symlink_to(target,target_is_directory=True)
            startup.__exit__()
            self.assertTrue(original.is_symlink());self.assertEqual(marker.read_text(),'synthetic untouched')
            self.assertTrue((held/'startup-url.txt').exists())

    def test_replaced_file_symlink_and_owner_mismatch_are_not_deleted(self):
        with secure_fixtures.secure_temporary_directory() as directory:
            root=Path(directory);startup=PrivateStartupFiles(root,'synthetic');startup.__enter__()
            target=root/'keep';target.write_text('synthetic untouched')
            startup.link.unlink();startup.link.symlink_to(target)
            real=Path.lstat
            def changed_owner(path,*args,**kwargs):
                info=real(path,*args,**kwargs)
                if path==startup.page:
                    return SimpleNamespace(st_ino=info.st_ino,st_mode=info.st_mode,st_uid=os.getuid()+1)
                return info
            with patch.object(Path,'lstat',changed_owner):startup.__exit__()
            self.assertTrue(startup.link.is_symlink());self.assertTrue(startup.page.exists())
            self.assertEqual(target.read_text(),'synthetic untouched')

if __name__=='__main__':unittest.main(verbosity=2)
