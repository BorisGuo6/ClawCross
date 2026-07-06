import unittest
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from services.anyrouter_autolog_service import masked_config, run_check_in


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    def __init__(self):
        self.cookies = {}
        self.get_count = 0
        self.calls = []
        self.closed = False

    def get(self, url, headers):
        self.calls.append(("GET", url, dict(headers)))
        self.get_count += 1
        quota = 1_000_000 if self.get_count == 1 else 1_350_000
        return FakeResponse(200, {"success": True, "data": {"quota": quota, "used_quota": 0}})

    def post(self, url, headers):
        self.calls.append(("POST", url, dict(headers)))
        return FakeResponse(200, {"success": True, "message": "ok"})

    def close(self):
        self.closed = True


class AnyRouterAutologServiceTests(unittest.TestCase):
    def test_run_check_in_reports_quota_delta(self):
        fake_client = FakeClient()
        result = run_check_in(
            {
                "accounts": [
                    {
                        "name": "primary",
                        "provider": "anyrouter",
                        "api_user": "user-123",
                        "cookies": {"session": "abc", "acw_tc": "waf"},
                    }
                ]
            },
            client_factory=lambda: fake_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["results"][0]["delta"]["check_in_reward"], 0.7)
        self.assertEqual([call[0] for call in fake_client.calls], ["GET", "POST", "GET"])
        self.assertTrue(fake_client.closed)

    def test_masked_config_hides_cookie_and_api_user(self):
        masked = masked_config({
            "accounts": [
                {
                    "name": "primary",
                    "provider": "anyrouter",
                    "api_user": "user123456789",
                    "cookies": {"session": "abcdefghijklmnop"},
                }
            ],
            "providers": {},
        })

        account = masked["accounts"][0]
        self.assertEqual(account["api_user"], "user****6789")
        self.assertEqual(account["cookies"]["session"], "abcd****mnop")


if __name__ == "__main__":
    unittest.main()
