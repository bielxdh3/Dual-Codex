from __future__ import annotations

import http.client
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen
from unittest.mock import patch

from dual_codex.config import load_config
from dual_codex.dashboard import DashboardServer, DashboardService


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "repo").mkdir()
        self.config_path = root / "config.toml"
        self.config_path.write_text(
            """[orchestrator]
repository = "repo"
runs_dir = "runs"
codex_command = "missing-codex-for-dashboard-test"

[accounts.primary]
label = "Primary"
codex_home = "profiles/primary"
model = ""
reasoning_effort = "high"
backend = "windows"

[roles]
architect = "primary"
executor = "primary"
""",
            encoding="utf-8",
        )
        self.config = load_config(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_old_config_gets_empty_service_tier_and_settings_persist(self) -> None:
        service = DashboardService(self.config)
        saved = service.save_settings(
            "primary",
            {"model": "gpt-test", "reasoning_effort": "medium", "service_tier": "fast", "scope": "future_turns"},
        )
        self.assertFalse(saved["current_thread_changed"])
        reloaded = load_config(self.config_path)
        self.assertEqual(reloaded.accounts["primary"].model, "gpt-test")
        self.assertEqual(reloaded.accounts["primary"].reasoning_effort, "medium")
        self.assertEqual(reloaded.accounts["primary"].service_tier, "fast")
        self.assertNotIn(b"\xef\xbb\xbf", self.config_path.read_bytes()[:3])

    def test_current_thread_scope_is_explicitly_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DashboardService(self.config).save_settings("primary", {"scope": "current_thread"})

    def test_server_smoke_and_security_boundary(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            with urlopen(server.url, timeout=3) as response:
                html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Dual Codex", html)
            with urlopen(server.url + "api/status", timeout=3) as response:
                status = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(status["schema_version"], 1)
            with urlopen(server.url + "api/accounts", timeout=3) as response:
                accounts = json.loads(response.read())
            self.assertEqual(accounts["accounts"][0]["name"], "primary")
            self.assertNotIn("auth", json.dumps(accounts).lower())

            connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=3)
            connection.request("GET", "/api/accounts/primary/settings")
            self.assertEqual(connection.getresponse().status, 405)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=3)
            connection.putrequest("GET", "/api/status", skip_host=True)
            connection.putheader("Host", "evil.example")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_patch_rejects_unknown_account_and_arbitrary_path(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/accounts/does-not-exist/settings",
                data=b'{"model":"x"}',
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with self.assertRaises(Exception):
                urlopen(request, timeout=3)
            request = Request(
                server.url + "api/accounts/primary/settings",
                data=b'{"command":"dir"}',
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with self.assertRaises(Exception):
                urlopen(request, timeout=3)
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_mocked_app_server_telemetry_keeps_dynamic_buckets_and_capabilities(self) -> None:
        self.config.accounts["primary"] = replace(self.config.accounts["primary"], backend="app_server")

        def call(*, method, **_kwargs):
            return {
                "model/list": {"data": [{"id": "model-a", "displayName": "Model A", "description": "", "isDefault": True, "hidden": False, "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}], "serviceTiers": [{"id": "fast", "name": "Fast", "description": ""}]}]},
                "account/read": {"account": {"type": "chatgpt", "email": "user@example.com", "planType": "plus"}},
                "account/rateLimits/read": {"rateLimitsByLimitId": {"five_hour": {"limitName": "Five hour", "primary": {"usedPercent": 42, "resetsAt": 123}}, "weekly": {"secondary": {"usedPercent": 7}}}},
                "account/usage/read": {"summary": {"lifetimeTokens": 123}, "dailyUsageBuckets": [{"startDate": "2026-08-08", "tokens": 12}]},
                "thread/list": {"data": []},
            }.get(method, {})

        with patch("dual_codex.dashboard.app_server_call", side_effect=call), patch("dual_codex.dashboard.app_server_events", return_value=[]), patch("dual_codex.dashboard.login_status", return_value="OK"):
            account = DashboardService(self.config).collect_account("primary", force=True)
        self.assertEqual(account["effective"]["model"], "model-a")
        self.assertEqual([row["id"] for row in account["rate_limits"]], ["five_hour", "weekly"])
        self.assertEqual(account["usage"]["summary"]["lifetimeTokens"], 123)
        self.assertTrue(account["capabilities"]["service_tier"])
        self.assertEqual(account["runtime_state"], "Idle")


if __name__ == "__main__":
    unittest.main()
