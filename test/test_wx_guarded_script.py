import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "wx_guarded.py"


def load_script():
    spec = importlib.util.spec_from_file_location("wx_guarded_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class WxGuardedScriptTests(unittest.TestCase):
    def test_forwards_wx_args_through_guarded_bridge(self):
        module = load_script()
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"ok": True, "stdout": '{"ok":true}\n', "stderr": "", "returncode": 0}

        stdout = io.StringIO()
        with patch.object(module, "run_opencli_command", side_effect=fake_run):
            with patch("sys.stdout", stdout):
                rc = module.main(["--timeout-seconds", "7", "--", "history", "文件传输助手", "--json"])

        self.assertEqual(rc, 0)
        self.assertEqual(captured["args"], ["wx", "history", "文件传输助手", "--json"])
        self.assertEqual(captured["kwargs"]["timeout_seconds"], 7)
        self.assertEqual(stdout.getvalue(), '{"ok":true}\n')

    def test_health_uses_redacted_status_path(self):
        module = load_script()
        stdout = io.StringIO()
        with patch.object(module, "get_opencli_status", return_value={"wx_health": {"known_message_key_count": 1}}):
            with patch("sys.stdout", stdout):
                rc = module.main(["--health"])

        self.assertEqual(rc, 0)
        self.assertIn("known_message_key_count", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
