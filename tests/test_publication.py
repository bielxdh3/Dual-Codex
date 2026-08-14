from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from dual_codex.delegation import MissionAuthorization
from dual_codex.process import CommandResult
from dual_codex.publication import (
    PublicationError,
    PublicationRequest,
    execute_publication,
    publication_request_from_delegation,
    publication_request_from_json,
)


OLD_SHA = "4a604e43a57ba8368fca35e72ff10dff4e271c71"
NEW_SHA = "44be672e9639e19406a606b8badf32bbf31f94ba"
OTHER_SHA = "28747c6ebdac873650e2d5a3c6193824e7cc9985"


class FakeHostRunner:
    def __init__(self, repository: Path, *, remote_sha: str = OLD_SHA, push_returncode: int = 0, remote_url: str = "https://github.com/bielxdh3/root.ark.git", identity: str = "bielxdh3", can_push: bool = True, auth_status: int = 0):
        self.repository = repository
        self.remote_sha = remote_sha
        self.push_returncode = push_returncode
        self.remote_url = remote_url
        self.identity = identity
        self.can_push = can_push
        self.auth_status = auth_status
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, command, *, cwd, env=None, check=False, **kwargs):
        command = [str(item) for item in command]
        self.commands.append(command)
        self.envs.append(env)
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return CommandResult(command, 0, str(self.repository), "")
        if command[:3] == ["git", "remote", "get-url"]:
            return CommandResult(command, 0, self.remote_url + "\n", "")
        if command[:2] == ["git", "rev-parse"] and any(item.startswith("refs/heads/") for item in command):
            return CommandResult(command, 0, NEW_SHA + "\n", "")
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return CommandResult(command, 0, NEW_SHA + "\n", "")
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return CommandResult(command, 0, "", "")
        if "ls-remote" in command:
            branch = command[-1]
            return CommandResult(command, 0, f"{self.remote_sha}\t{branch}\n", "")
        if "push" in command:
            if self.push_returncode == 0:
                self.remote_sha = NEW_SHA
            return CommandResult(command, self.push_returncode, "", "Authorization: bearer secret" if self.push_returncode else "")
        if command[:4] == ["gh", "auth", "status", "--active"]:
            return CommandResult(command, self.auth_status, "", "")
        if command[:3] == ["gh", "api", "user"]:
            return CommandResult(command, 0, self.identity + "\n", "")
        if command[:3] == ["gh", "api", "repos/bielxdh3/root.ark"] and "pulls" not in command:
            push = "true" if self.can_push else "null"
            return CommandResult(command, 0, '{"full_name":"bielxdh3/root.ark","permissions":{"push":' + push + '}}\n', "")
        if command[:3] == ["gh", "api", "repos/bielxdh3/root.ark/pulls/54"]:
            return CommandResult(command, 0, '{"number":54,"state":"open","draft":true,"headSha":"' + OLD_SHA + '","baseRef":"Root/main"}\n', "")
        if "repos/bielxdh3/root.ark/pulls" in command and "POST" in command:
            return CommandResult(command, 0, '{"number":55,"state":"open","draft":true,"headRef":"cdx/test","baseRef":"Root/main"}\n', "")
        if command[:3] == ["gh", "api", "--method"]:
            return CommandResult(command, 0, '{"number":54,"state":"open","draft":true,"headSha":"' + OLD_SHA + '","baseRef":"Root/main"}\n', "")
        raise AssertionError(f"unexpected command: {command}")


def _auth(*actions: str) -> MissionAuthorization:
    return MissionAuthorization(frozenset(actions))


def _push_request(root: Path, *, auth: MissionAuthorization | None = None, old: str = OLD_SHA, new: str = NEW_SHA) -> PublicationRequest:
    return PublicationRequest(
        operation="normal_push",
        mission_id="mission-1",
        repository=root,
        repository_full_name="bielxdh3/root.ark",
        authorization=auth or _auth("normal_push"),
        authorization_reference="mission-1",
        branch="cdx/rootark-roadmap-evidence",
        expected_remote_old_sha=old,
        new_sha=new,
    )


