import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.cli as cli  # noqa: E402


class CliOpenCliTests(unittest.TestCase):
    def test_parser_accepts_wx_remainder_args(self):
        parser = cli.build_parser()
        args = parser.parse_args(
            ["-u", "alice", "wx", "--timeout-seconds", "90", "--", "history", "文件传输助手", "--json"]
        )

        self.assertEqual(args.command, "wx")
        self.assertEqual(args.command_name, "wx")
        self.assertEqual(args.timeout_seconds, 90)
        self.assertEqual(args.opencli_args, ["--", "history", "文件传输助手", "--json"])

    def test_cmd_opencli_run_prefixes_wx_and_forwards_payload(self):
        captured = {}

        def fake_req(method, url, headers=None, data=None, params=None, timeout=30):
            captured.update(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "data": data,
                    "params": params,
                    "timeout": timeout,
                }
            )
            return 200, {"ok": True}

        parser = cli.build_parser()
        args = parser.parse_args(["-u", "alice", "wx", "--", "history", "文件传输助手", "--json"])

        with patch.object(cli, "_req", side_effect=fake_req):
            with patch.object(cli, "_pp") as pretty:
                with patch.object(cli, "_check_token"):
                    cli.cmd_opencli_run(args)

        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/harness/opencli/run"))
        self.assertEqual(captured["headers"]["X-User-Id"], "alice")
        self.assertEqual(captured["data"]["args"], ["wx", "history", "文件传输助手", "--json"])
        self.assertEqual(captured["data"]["timeout_seconds"], 60)
        pretty.assert_called_once_with({"ok": True})

    def test_cmd_opencli_run_keeps_generic_opencli_args(self):
        captured = {}

        def fake_req(method, url, headers=None, data=None, params=None, timeout=30):
            captured["data"] = data
            return 200, {"ok": True}

        parser = cli.build_parser()
        args = parser.parse_args(["opencli", "--", "docker", "ps"])

        with patch.object(cli, "_req", side_effect=fake_req):
            with patch.object(cli, "_pp"):
                with patch.object(cli, "_check_token"):
                    cli.cmd_opencli_run(args)

        self.assertEqual(captured["data"]["args"], ["docker", "ps"])


if __name__ == "__main__":
    unittest.main()
