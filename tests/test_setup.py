"""Local setup tests use fake terminals, installers and login commands only."""

import copy
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aigc import setup, setup_worker


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aigc-setup-test-")
        self.root = Path(self.tmp.name)
        self.manager = setup.SetupManager(SimpleNamespace(root=self.root), object())
        self.settings = {"membership_only": True, "proxy": {"mode": "custom", "url": "http://127.0.0.1:7890"},
                         "members": {member: {"command": ""} for member in ("claude", "gpt", "gemini")}}

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, member="claude", action="login", found="/fake/cli"):
        with mock.patch.object(setup, "_terminal", return_value=("/fake/terminal", "")), \
                mock.patch.object(setup, "find_command", return_value=found), \
                mock.patch.object(setup, "_launch_terminal") as launch:
            result = self.manager.start(member, action, self.settings)
        return result, launch

    def test_demo_remote_and_headless_never_launch(self):
        for options in ({"demo": True}, {"local_only": False}, {}):
            with self.subTest(options=options):
                manager = setup.SetupManager(SimpleNamespace(root=self.root), object(), **options)
                with mock.patch.object(setup, "_terminal", return_value=(None, "headless")), \
                        mock.patch.object(setup, "_launch_terminal") as launch:
                    with self.assertRaises(ValueError):
                        manager.start("claude", "setup", self.settings)
                    self.assertFalse(launch.called)
        self.assertFalse((self.root / "setup-jobs").exists())

    def test_overview_is_discovery_only_with_copyable_commands(self):
        with mock.patch.object(setup, "_terminal", return_value=(None, "headless")), \
                mock.patch.object(setup, "find_command", return_value=None), \
                mock.patch.object(setup, "_platform", return_value="linux"), \
                mock.patch.object(setup.subprocess, "Popen") as process:
            overview = self.manager.overview(self.settings)
        self.assertFalse(overview["can_launch"])
        self.assertEqual(overview["members"]["claude"]["login_command"], "claude auth login")
        self.assertEqual(overview["members"]["gpt"]["login_command"], "codex login")
        self.assertEqual(overview["members"]["gemini"]["login_command"], "agy")
        self.assertIn("https://claude.ai/install.sh", overview["members"]["claude"]["install_command"])
        self.assertFalse(process.called)

    def test_job_files_never_save_environment_or_api_credentials(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-api-value", "OTHER_SECRET": "private-secret-value"}):
            job, launch = self.launch()
        self.assertRegex(job["id"], r"^[a-f0-9]{32}$")
        self.assertEqual(job["status"], "launching")
        directory = self.manager.root / job["id"]
        saved = "\n".join(path.read_text() for path in directory.iterdir())
        self.assertNotIn("secret-api-value", saved)
        self.assertNotIn("private-secret-value", saved)
        self.assertNotIn("ANTHROPIC_API_KEY", launch.call_args.args[2])
        config = json.loads((directory / "config.json").read_text())
        self.assertEqual(set(config), {"member", "action", "command", "proxy", "membership_only"})
        self.assertEqual(config["proxy"], self.settings["proxy"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((directory / "config.json").stat().st_mode), 0o600)

    def test_duplicate_member_jobs_rejected_and_cancel_is_marker_only(self):
        job, _ = self.launch()
        with self.assertRaises(ValueError):
            self.launch()
        with mock.patch.object(setup.subprocess, "Popen") as process:
            pending = self.manager.cancel(job["id"])
        self.assertTrue(pending["cancel_requested"])
        self.assertTrue((self.manager.root / job["id"] / "cancel").exists())
        self.assertFalse(process.called)
        self.assertEqual(self.manager.job(job["id"])["status"], "launching")
        self.assertTrue(self.manager.job(job["id"])["cancel_requested"])

    def test_login_requires_installed_cli_and_invalid_actions_create_no_job(self):
        for member, action in (("unknown", "setup"), ("claude", "rm")):
            with self.assertRaises(ValueError):
                self.manager.start(member, action, self.settings)
        with self.assertRaises(ValueError):
            self.launch(found=None)
        self.assertFalse(self.manager.root.exists())

    def test_job_id_traversal_rejected(self):
        for value in ("../settings", "" , None, "a" * 33, "A" * 32):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.job(value)

    def test_terminal_launch_failure_is_terminal_status(self):
        with mock.patch.object(setup, "_terminal", return_value=("/fake/terminal", "")), \
                mock.patch.object(setup, "find_command", return_value="/fake/cli"), \
                mock.patch.object(setup, "_launch_terminal", side_effect=OSError("private auth details")):
            job = self.manager.start("claude", "login", self.settings)
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("private", json.dumps(job))

    def test_stale_job_is_not_mistaken_for_success(self):
        job, _ = self.launch()
        job["updated_at"] = time.time() - 100
        setup._write_private_json(self.manager.root / job["id"] / "state.json", job)
        self.assertEqual(self.manager.job(job["id"])["status"], "failed")

    def test_native_terminal_arguments_preserve_paths_as_arguments(self):
        directory = self.root / "space ' quote & $path"
        directory.mkdir()
        with mock.patch.object(setup, "_platform", return_value="linux"), \
                mock.patch.object(setup.subprocess, "Popen") as process:
            setup._launch_terminal("/usr/bin/gnome-terminal", directory, {})
        argv = process.call_args.args[0]
        self.assertEqual(argv[:2], ["/usr/bin/gnome-terminal", "--"])
        self.assertEqual(argv[-1], str(directory))
        self.assertNotIn("shell", process.call_args.kwargs)
        with mock.patch.object(setup, "_platform", return_value="macos"), \
                mock.patch.object(setup.subprocess, "Popen") as process:
            setup._launch_terminal("macos", directory, {})
        self.assertEqual(process.call_args.args[0], ["/usr/bin/open", "-a", "Terminal", str(directory / "launch.command")])
        import shlex
        script = (directory / "launch.command").read_text().splitlines()[1]
        self.assertEqual(shlex.split(script)[-1], str(directory))
        with mock.patch.object(setup, "_platform", return_value="windows"), \
                mock.patch.object(setup.subprocess, "Popen") as process:
            setup._launch_terminal("windows", directory, {})
        self.assertEqual(process.call_args.args[0][-1], str(directory))
        self.assertEqual(process.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
        self.assertNotIn("shell", process.call_args.kwargs)


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aigc-worker-test-")
        self.directory = Path(self.tmp.name) / ("a" * 32)
        self.directory.mkdir()
        self.config = {"member": "claude", "action": "login", "command": "/fake/claude",
                       "proxy": {"mode": "direct", "url": ""}, "membership_only": True}
        self.write_config()

    def write_config(self):
        setup._write_private_json(self.directory / "config.json", self.config)
        setup._write_private_json(self.directory / "state.json", {"id": self.directory.name, "member": self.config["member"],
                    "action": self.config["action"], "status": "launching", "created_at": time.time(), "updated_at": time.time()})

    def tearDown(self):
        self.tmp.cleanup()

    def test_login_commands_do_not_send_prompts_or_capture_output(self):
        for member, args in setup.LOGIN_ARGS.items():
            with self.subTest(member=member):
                self.config.update(member=member, action="login", command="/fake/" + member)
                self.write_config()
                with mock.patch.object(setup_worker, "find_command", return_value="/fake/" + member), \
                        mock.patch.object(setup_worker, "_download_installer") as download, \
                        mock.patch.object(setup_worker, "_run_child") as child, \
                        mock.patch("builtins.print"):
                    setup_worker.perform(self.directory)
                self.assertEqual(child.call_args.args[0], ["/fake/" + member] + args)
                self.assertFalse(download.called)
                self.assertEqual(setup._read_json(self.directory / "state.json")["status"], "completed")
                self.assertIn("不代表", setup._read_json(self.directory / "state.json")["message"])

    def test_setup_existing_cli_skips_install_and_missing_cli_installs_then_logs_in(self):
        self.config["action"] = "setup"
        self.write_config()
        installer = self.directory / "official-install.sh"
        installer.write_text("fake")
        with mock.patch.object(setup_worker, "find_command", side_effect=[None, "/new/claude"]), \
                mock.patch.object(setup_worker, "_download_installer", return_value=installer), \
                mock.patch.object(setup_worker, "_installer_command", return_value=["/fake/bash", str(installer)]), \
                mock.patch.object(setup_worker, "_run_child") as child, mock.patch("builtins.print"):
            setup_worker.perform(self.directory)
        self.assertEqual(child.call_count, 2)
        self.assertEqual(child.call_args.args[0], ["/new/claude", "auth", "login"])
        self.assertFalse(installer.exists())
        with mock.patch.object(setup_worker, "find_command", return_value="/new/claude"), \
                mock.patch.object(setup_worker, "_download_installer") as download, \
                mock.patch.object(setup_worker, "_run_child") as child, mock.patch("builtins.print"):
            setup_worker.perform(self.directory)
        self.assertFalse(download.called)
        self.assertEqual(child.call_count, 1)

    def test_cancel_before_start_executes_nothing(self):
        (self.directory / "cancel").touch()
        with mock.patch.object(setup_worker, "find_command", return_value="/fake/cli"), \
                mock.patch.object(setup_worker.subprocess, "Popen") as process:
            with self.assertRaises(setup_worker.SetupStopped):
                setup_worker.perform(self.directory)
        self.assertFalse(process.called)

    def test_child_inherits_terminal_and_failure_does_not_capture_auth_output(self):
        proc = mock.Mock(returncode=1)
        proc.poll.return_value = 1
        with mock.patch.object(setup_worker.subprocess, "Popen", return_value=proc) as process:
            with self.assertRaises(setup_worker.SetupFailure):
                setup_worker._run_child(["/fake/cli", "login"], self.directory, {}, 30, "登录中")
        self.assertNotIn("stdout", process.call_args.kwargs)
        self.assertNotIn("stderr", process.call_args.kwargs)
        self.assertNotIn("stdin", process.call_args.kwargs)
        self.assertNotIn("shell", process.call_args.kwargs)

    def test_cancel_running_child_stops_only_owned_process(self):
        proc = mock.Mock()
        def poll():
            (self.directory / "cancel").touch()
            return None
        proc.poll.side_effect = poll
        with mock.patch.object(setup_worker.subprocess, "Popen", return_value=proc), \
                mock.patch.object(setup_worker, "_stop_child") as stop:
            with self.assertRaises(setup_worker.SetupStopped):
                setup_worker._run_child(["/fake/cli", "login"], self.directory, {}, 30, "登录中")
        stop.assert_called_once_with(proc)

    def test_child_timeout_stops_owned_process(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(setup_worker.subprocess, "Popen", return_value=proc), \
                mock.patch.object(setup_worker, "_stop_child") as stop:
            with self.assertRaises(setup_worker.SetupFailure):
                setup_worker._run_child(["/fake/cli", "login"], self.directory, {}, 0, "登录中")
        stop.assert_called_once_with(proc)

    def test_installer_downloader_only_uses_fixed_https_source_and_size_limit(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://claude.ai/install.sh"
        response.read.side_effect = [b"fake installer", b""]
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(setup_worker, "_platform", return_value="linux"), \
                mock.patch.object(setup_worker.urllib.request, "build_opener", return_value=opener), mock.patch("builtins.print"):
            path = setup_worker._download_installer("claude", self.directory, {})
        self.assertEqual(opener.open.call_args.args[0], "https://claude.ai/install.sh")
        self.assertEqual(path.read_bytes(), b"fake installer")
        response.read.side_effect = [b"x" * 11]
        with mock.patch.object(setup_worker, "MAX_INSTALLER_BYTES", 10), \
                mock.patch.object(setup_worker.urllib.request, "build_opener", return_value=opener), mock.patch("builtins.print"):
            with self.assertRaises(setup_worker.SetupFailure):
                setup_worker._download_installer("claude", self.directory, {})

    def test_https_redirect_cannot_downgrade(self):
        handler = setup_worker.HTTPSRedirectsOnly()
        with self.assertRaises(setup_worker.SetupFailure):
            handler.redirect_request(None, None, 302, "", {}, "http://example.invalid/install.sh")

    def test_blocked_download_times_out_and_late_result_never_writes_script(self):
        gate = threading.Event()
        exited = threading.Event()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://claude.ai/install.sh"
        response.read.side_effect = [b"not executed", b""]
        def slow_open(*args, **kwargs):
            gate.wait(2)
            exited.set()
            return response
        opener = mock.Mock()
        opener.open.side_effect = slow_open
        with mock.patch.object(setup_worker, "DOWNLOAD_TIMEOUT", .03), \
                mock.patch.object(setup_worker.urllib.request, "build_opener", return_value=opener), mock.patch("builtins.print"):
            started = time.monotonic()
            with self.assertRaises(setup_worker.SetupFailure):
                setup_worker._download_installer("claude", self.directory, {})
            self.assertLess(time.monotonic() - started, .5)
            snapshot = setup._read_json(self.directory / "state.json")
            gate.set()
            self.assertTrue(exited.wait(1))
            time.sleep(.03)
        self.assertFalse((self.directory / "official-install.sh").exists())
        self.assertEqual(setup._read_json(self.directory / "state.json"), snapshot)

    def test_cancel_during_download_never_executes_late_script(self):
        self.config["action"] = "setup"
        self.write_config()
        gate = threading.Event()
        started = threading.Event()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://claude.ai/install.sh"
        response.read.side_effect = [b"not executed", b""]
        def slow_open(*args, **kwargs):
            started.set()
            gate.wait(2)
            return response
        opener = mock.Mock()
        opener.open.side_effect = slow_open
        def stop():
            if started.wait(1):
                (self.directory / "cancel").touch()
        stopper = threading.Thread(target=stop)
        stopper.start()
        try:
            with mock.patch.object(setup_worker, "find_command", return_value=None), \
                    mock.patch.object(setup_worker.urllib.request, "build_opener", return_value=opener), \
                    mock.patch.object(setup_worker, "_run_child") as child, \
                    mock.patch.object(setup_worker.sys.stdin, "isatty", return_value=False), mock.patch("builtins.print"):
                self.assertEqual(setup_worker.main([str(self.directory)]), 1)
                self.assertFalse(child.called)
                gate.set()
                time.sleep(.03)
            self.assertEqual(setup._read_json(self.directory / "state.json")["status"], "cancelled")
            self.assertFalse((self.directory / "official-install.sh").exists())
        finally:
            gate.set()
            stopper.join(timeout=2)

    def test_windows_installer_wrapper_passes_path_separately_and_never_embeds_proxy(self):
        installer = self.directory / "space ' installer.ps1"
        with mock.patch.object(setup_worker, "_platform", return_value="windows"), \
                mock.patch.object(setup_worker.shutil, "which", return_value="powershell.exe"):
            argv = setup_worker._installer_command("claude", installer, self.directory, "custom")
        self.assertEqual(argv[-1], str(installer))
        self.assertNotIn("-Command", argv)
        script = (self.directory / "install-wrapper.ps1").read_text()
        self.assertIn("Invoke-WebRequest:Proxy", script)
        self.assertIn("$env:HTTPS_PROXY", script)
        self.assertNotIn("7890", script)

    def test_worker_generic_error_does_not_persist_exception_details(self):
        with mock.patch.object(setup_worker, "perform", side_effect=RuntimeError("access-token-secret")), \
                mock.patch.object(setup_worker.sys.stdin, "isatty", return_value=False), mock.patch("builtins.print"):
            result = setup_worker.main([str(self.directory)])
        self.assertEqual(result, 1)
        state = setup._read_json(self.directory / "state.json")
        self.assertEqual(state["status"], "failed")
        self.assertNotIn("access-token-secret", json.dumps(state))

    @unittest.skipUnless(sys.platform.startswith("linux"), "benign process-tree fixture requires Linux /proc")
    def test_cancel_force_stops_descendant_even_after_parent_exits(self):
        marker = self.directory / "descendant.pid"
        child = "import os,signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(%r).write_text(str(os.getpid()));time.sleep(30)" % str(marker)
        parent_code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',%r]);time.sleep(30)" % child
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), "benign descendant did not start")
            child_pid = int(marker.read_text())
            setup_worker._stop_child(parent)
            self.assertIsNotNone(parent.poll())
            deadline = time.monotonic() + 2
            state = ""
            while time.monotonic() < deadline:
                try:
                    state = Path("/proc/%d/stat" % child_pid).read_text().rpartition(")")[2].split()[0]
                except FileNotFoundError:
                    state = "gone"
                if state in ("gone", "Z"):
                    break
                time.sleep(.01)
            self.assertIn(state, ("gone", "Z"), "TERM-resistant descendant must not keep running")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            if child_pid is not None:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_changed_process_identity_is_never_signalled(self):
        with mock.patch.object(setup_worker, "_process_identity", return_value=("proc", "new-start")), \
                mock.patch.object(setup_worker.os, "kill") as kill:
            setup_worker._signal_same_process(12345, ("proc", "old-start"), 9)
        self.assertFalse(kill.called)


if __name__ == "__main__":
    unittest.main()
