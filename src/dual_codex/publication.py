"""Typed, host-side publication operations.

This module is intentionally separate from the App Server executor.  The
caller is the trusted local control plane; requests contain only non-secret
operation parameters and the existing request-scoped authorization allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlsplit

from .delegation import MissionAuthorization, PUBLICATION_ACTIONS
from .paths import same_path
from .process import CommandResult, run_command


PublicationOperation = Literal["normal_push", "create_branch", "draft_pr_create", "draft_pr_update"]
PUBLICATION_OPERATIONS = frozenset({"normal_push", "create_branch", "draft_pr_create", "draft_pr_update"})
_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
_MISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_REMOTE = re.compile(r"^[A-Za-z0-9._-]+$")
_REF_FORBIDDEN = ("..", "@{", "//", "\\", "~", "^", ":", "?", "*", "[", "]")
_SECRET_INPUT = re.compile(
    r"(?ix)(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|authorization\s*:\s*bearer\s+\S+|password\s*=\s*\S+)"
)
HOST_GITHUB_LOGIN = "bielxdh3"
HOST_GITHUB = "github.com"


class PublicationError(RuntimeError):
    """A safe, non-secret broker failure."""

    def __init__(self, classification: str, detail: str):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail


@dataclass(frozen=True)
class PublicationRequest:
    """A typed operation; arbitrary shell commands are not representable."""

    operation: PublicationOperation
    mission_id: str
    repository: Path
    repository_full_name: str
    authorization: MissionAuthorization
    authorization_reference: str = ""
    remote: str = "origin"
    branch: str = ""
    expected_remote_old_sha: str = ""
    expected_remote_state: str = ""
    new_sha: str = ""
    expected_base_sha: str = ""
    pr_number: int | None = None
    expected_pr_head_sha: str = ""
    expected_pr_state: str = "open"
    expected_pr_draft: bool = True
    expected_pr_base: str = ""
    title: str = ""
    body: str = ""
    base_branch: str = ""
    draft: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or self.operation not in PUBLICATION_OPERATIONS:
            raise PublicationError("UNSUPPORTED_PUBLICATION_OPERATION", "Unsupported publication operation.")
        if not isinstance(self.mission_id, str) or not _MISSION_ID.fullmatch(self.mission_id):
            raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Invalid mission identifier.")
        if not isinstance(self.repository, Path) or not self.repository.is_absolute():
            raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Repository path must be absolute.")
        if not isinstance(self.repository_full_name, str) or not _REPOSITORY_NAME.fullmatch(self.repository_full_name):
            raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Invalid repository identity.")
        if not _REMOTE.fullmatch(self.remote) or self.remote.startswith("-"):
            raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Invalid Git remote name.")
        if not isinstance(self.authorization, MissionAuthorization) or not self.authorization.allows(self.operation):
            raise PublicationError("PUBLICATION_AUTHORIZATION_DENIED", "Operation is not authorized for this mission.")
        if not _MISSION_ID.fullmatch(self.authorization_reference) or self.authorization_reference != self.mission_id:
            raise PublicationError("PUBLICATION_AUTHORIZATION_DENIED", "Authorization is not bound to this mission.")
        if self.operation == "normal_push":
            _validate_branch(self.branch)
            _validate_sha(self.expected_remote_old_sha)
            _validate_sha(self.new_sha)
        elif self.operation == "create_branch":
            _validate_branch(self.branch, classification="INVALID_BRANCH_REF")
            _validate_sha(self.new_sha)
            if self.expected_remote_state != "absent":
                raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "New branch publication requires expected remote state 'absent'.")
            if self.expected_remote_old_sha:
                raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "New branch publication cannot include an existing remote SHA.")
            if self.expected_base_sha:
                _validate_sha(self.expected_base_sha)
        elif self.operation == "draft_pr_update":
            if not isinstance(self.pr_number, int) or isinstance(self.pr_number, bool) or self.pr_number <= 0:
                raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "A positive PR number is required.")
            if self.expected_pr_head_sha:
                _validate_sha(self.expected_pr_head_sha)
            if self.expected_pr_base:
                _validate_branch(self.expected_pr_base)
            if self.expected_pr_state != "open" or self.expected_pr_draft is not True:
                raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Draft PR updates require open/draft expectations.")
            if not self.title and not self.body:
                raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "A title or body update is required.")
            _validate_text(self.title, "title", max_length=256, allow_empty=True)
            _validate_text(self.body, "body", max_length=100_000, allow_empty=True)
        else:
            _validate_branch(self.branch)
            _validate_branch(self.base_branch)
            if self.draft is not True:
                raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Draft PR creation must remain draft.")
            _validate_text(self.title, "title", max_length=256)
            _validate_text(self.body, "body", max_length=100_000, allow_empty=True)


@dataclass(frozen=True)
class PublicationResult:
    status: str
    operation: str
    repository: str
    repository_full_name: str
    mission_id: str
    branch: str = ""
    pr_number: int | None = None
    expected_remote_old_sha: str = ""
    observed_remote_old_sha: str = ""
    expected_remote_state: str = ""
    observed_pre_state: str = ""
    observed_remote_state: str = ""
    requested_new_sha: str = ""
    observed_remote_new_sha: str = ""
    created: bool = False
    fast_forward: bool = False
    remote_state: str = ""
    error_classification: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "repository": self.repository,
            "repository_full_name": self.repository_full_name,
            "mission_id": self.mission_id,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "expected_remote_old_sha": self.expected_remote_old_sha,
            "observed_remote_old_sha": self.observed_remote_old_sha,
            "expected_remote_state": self.expected_remote_state,
            "observed_pre_state": self.observed_pre_state,
            "observed_remote_state": self.observed_remote_state,
            "requested_new_sha": self.requested_new_sha,
            "observed_remote_new_sha": self.observed_remote_new_sha,
            "created": self.created,
            "fast_forward": self.fast_forward,
            "remote_state": self.remote_state,
            "error_classification": self.error_classification,
            "detail": self.detail,
        }


def _validate_sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Invalid commit SHA.")
    return value.lower()


def _validate_branch(value: str, *, classification: str = "MALFORMED_PUBLICATION_REQUEST") -> str:
    if (
        not isinstance(value, str)
        or not value
        or not _BRANCH.fullmatch(value)
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/"))
        or any(marker in value for marker in _REF_FORBIDDEN)
    ):
        raise PublicationError(classification, "Invalid Git branch name.")
    return value


def _validate_text(value: str, name: str, *, max_length: int, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > max_length
        or "\x00" in value
        or _SECRET_INPUT.search(value)
    ):
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", f"Invalid {name}.")
    return value


def _canonical_repository_url(url: str, repository_full_name: str) -> bool:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or parsed.hostname != HOST_GITHUB or parsed.username or parsed.password:
        return False
    if parsed.query or parsed.fragment:
        return False
    path = parsed.path.rstrip("/")
    return path.removesuffix(".git").lstrip("/").casefold() == repository_full_name.casefold()


def _host_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    env["GH_HOST"] = HOST_GITHUB
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        env.pop(name, None)
    return env


def _git_network_command(*args: str) -> list[str]:
    """Use certificate-validating OpenSSL and the host GCM, ignoring repo helpers."""

    return [
        "git",
        "-c",
        "http.sslBackend=openssl",
        "-c",
        "http.sslVerify=true",
        "-c",
        "http.extraHeader=",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=manager",
        *args,
    ]


Runner = Callable[..., CommandResult]


def _run(
    runner: Runner,
    command: list[str],
    *,
    cwd: Path,
    network: bool = False,
    stdin: str | None = None,
) -> CommandResult:
    env = _host_env() if network else None
    try:
        return runner(command, cwd=cwd, env=env, stdin=stdin, check=False)
    except (OSError, ValueError):
        return CommandResult(command, 126, "", "")


def _result(request: PublicationRequest, *, status: str, classification: str = "", detail: str = "", **kwargs: Any) -> PublicationResult:
    return PublicationResult(
        status=status,
        operation=request.operation,
        repository=str(request.repository),
        repository_full_name=request.repository_full_name,
        mission_id=request.mission_id,
        error_classification=classification,
        detail=detail,
        **kwargs,
    )


def _safe_repository_scope(
    request: PublicationRequest,
    runner: Runner,
    *,
    classification: str = "REPOSITORY_SCOPE_DENIED",
) -> None:
    repository = request.repository.expanduser().resolve()
    if not repository.is_dir():
        raise PublicationError(classification, "Repository path is not a directory.")
    root = _run(runner, ["git", "rev-parse", "--show-toplevel"], cwd=repository)
    if root.returncode != 0 or not same_path(repository, Path(root.stdout.strip())):
        raise PublicationError(classification, "Requested path is not the repository root.")
    for remote_args in (
        ["git", "remote", "get-url", request.remote],
        ["git", "remote", "get-url", "--push", request.remote],
    ):
        remote = _run(runner, remote_args, cwd=repository)
        if remote.returncode != 0 or not _canonical_repository_url(remote.stdout, request.repository_full_name):
            raise PublicationError(classification, "Remote is not the authorized GitHub repository.")


def _host_authentication(
    runner: Runner,
    repository: Path,
    full_name: str,
    *,
    write_classification: str = "GITHUB_WRITE_PERMISSION_UNVERIFIED",
) -> None:
    status = _run(runner, ["gh", "auth", "status", "--active", "--hostname", HOST_GITHUB], cwd=repository, network=True)
    if status.returncode != 0:
        raise PublicationError("HOST_GITHUB_AUTH_NOT_READY", "Host GitHub authentication is unavailable.")
    identity = _run(runner, ["gh", "api", "user", "--jq", ".login"], cwd=repository, network=True)
    if identity.returncode != 0 or identity.stdout.strip() != HOST_GITHUB_LOGIN:
        raise PublicationError("GITHUB_AUTH_IDENTITY_MISMATCH", "Authenticated GitHub identity is not the authorized account.")
    permissions = _run(
        runner,
        ["gh", "api", f"repos/{full_name}", "--jq", "{full_name,permissions}"],
        cwd=repository,
        network=True,
    )
    if permissions.returncode != 0:
        raise PublicationError(write_classification, "Repository permission probe failed.")
    try:
        value = json.loads(permissions.stdout)
    except (TypeError, ValueError):
        value = {}
    if value.get("full_name", "").casefold() != full_name.casefold() or value.get("permissions", {}).get("push") is not True:
        raise PublicationError(write_classification, "Authenticated repository write permission was not proven.")


def _remote_ref_sha(request: PublicationRequest, runner: Runner) -> str | None:
    result = _run(
        runner,
        _git_network_command(
            "ls-remote",
            "--heads",
            request.remote,
            f"refs/heads/{request.branch}",
        ),
        cwd=request.repository,
        network=True,
    )
    if result.returncode != 0:
        raise PublicationError("PUBLICATION_CONNECTIVITY_BLOCKED", "Remote branch read failed.")
    lines = [item for item in result.stdout.splitlines() if item.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise PublicationError("REMOTE_STATE_UNREADABLE", "Remote branch state was not safely parsed.")
    line = lines[0]
    parts = line.split()
    if len(parts) != 2 or parts[1] != f"refs/heads/{request.branch}" or not _SHA.fullmatch(parts[0]):
        raise PublicationError("REMOTE_STATE_UNREADABLE", "Remote branch state was not safely parsed.")
    return parts[0].lower()


def _remote_sha(request: PublicationRequest, runner: Runner) -> str:
    value = _remote_ref_sha(request, runner)
    if value is None:
        raise PublicationError("REMOTE_STATE_UNREADABLE", "Remote branch state was not safely parsed.")
    return value


def _verify_push_inputs(request: PublicationRequest, runner: Runner) -> None:
    local_ref = _run(runner, ["git", "rev-parse", f"refs/heads/{request.branch}"], cwd=request.repository)
    if local_ref.returncode != 0 or local_ref.stdout.strip().lower() != request.new_sha.lower():
        raise PublicationError("LOCAL_REF_MISMATCH", "Local branch does not equal the requested new SHA.")
    object_check = _run(runner, ["git", "rev-parse", "--verify", f"{request.new_sha}^{{commit}}"], cwd=request.repository)
    if object_check.returncode != 0:
        raise PublicationError("LOCAL_OBJECT_MISSING", "Requested commit object is unavailable locally.")
    ancestry = _run(
        runner,
        ["git", "merge-base", "--is-ancestor", request.expected_remote_old_sha, request.new_sha],
        cwd=request.repository,
    )
    if ancestry.returncode != 0:
        raise PublicationError("NON_FAST_FORWARD", "Requested new SHA does not descend from the expected remote SHA.")


def _execute_push(request: PublicationRequest, runner: Runner) -> PublicationResult:
    _safe_repository_scope(request, runner)
    _verify_push_inputs(request, runner)
    observed_old = _remote_sha(request, runner)
    if observed_old != request.expected_remote_old_sha.lower():
        return _result(
            request,
            status="blocked",
            classification="REMOTE_HEAD_MOVED",
            detail="Remote branch no longer matches the expected old SHA.",
            expected_remote_old_sha=request.expected_remote_old_sha.lower(),
            observed_remote_old_sha=observed_old,
            requested_new_sha=request.new_sha.lower(),
            remote_state="unchanged",
        )
    _host_authentication(runner, request.repository, request.repository_full_name)
    # A non-force create refspec is advertised with a zero old-id; receive-pack
    # rejects a branch that appears after the absent-state precheck instead of
    # fast-forwarding or overwriting it.
    push = _run(
        runner,
        _git_network_command(
            "push",
            "--no-force",
            "--porcelain",
            request.remote,
            f"refs/heads/{request.branch}:refs/heads/{request.branch}",
        ),
        cwd=request.repository,
        network=True,
    )
    if push.returncode != 0:
        classification = "PUBLICATION_AUTHENTICATION_BLOCKED" if any(
            marker in (push.stderr or "").casefold()
            for marker in ("credential", "authentication", "authorization", "bearer", "could not read username", "401", "403")
        ) else "PUBLICATION_CONNECTIVITY_BLOCKED"
        return _result(
            request,
            status="blocked",
            classification=classification,
            detail="Normal fast-forward push failed; no credential or command output was retained.",
            expected_remote_old_sha=request.expected_remote_old_sha.lower(),
            observed_remote_old_sha=observed_old,
            requested_new_sha=request.new_sha.lower(),
            remote_state="unchanged_or_unknown",
        )
    observed_new = _remote_sha(request, runner)
    if observed_new != request.new_sha.lower():
        return _result(
            request,
            status="blocked",
            classification="PUBLICATION_POST_VERIFY_FAILED",
            detail="Remote head did not equal the requested new SHA after push.",
            expected_remote_old_sha=request.expected_remote_old_sha.lower(),
            observed_remote_old_sha=observed_old,
            requested_new_sha=request.new_sha.lower(),
            observed_remote_new_sha=observed_new,
            fast_forward=True,
            remote_state="post_verify_failed",
        )
    return _result(
        request,
        status="completed",
        expected_remote_old_sha=request.expected_remote_old_sha.lower(),
        observed_remote_old_sha=observed_old,
        requested_new_sha=request.new_sha.lower(),
        observed_remote_new_sha=observed_new,
        fast_forward=True,
        remote_state="updated",
        detail="Normal fast-forward push completed and remote head was reverified.",
    )


def _validate_new_branch_ref(request: PublicationRequest, runner: Runner) -> None:
    if request.branch.casefold() == "head" or request.branch.casefold().startswith("refs/"):
        raise PublicationError("INVALID_BRANCH_REF", "New branch must be a non-special branch name.")
    checked = _run(runner, ["git", "check-ref-format", "--branch", request.branch], cwd=request.repository)
    if checked.returncode != 0:
        raise PublicationError("INVALID_BRANCH_REF", "New branch name failed Git ref validation.")


def _verify_create_inputs(request: PublicationRequest, runner: Runner) -> None:
    _validate_new_branch_ref(request, runner)
    local_ref = _run(runner, ["git", "rev-parse", f"refs/heads/{request.branch}"], cwd=request.repository)
    if local_ref.returncode != 0 or local_ref.stdout.strip().lower() != request.new_sha.lower():
        raise PublicationError("LOCAL_BRANCH_MISMATCH", "Local branch does not equal the requested new SHA.")
    object_check = _run(runner, ["git", "rev-parse", "--verify", f"{request.new_sha}^{{commit}}"], cwd=request.repository)
    if object_check.returncode != 0:
        raise PublicationError("LOCAL_SHA_MISMATCH", "Requested commit SHA is not a local commit.")
    if request.expected_base_sha:
        ancestry = _run(
            runner,
            ["git", "merge-base", "--is-ancestor", request.expected_base_sha, request.new_sha],
            cwd=request.repository,
        )
        if ancestry.returncode != 0:
            raise PublicationError("BASE_ANCESTRY_MISMATCH", "Requested new SHA does not descend from the expected base SHA.")


def _execute_create_branch(request: PublicationRequest, runner: Runner) -> PublicationResult:
    _safe_repository_scope(request, runner, classification="REPOSITORY_SCOPE_MISMATCH")
    _verify_create_inputs(request, runner)
    observed = _remote_ref_sha(request, runner)
    if observed is not None:
        return _result(
            request,
            status="blocked",
            classification="REMOTE_BRANCH_ALREADY_EXISTS",
            detail="Remote branch already exists; create-only publication performed no mutation.",
            branch=request.branch,
            expected_remote_state="absent",
            observed_pre_state="present",
            observed_remote_state="present",
            requested_new_sha=request.new_sha.lower(),
            observed_remote_old_sha=observed,
            remote_state="unchanged",
        )
    _host_authentication(
        runner,
        request.repository,
        request.repository_full_name,
        write_classification="HOST_GITHUB_WRITE_NOT_AUTHORIZED",
    )
    push = _run(
        runner,
        _git_network_command(
            "push",
            "--no-force",
            "--porcelain",
            request.remote,
            f"{request.new_sha.lower()}:refs/heads/{request.branch}",
        ),
        cwd=request.repository,
        network=True,
    )
    if push.returncode != 0:
        classification = "PUBLICATION_AUTHENTICATION_BLOCKED" if any(
            marker in (push.stderr or "").casefold()
            for marker in ("credential", "authentication", "authorization", "bearer", "could not read username", "401", "403")
        ) else "PUBLICATION_CONNECTIVITY_BLOCKED"
        return _result(
            request,
            status="blocked",
            classification=classification,
            detail="New branch publication failed; no credential or command output was retained.",
            branch=request.branch,
            expected_remote_state="absent",
            observed_pre_state="absent",
            observed_remote_state="absent_before_push",
            requested_new_sha=request.new_sha.lower(),
            remote_state="unchanged_or_unknown",
        )
    observed_new = _remote_sha(request, runner)
    if observed_new != request.new_sha.lower():
        return _result(
            request,
            status="blocked",
            classification="PUBLICATION_POSTCONDITION_MISMATCH",
            detail="Remote branch head did not equal the requested SHA after creation.",
            branch=request.branch,
            expected_remote_state="absent",
            observed_pre_state="absent",
            observed_remote_state="created",
            requested_new_sha=request.new_sha.lower(),
            observed_remote_new_sha=observed_new,
            remote_state="post_verify_failed",
        )
    return _result(
        request,
        status="completed",
        branch=request.branch,
        expected_remote_state="absent",
        observed_pre_state="absent",
        observed_remote_state="created",
        requested_new_sha=request.new_sha.lower(),
        observed_remote_new_sha=observed_new,
        created=True,
        remote_state="created",
        detail="New remote branch was created and its exact head was reverified.",
    )


def _gh_pr(request: PublicationRequest, runner: Runner) -> dict[str, Any]:
    if request.pr_number is None:
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "A PR number is required.")
    result = _run(
        runner,
        [
            "gh",
            "api",
            f"repos/{request.repository_full_name}/pulls/{request.pr_number}",
            "--jq",
            "{number,state,draft,headSha:.head.sha,baseRef:.base.ref}",
        ],
        cwd=request.repository,
        network=True,
    )
    if result.returncode != 0:
        raise PublicationError("PR_SCOPE_DENIED", "PR metadata could not be verified.")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise PublicationError("PR_SCOPE_DENIED", "PR metadata was not safely parsed.")
    if value.get("state") != "open" or value.get("draft") is not True:
        raise PublicationError("PR_STATE_MISMATCH", "Target PR is not open and Draft.")
    if request.expected_pr_base and value.get("baseRef") != request.expected_pr_base:
        raise PublicationError("PR_SCOPE_DENIED", "Target PR base branch does not match the expected branch.")
    if request.expected_pr_head_sha and value.get("headSha", "").lower() != request.expected_pr_head_sha.lower():
        raise PublicationError("REMOTE_HEAD_MOVED", "Target PR head no longer matches the expected SHA.")
    return value


def _execute_pr_update(request: PublicationRequest, runner: Runner) -> PublicationResult:
    _safe_repository_scope(request, runner)
    _host_authentication(runner, request.repository, request.repository_full_name)
    before = _gh_pr(request, runner)
    command = [
        "gh",
        "api",
        "--method",
        "PATCH",
        f"repos/{request.repository_full_name}/pulls/{request.pr_number}",
        "--input",
        "-",
        "--jq",
        "{number,state,draft,headSha:.head.sha,baseRef:.base.ref}",
    ]
    payload = {name: value for name, value in (("title", request.title), ("body", request.body)) if value}
    updated = _run(runner, command, cwd=request.repository, network=True, stdin=json.dumps(payload, ensure_ascii=False))
    if updated.returncode != 0:
        return _result(request, status="blocked", classification="PUBLICATION_CONNECTIVITY_BLOCKED", detail="Draft PR update failed; no response body was retained.", pr_number=request.pr_number, remote_state="unchanged_or_unknown")
    after = _gh_pr(request, runner)
    if after.get("state") != "open" or after.get("draft") is not True:
        return _result(request, status="blocked", classification="PR_STATE_MISMATCH", detail="Draft PR postcondition failed.", pr_number=request.pr_number, remote_state="post_verify_failed")
    if before.get("headSha") != after.get("headSha"):
        return _result(request, status="blocked", classification="REMOTE_HEAD_MOVED", detail="PR head changed during update verification.", pr_number=request.pr_number, remote_state="post_verify_failed")
    return _result(request, status="completed", pr_number=request.pr_number, remote_state="updated", detail="Draft PR metadata was updated and reverified.")


def _execute_pr_create(request: PublicationRequest, runner: Runner) -> PublicationResult:
    _safe_repository_scope(request, runner)
    _host_authentication(runner, request.repository, request.repository_full_name)
    command = [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{request.repository_full_name}/pulls",
        "--input",
        "-",
        "--jq",
        "{number,state,draft,headRef:.head.ref,baseRef:.base.ref}",
    ]
    payload = {
        "head": request.branch,
        "base": request.base_branch,
        "title": request.title,
        "body": request.body,
        "draft": True,
    }
    created = _run(runner, command, cwd=request.repository, network=True, stdin=json.dumps(payload, ensure_ascii=False))
    if created.returncode != 0:
        return _result(request, status="blocked", classification="PUBLICATION_CONNECTIVITY_BLOCKED", detail="Draft PR creation failed; no response body was retained.", remote_state="unchanged_or_unknown")
    try:
        value = json.loads(created.stdout)
    except (TypeError, ValueError):
        return _result(request, status="blocked", classification="PR_SCOPE_DENIED", detail="Draft PR creation response was not safely parsed.", remote_state="post_verify_failed")
    if value.get("state") != "open" or value.get("draft") is not True or value.get("headRef") != request.branch or value.get("baseRef") != request.base_branch:
        return _result(request, status="blocked", classification="PR_STATE_MISMATCH", detail="Draft PR creation postcondition failed.", pr_number=value.get("number"), remote_state="post_verify_failed")
    return _result(request, status="completed", pr_number=value.get("number"), remote_state="created", detail="Draft PR was created and its state was verified.")


def execute_publication(request: PublicationRequest, *, runner: Runner = run_command) -> PublicationResult:
    """Execute one authorized typed operation in the host context."""

    try:
        if request.operation == "normal_push":
            return _execute_push(request, runner)
        if request.operation == "create_branch":
            return _execute_create_branch(request, runner)
        if request.operation == "draft_pr_update":
            return _execute_pr_update(request, runner)
        return _execute_pr_create(request, runner)
    except PublicationError as exc:
        return _result(request, status="blocked", classification=exc.classification, detail=exc.detail, remote_state="not_mutated")


def publication_request_from_json(raw: Any) -> PublicationRequest:
    """Parse the broker's non-secret JSON envelope; no shell text is accepted."""

    if not isinstance(raw, Mapping):
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Publication request must be an object.")
    allowed = {
        "operation", "mission_id", "repository", "repository_full_name", "authorization", "authorization_reference", "remote", "branch",
        "expected_remote_old_sha", "expected_remote_state", "new_sha", "expected_base_sha", "pr_number", "expected_pr_head_sha", "expected_pr_state",
        "expected_pr_draft", "expected_pr_base", "title", "body", "base_branch", "draft",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Unknown publication request field.")
    auth = raw.get("authorization", {})
    if not isinstance(auth, Mapping) or set(auth) != {"allowed_actions"}:
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Publication authorization must use the existing allowlist shape.")
    actions = auth.get("allowed_actions")
    if (
        not isinstance(actions, list)
        or any(not isinstance(item, str) or not item.strip() for item in actions)
        or len(actions) != len(set(actions))
        or any(item.strip() not in PUBLICATION_ACTIONS for item in actions)
    ):
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Publication authorization allowlist is invalid.")
    operation = raw.get("operation")
    if not isinstance(operation, str) or operation not in PUBLICATION_OPERATIONS:
        raise PublicationError("UNSUPPORTED_PUBLICATION_OPERATION", "Unsupported publication operation.")

    def string_field(name: str, default: str = "") -> str:
        value = raw.get(name, default)
        if not isinstance(value, str):
            raise PublicationError("MALFORMED_PUBLICATION_REQUEST", f"Publication field '{name}' must be a string.")
        return value

    repository_value = string_field("repository")
    if not repository_value.strip():
        raise PublicationError("MALFORMED_PUBLICATION_REQUEST", "Publication field 'repository' must be non-empty.")
    return PublicationRequest(
        operation=operation,
        mission_id=string_field("mission_id"),
        repository=Path(repository_value).expanduser().resolve(),
        repository_full_name=string_field("repository_full_name"),
        authorization=MissionAuthorization(frozenset(actions)),
        authorization_reference=string_field("authorization_reference"),
        remote=string_field("remote", "origin"),
        branch=string_field("branch"),
        expected_remote_old_sha=string_field("expected_remote_old_sha"),
        expected_remote_state=string_field("expected_remote_state"),
        new_sha=string_field("new_sha"),
        expected_base_sha=string_field("expected_base_sha"),
        pr_number=raw.get("pr_number"),
        expected_pr_head_sha=string_field("expected_pr_head_sha"),
        expected_pr_state=string_field("expected_pr_state", "open"),
        expected_pr_draft=raw.get("expected_pr_draft", True),
        expected_pr_base=string_field("expected_pr_base"),
        title=string_field("title"),
        body=string_field("body"),
        base_branch=string_field("base_branch"),
        draft=raw.get("draft", True),
    )


def publication_request_from_delegation(delegation: Any, **fields: Any) -> PublicationRequest:
    """Build a broker request from an already parsed trusted delegation.

    Executor prose is never consulted here; the existing MissionAuthorization
    object is the only capability source.
    """

    if not hasattr(delegation, "authorization") or not hasattr(delegation, "request_id"):
        raise PublicationError("PUBLICATION_AUTHORIZATION_DENIED", "Trusted delegation authorization is required.")
    operation = fields.get("operation")
    authorization = delegation.authorization
    if not isinstance(authorization, MissionAuthorization) or not authorization.allows(str(operation)):
        raise PublicationError("PUBLICATION_AUTHORIZATION_DENIED", "Delegation does not authorize this operation.")
    if fields.get("mission_id", delegation.request_id) != delegation.request_id:
        raise PublicationError("PUBLICATION_AUTHORIZATION_DENIED", "Delegation mission binding does not match.")
    fields.setdefault("mission_id", delegation.request_id)
    fields.setdefault("authorization_reference", delegation.request_id)
    fields.setdefault("repository", delegation.repository)
    fields.setdefault("authorization", authorization)
    return PublicationRequest(**fields)
