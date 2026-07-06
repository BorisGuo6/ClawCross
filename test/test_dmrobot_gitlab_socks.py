import argparse
import plistlib
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import dmrobot_gitlab_socks as socks  # noqa: E402


def make_args(**overrides):
    data = {
        "remote": "lenovo@100.77.85.105",
        "listen_host": "127.0.0.1",
        "listen_port": 18080,
        "ssh_bin": "/usr/bin/ssh",
        "ssh_config": "/dev/null",
        "connect_timeout": 20,
        "server_alive_interval": 30,
        "server_alive_count_max": 2,
        "verify_timeout": 0.1,
        "json": True,
        "curl_bin": "curl",
        "git_bin": "git",
        "probe_url": "http://gitlab.dmrobot.com/users/sign_in",
        "repo": "http://gitlab.dmrobot.com/shenrui.liu/tacsim_collect.git",
        "branch": "RobOmni_v1.0",
        "check_timeout": 15,
        "interval": 120,
        "check": True,
        "restart_on_failed_check": True,
        "once": True,
        "label": "com.example.dmrobot-gitlab-socks",
        "load": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


class DmrobotGitlabSocksTests(unittest.TestCase):
    def test_build_ssh_command_uses_dynamic_forward_and_safety_options(self):
        command = socks.build_ssh_command(make_args())

        self.assertEqual(command[:4], ["/usr/bin/ssh", "-f", "-N", "-D"])
        self.assertIn("127.0.0.1:18080", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ServerAliveInterval=30", command)
        self.assertIn("ServerAliveCountMax=2", command)
        self.assertEqual(command[-1], "lenovo@100.77.85.105")

    def test_start_reuses_existing_managed_listener(self):
        listener = socks.Listener(
            command="ssh",
            pid=31674,
            user="boris",
            name="TCP 127.0.0.1:18080 (LISTEN)",
            process_command="/usr/bin/ssh -f -N -D 127.0.0.1:18080 lenovo@100.77.85.105",
        )
        with mock.patch.object(socks, "listeners_for_port", return_value=[listener]), mock.patch.object(socks, "write_pid_file"):
            payload = socks.start_tunnel(make_args())

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["already_running"])
        self.assertEqual(payload["managed_pids"], [31674])

    def test_check_tunnel_does_not_require_ensure_attribute(self):
        args = make_args()
        with mock.patch.object(
            socks,
            "payload_status",
            return_value={"managed_pids": [31674], "listeners": [], "ok": True},
        ), mock.patch.object(socks, "_run", return_value=socks.CommandResult(0, "ok", "")):
            payload = socks.check_tunnel(args)

        self.assertTrue(payload["ok"])

    def test_install_launch_agent_writes_daemon_plist(self):
        args = make_args()
        with mock.patch.object(Path, "home", return_value=PROJECT_ROOT):
            path = socks.install_launch_agent(args)

        try:
            payload = plistlib.loads(path.read_bytes())
            program = payload["ProgramArguments"]
            self.assertEqual(payload["Label"], "com.example.dmrobot-gitlab-socks")
            self.assertIn("dmrobot_gitlab_socks.py", program[1])
            self.assertIn("daemon", program)
            self.assertIn("lenovo@100.77.85.105", program)
            self.assertEqual(payload["RunAtLoad"], True)
            self.assertEqual(payload["KeepAlive"], True)
        finally:
            if path.exists():
                path.unlink()
            launch_dir = PROJECT_ROOT / "Library" / "LaunchAgents"
            if launch_dir.exists():
                try:
                    launch_dir.rmdir()
                    launch_dir.parent.rmdir()
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