class PublicationTests(unittest.TestCase):
    def test_normal_push_is_typed_and_fast_forward_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = FakeHostRunner(root)
            result = execute_publication(_push_request(root), runner=runner)
            self.assertEqual(result.status, "completed")
            self.assertTrue(result.fast_forward)
            push = next(command for command in runner.commands if "push" in command)
            self.assertIn("--no-force", push)
            self.assertNotIn("--force", [item for item in push if item == "--force"])
            self.assertIn("http.sslBackend=openssl", push)
            self.assertIn("http.sslVerify=true", push)
            self.assertIn("http.extraHeader=", push)

    def test_host_network_environment_is_noninteractive_and_token_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = FakeHostRunner(root)
            old = os.environ.get("GH_TOKEN")
            os.environ["GH_TOKEN"] = "test-secret-that-must-not-cross"
            try:
                result = execute_publication(_push_request(root), runner=runner)
            finally:
                if old is None:
                    os.environ.pop("GH_TOKEN", None)
                else:
                    os.environ["GH_TOKEN"] = old
            self.assertEqual(result.status, "completed")
            network_envs = [
                entry
                for command, entry in zip(runner.commands, runner.envs)
                if command
                and command[0] in {"gh", "git"}
                and ("ls-remote" in command or "push" in command or command[:3] == ["gh", "auth", "status"])
            ]
            self.assertTrue(network_envs)
            for env in network_envs:
                self.assertIsNotNone(env)
                self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
                self.assertEqual(env.get("GCM_INTERACTIVE"), "Never")
                self.assertNotIn("GH_TOKEN", env)

    def test_remote_head_mismatch_is_cas_block_and_does_not_push(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = FakeHostRunner(root, remote_sha=OTHER_SHA)
            result = execute_publication(_push_request(root), runner=runner)
            self.assertEqual(result.error_classification, "REMOTE_HEAD_MOVED")
            self.assertFalse(any("push" in command for command in runner.commands))

    def test_non_descendant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            class NonDescendant(FakeHostRunner):
                def __call__(self, command, **kwargs):
                    if list(command)[:3] == ["git", "merge-base", "--is-ancestor"]:
                        self.commands.append([str(item) for item in command])
                        return CommandResult([str(item) for item in command], 1, "", "")
                    return super().__call__(command, **kwargs)
            result = execute_publication(_push_request(root), runner=NonDescendant(root))
            self.assertEqual(result.error_classification, "NON_FAST_FORWARD")

    def test_authorization_is_deny_by_default_and_operation_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(PublicationError):
                _push_request(root, auth=_auth())
            with self.assertRaises(PublicationError):
                PublicationRequest(
                    operation="draft_pr_update",
                    mission_id="mission-1",
                    repository=root,
                    repository_full_name="bielxdh3/root.ark",
                    authorization=_auth("normal_push"),
                    authorization_reference="mission-1",
                    pr_number=54,
                    expected_pr_head_sha=OLD_SHA,
                    expected_pr_base="Root/main",
                    body="truthful update",
                )

    def test_unknown_fields_and_force_operation_are_rejected(self) -> None:
        base = {
            "operation": "normal_push",
            "mission_id": "mission-1",
            "repository": "C:/repo",
            "repository_full_name": "bielxdh3/root.ark",
            "authorization": {"allowed_actions": ["normal_push"]},
            "authorization_reference": "mission-1",
            "branch": "main",
            "expected_remote_old_sha": OLD_SHA,
            "new_sha": NEW_SHA,
        }
        with self.assertRaises(PublicationError):
            publication_request_from_json({**base, "command": "git push --force"})
        with self.assertRaises(PublicationError):
            publication_request_from_json({**base, "operation": "force_push"})
        with self.assertRaises(PublicationError):
            publication_request_from_json({**base, "operation": []})
        with self.assertRaises(PublicationError):
            publication_request_from_json({**base, "branch": 42})
        with self.assertRaises(PublicationError):
            publication_request_from_json({**base, "authorization": {"allowed_actions": ["unknown_action"]}})

    def test_secret_like_pr_content_is_rejected_before_host_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(PublicationError):
                PublicationRequest(
                    operation="draft_pr_update",
                    mission_id="mission-1",
                    repository=root,
                    repository_full_name="bielxdh3/root.ark",
                    authorization=_auth("draft_pr_update"),
                    authorization_reference="mission-1",
                    pr_number=54,
                    expected_pr_head_sha=OLD_SHA,
                    expected_pr_base="Root/main",
                    body="Authorization: bearer gho_abcdefghijklmnopqrstuvwxyz",
                )

    def test_wrong_repository_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = FakeHostRunner(root, remote_url="https://github.com/other/repo.git")
            result = execute_publication(_push_request(root), runner=runner)
            self.assertEqual(result.error_classification, "REPOSITORY_SCOPE_DENIED")

    def test_push_url_cannot_redirect_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            class RedirectedPushUrl(FakeHostRunner):
                def __call__(self, command, **kwargs):
                    if list(command)[:4] == ["git", "remote", "get-url", "--push"]:
                        command = [str(item) for item in command]
                        self.commands.append(command)
                        self.envs.append(kwargs.get("env"))
                        return CommandResult(command, 0, "https://github.com/attacker/redirect.git\n", "")
                    return super().__call__(command, **kwargs)

            result = execute_publication(_push_request(root), runner=RedirectedPushUrl(root))
            self.assertEqual(result.error_classification, "REPOSITORY_SCOPE_DENIED")

    def test_push_failure_result_does_not_leak_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = execute_publication(_push_request(root), runner=FakeHostRunner(root, push_returncode=1))
            self.assertEqual(result.error_classification, "PUBLICATION_AUTHENTICATION_BLOCKED")
            self.assertNotIn("secret", json.dumps(result.as_dict()))
            self.assertNotIn("bearer", json.dumps(result.as_dict()).casefold())

    def test_host_identity_mismatch_and_write_permission_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mismatch = execute_publication(_push_request(root), runner=FakeHostRunner(root, identity="other-account"))
            self.assertEqual(mismatch.error_classification, "GITHUB_AUTH_IDENTITY_MISMATCH")
            no_write = execute_publication(_push_request(root), runner=FakeHostRunner(root, can_push=False))
            self.assertEqual(no_write.error_classification, "GITHUB_WRITE_PERMISSION_UNVERIFIED")
            unavailable = execute_publication(_push_request(root), runner=FakeHostRunner(root, auth_status=1))
            self.assertEqual(unavailable.error_classification, "HOST_GITHUB_AUTH_NOT_READY")

    def test_draft_pr_update_requires_open_draft_and_reverifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = PublicationRequest(
                operation="draft_pr_update",
                mission_id="mission-1",
                repository=root,
                repository_full_name="bielxdh3/root.ark",
                authorization=_auth("draft_pr_update"),
                authorization_reference="mission-1",
                pr_number=54,
                expected_pr_head_sha=OLD_SHA,
                expected_pr_base="Root/main",
                body="truthful update",
            )
            runner = FakeHostRunner(root)
            result = execute_publication(request, runner=runner)
            self.assertEqual(result.status, "completed")
            self.assertTrue(any("PATCH" in command for command in runner.commands))

    def test_draft_pr_update_expected_head_mismatch_blocks_without_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = PublicationRequest(
                operation="draft_pr_update",
                mission_id="mission-1",
                repository=root,
                repository_full_name="bielxdh3/root.ark",
                authorization=_auth("draft_pr_update"),
                authorization_reference="mission-1",
                pr_number=54,
                expected_pr_head_sha=OTHER_SHA,
                expected_pr_base="Root/main",
                body="truthful update",
            )
            runner = FakeHostRunner(root)
            result = execute_publication(request, runner=runner)
            self.assertEqual(result.error_classification, "REMOTE_HEAD_MOVED")
            self.assertFalse(any("PATCH" in command for command in runner.commands))

    def test_draft_pr_creation_cannot_be_non_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(PublicationError):
                PublicationRequest(
                    operation="draft_pr_create",
                    mission_id="mission-1",
                    repository=root,
                    repository_full_name="bielxdh3/root.ark",
                    authorization=_auth("draft_pr_create"),
                    authorization_reference="mission-1",
                    branch="feature/test",
                    base_branch="Root/main",
                    title="Draft",
                    draft=False,
                )

    def test_trusted_delegation_is_the_capability_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            delegation = SimpleNamespace(
                request_id="mission-1",
                repository=root,
                authorization=_auth("normal_push"),
            )
            request = publication_request_from_delegation(
                delegation,
                operation="normal_push",
                repository_full_name="bielxdh3/root.ark",
                branch="cdx/rootark-roadmap-evidence",
                expected_remote_old_sha=OLD_SHA,
                new_sha=NEW_SHA,
            )
            self.assertEqual(execute_publication(request, runner=FakeHostRunner(root)).status, "completed")
            with self.assertRaises(PublicationError):
                publication_request_from_delegation(
                    delegation,
                    operation="normal_push",
                    mission_id="other-mission",
                    repository_full_name="bielxdh3/root.ark",
                    branch="cdx/rootark-roadmap-evidence",
                    expected_remote_old_sha=OLD_SHA,
                    new_sha=NEW_SHA,
                )

    def test_draft_pr_creation_is_typed_and_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = PublicationRequest(
                operation="draft_pr_create",
                mission_id="mission-1",
                repository=root,
                repository_full_name="bielxdh3/root.ark",
                authorization=_auth("draft_pr_create"),
                authorization_reference="mission-1",
                branch="cdx/test",
                base_branch="Root/main",
                title="Draft",
                body="Documentation only",
            )
            result = execute_publication(request, runner=FakeHostRunner(root))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.pr_number, 55)


if __name__ == "__main__":
    unittest.main()
