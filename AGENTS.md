# Dual Codex App integration

The Codex App is the visible orchestrator, architect, reviewer, and user
interface. The account assigned to the `executor` role is the only account
that may be launched for delegated implementation work.

When the user says “Use Dual Codex to implement this task.”:

1. Inspect and understand the target repository in the visible App.
2. Prepare a precise version-1 JSON request with `action: "implement"` and an
   explicit `repository` path.
3. Run the repository-local launcher:

   ```powershell
   .\scripts\dual-codex.ps1 --config <config> delegate --request-file <request> --result-file <result>
   ```

   Standard input is also supported with `delegate --stdin --result-file`.
4. Wait for the final `DUAL_CODEX_RESULT` line, then read the result JSON,
   executor report, Git status, and diff named by that result.
5. Review the real implementation in the visible App. Create a version-1
   `correct` request only for concrete blocking or important findings. A
   correction must include the original task, `parent_request_id`, and
   actionable `review_findings`.
6. Respect `max_correction_cycles` from configuration and present the final
   evidence to the user. Never claim success without reading the result and
   diff.

Before delegating, use `status --json` when useful to verify the executor role,
executor label and login status, the active repository, Git state, and Codex
CLI version. Delegation refuses an unassigned or unavailable executor. Do not
invoke the visible orchestrator account through `codex exec` for the same
delegation, and do not print or read authentication files.
