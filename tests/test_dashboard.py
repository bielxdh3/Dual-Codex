from __future__ import annotations

import http.client
import json
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.request import Request, urlopen
from unittest.mock import patch

from dual_codex.config import load_config
from dual_codex.dashboard import (
    CAPABILITY_SCRIPT,
    HTML,
    LIVE_EXECUTOR_SCRIPT,
    SCRIPT,
    STYLES,
    DashboardError,
    DashboardServer,
    DashboardService,
)
from dual_codex.live_events import LiveEventJournal


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

[accounts.secondary]
label = "Secondary"
codex_home = "profiles/secondary"
model = ""
reasoning_effort = "high"
backend = "windows"

[roles]
orchestrator = "primary"
architect = "primary"
reviewer = "secondary"
executor = "secondary"
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

    def test_role_assignment_api_uses_registry_validation(self) -> None:
        result = DashboardService(self.config).assign({"role": "executor", "account": "primary"})
        self.assertEqual(result["account"], "primary")
        self.assertEqual(load_config(self.config_path).roles["executor"], "primary")

    def test_role_set_updates_complete_map_and_transfers_roles(self) -> None:
        result = DashboardService(self.config).set_roles(
            {"account": "primary", "roles": ["orchestrator", "architect", "reviewer"]}
        )
        self.assertEqual(result["message"], "Roles updated")
        self.assertEqual(
            result["roles"],
            {"orchestrator": "primary", "architect": "primary", "reviewer": "primary", "executor": "secondary"},
        )
        reloaded = load_config(self.config_path)
        self.assertEqual(
            reloaded.roles,
            {"orchestrator": "primary", "architect": "primary", "reviewer": "primary", "executor": "secondary"},
        )

    def test_role_set_rejects_invalid_input_without_persisting(self) -> None:
        service = DashboardService(self.config)
        original = self.config_path.read_bytes()
        for body in (
            {"account": "missing", "roles": []},
            {"account": "primary", "roles": ["unknown"]},
            {"account": "primary", "roles": ["architect", "architect"]},
            {"account": "primary", "roles": "architect"},
        ):
            with self.subTest(body=body):
                with self.assertRaises((DashboardError, ValueError)):
                    service.set_roles(body)
                self.assertEqual(self.config_path.read_bytes(), original)

    def test_role_set_api_returns_resulting_role_map(self) -> None:
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/roles/set",
                data=json.dumps({"account": "primary", "roles": ["orchestrator", "architect", "reviewer"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["message"], "Roles updated")
            self.assertEqual(payload["roles"]["reviewer"], "primary")
            self.assertEqual(load_config(self.config_path).roles["executor"], "secondary")
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_model_linked_frontend_capabilities_reconcile_live_selection(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the capability helper regression test")
        source = CAPABILITY_SCRIPT + """
const capabilityHelper = globalThis.dualCodexDashboardCapabilities;
const models = [
  {id: 'model-a', is_default: true, default_reasoning: 'medium', reasoning_efforts: ['low', 'medium'], default_service_tier: 'fast', service_tiers: [{id: 'fast', name: 'Fast'}]},
  {id: 'model-b', is_default: false, default_reasoning: 'max', reasoning_efforts: ['high', 'max'], default_service_tier: 'premium', service_tiers: [{id: 'premium', name: 'Premium'}]},
];
console.log(JSON.stringify({
  changed: capabilityHelper.reconcileCapabilitySelection(models, 'model-b', 'medium', 'fast'),
  inherit: capabilityHelper.reconcileCapabilitySelection(models, '', 'ultra', 'fast'),
  missingDefault: capabilityHelper.reconcileCapabilitySelection(models.map(model => ({...model, is_default: false})), '', 'high', 'fast'),
}));
"""
        result = subprocess.run([node, "-"], input=source, text=True, capture_output=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["changed"]["selected_model"], "model-b")
        self.assertEqual(payload["changed"]["reasoning_value"], "max")
        self.assertEqual(payload["changed"]["service_tier_value"], "")
        self.assertIn("Reasoning updated", payload["changed"]["message"])
        self.assertIn("Service tier reset", payload["changed"]["message"])
        self.assertEqual(payload["inherit"]["selected_model"], "model-a")
        self.assertEqual(payload["inherit"]["reasoning_value"], "medium")
        self.assertFalse(payload["inherit"]["reasoning_disabled"])
        self.assertIsNone(payload["missingDefault"]["selected_model"])
        self.assertTrue(payload["missingDefault"]["reasoning_disabled"])

    def test_backend_rejects_invalid_model_reasoning_and_tier_combinations(self) -> None:
        service = DashboardService(self.config)
        catalog = {
            "models": [
                {"id": "model-a", "is_default": True, "reasoning_efforts": ["low", "medium"], "service_tiers": [{"id": "fast"}]},
                {"id": "model-b", "is_default": False, "reasoning_efforts": ["high"], "service_tiers": [{"id": "premium"}]},
            ]
        }
        with patch.object(service, "collect_account", return_value=catalog):
            with self.assertRaises(DashboardError):
                service.save_settings(
                    "primary",
                    {"model": "model-b", "reasoning_effort": "medium", "service_tier": "premium"},
                )
            with self.assertRaises(DashboardError):
                service.save_settings(
                    "primary",
                    {"model": "model-b", "reasoning_effort": "high", "service_tier": "fast"},
                )

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

    def test_live_executor_snapshot_is_path_derived_and_bounded(self) -> None:
        service = DashboardService(self.config)
        idle = service.live_executor_snapshot()
        self.assertEqual(idle["state"], "IDLE")
        self.assertEqual(idle["cursor"], 0)

        journal = LiveEventJournal(
            self.config.runs_dir,
            account="secondary",
            role="executor",
            repository=self.config.repository,
            run_id="run-1",
            request_id="request-1",
            max_records=20,
            max_record_bytes=2048,
        )
        journal.append_notification(
            "turn/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "turn": {
                    "id": "turn-1",
                    "model": "model-live",
                    "reasoningEffort": "high",
                    "serviceTier": "fast",
                },
            },
        )
        journal.append_notification(
            "item/commandExecution/outputDelta",
            {"threadId": "thread-1", "turnId": "turn-1", "output": "safe output"},
        )
        journal.append_notification(
            "item/fileChange/updated",
            {"threadId": "thread-1", "turnId": "turn-1", "diff": "diff --git a/a b/a"},
        )
        journal.append_notification(
            "item/agentMessage/delta",
            {"threadId": "thread-1", "turnId": "turn-1", "delta": "safe message"},
        )
        journal.append_notification(
            "thread/tokenUsage/updated",
            {"threadId": "thread-1", "tokenUsage": {"total": 42}},
        )
        journal.append_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        )

        snapshot = service.live_executor_snapshot()
        self.assertEqual(snapshot["state"], "COMPLETE")
        self.assertEqual(snapshot["request_id"], "request-1")
        self.assertEqual(snapshot["run_id"], "run-1")
        self.assertEqual(snapshot["thread_id"], "thread-1")
        self.assertEqual(snapshot["turn_id"], "turn-1")
        self.assertEqual(snapshot["model"], "model-live")
        self.assertEqual(snapshot["reasoning_effort"], "high")
        self.assertEqual(snapshot["service_tier"], "fast")
        self.assertEqual(snapshot["token_usage"]["total"], 42)
        self.assertTrue(snapshot["activity"]["commands"])
        self.assertTrue(snapshot["activity"]["outputs"])
        self.assertTrue(snapshot["activity"]["file_changes"])
        self.assertTrue(snapshot["activity"]["diffs"])
        self.assertTrue(snapshot["activity"]["messages"])
        self.assertNotIn("path", snapshot)

    def test_live_executor_snapshot_keeps_run_times_and_nested_token_totals(self) -> None:
        long_repo = self.config.repository.parent / "long-repo"
        long_repo.mkdir()
        long_config = replace(self.config, repository=long_repo)
        journal = LiveEventJournal(
            long_config.runs_dir,
            account="secondary",
            role="executor",
            repository=long_repo,
            run_id="run-long",
            request_id="request-long",
            max_records=300,
            max_record_bytes=2048,
        )
        started = journal.append_notification(
            "turn/started",
            {"threadId": "thread-long", "turnId": "turn-long", "turn": {"id": "turn-long"}},
        )[0]
        journal.append_notification(
            "thread/tokenUsage/updated",
            {
                "threadId": "thread-long",
                "tokenUsage": {"total": {"totalTokens": 1234}, "last": {"totalTokens": 56}},
            },
        )
        for index in range(130):
            journal.append(kind="notification", state="observed", method=f"future/{index}")
        completed = journal.append_notification(
            "turn/completed",
            {"threadId": "thread-long", "turn": {"id": "turn-long", "status": "completed"}},
        )[0]

        snapshot = DashboardService(long_config).live_executor_snapshot()
        self.assertEqual(snapshot["state"], "COMPLETE")
        self.assertEqual(len(snapshot["events"]), 128)
        self.assertEqual(snapshot["started_at"], started.timestamp)
        self.assertEqual(snapshot["completed_at"], completed.timestamp)
        self.assertEqual(snapshot["token_usage"]["total"]["totalTokens"], 1234)
        self.assertEqual(snapshot["token_usage"]["last"]["totalTokens"], 56)

    def test_live_executor_sse_replays_from_cursor_and_keeps_loopback_boundary(self) -> None:
        journal = LiveEventJournal(
            self.config.runs_dir,
            account="secondary",
            role="executor",
            repository=self.config.repository,
            run_id="run-1",
            request_id="request-1",
            max_records=20,
            max_record_bytes=2048,
        )
        journal.append(kind="notification", state="one", method="future/one")
        journal.append(kind="notification", state="two", method="future/two")
        server = DashboardServer(self.config)
        thread = server.serve_in_thread()
        try:
            request = Request(
                server.url + "api/live-executor/events?path=outside.jsonl",
                headers={"Last-Event-ID": "1"},
            )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"].split(";", 1)[0], "text/event-stream")
                events: list[dict[str, str]] = []
                current: dict[str, str] = {}
                while len(events) < 2:
                    line = response.readline().decode("utf-8")
                    self.assertTrue(line)
                    if line == "\n":
                        events.append(current)
                        current = {}
                    elif line.startswith("event: "):
                        current["event"] = line[7:].strip()
                    elif line.startswith("id: "):
                        current["id"] = line[4:].strip()
                self.assertEqual(events[0], {"event": "snapshot", "id": "1"})
                self.assertEqual(events[1], {"event": "live", "id": "2"})

            connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=3)
            connection.putrequest("GET", "/api/live-executor/events", skip_host=True)
            connection.putheader("Host", "evil.example")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
            thread.join(timeout=3)

    def test_live_executor_ui_is_bounded_cli_like_and_text_safe(self) -> None:
        for marker in (
            "EXECUTOR LIVE",
            "Follow Live",
            "Pause",
            "Clear View",
            "CURRENT PLAN",
            "Live Diff",
        ):
            self.assertIn(marker, HTML)
        self.assertIn("ui-monospace", STYLES)
        self.assertIn("EventSource", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("EXECUTOR_MAX_ROWS=128", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("textContent", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("replaceChildren", LIVE_EXECUTOR_SCRIPT)
        self.assertNotIn("executor-feed.innerHTML", LIVE_EXECUTOR_SCRIPT)
        self.assertIn("totalTokens", SCRIPT)
        self.assertIn("started_at", SCRIPT)
        self.assertIn("completed_at", SCRIPT)


if __name__ == "__main__":
    unittest.main()
