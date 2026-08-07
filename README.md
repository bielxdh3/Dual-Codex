<div align="center">

# Dual Codex

**Two Codex accounts. One coordinated development loop.**

[![Status](https://img.shields.io/badge/status-prototype-orange)](#project-status)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4)](#requirements)
[![Tests](https://github.com/bielxdh3/Dual-Codex/actions/workflows/tests.yml/badge.svg)](https://github.com/bielxdh3/Dual-Codex/actions/workflows/tests.yml)

A local orchestrator that keeps two independently authenticated Codex CLI profiles in distinct roles: one plans and reviews, while the other implements and tests.

</div>

> [!IMPORTANT]
> Dual Codex is an early prototype. Test it on a disposable branch or repository before trusting it with important work.

## How it works

```text
                         ┌──────────────────────┐
                         │   Markdown task      │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │ Architect account    │
                         │ plan · read-only     │
                         └──────────┬───────────┘
                                    │ plan.json
                         ┌──────────▼───────────┐
                         │ Executor account     │
                         │ edit · test · report │
                         └──────────┬───────────┘
                                    │ git diff
                         ┌──────────▼───────────┐
                         │ Architect account    │
                         │ review · read-only   │
                         └──────┬─────────┬─────┘
                                │         │
                         approved     changes requested
                                │         │
                                │   ┌─────▼────────────┐
                                │   │ Executor fixes   │
                                │   └─────┬────────────┘
                                └─────────┴──► REPORT.md
```

The two accounts are isolated with separate `CODEX_HOME` directories. Each run produces structured JSON artifacts, the captured Git diff, and a final Markdown report.

## Project status

Version **0.1.2** currently supports:

- separate Architect and Executor Codex profiles;
- ChatGPT subscription authentication through Codex CLI;
- read-only planning and review;
- workspace-write implementation;
- JSON Schema-constrained outputs;
- configurable correction cycles;
- Git cleanliness checks;
- run artifacts and final reports;
- Windows `.CMD` launcher compatibility;
- visible progress while long Codex turns are running.

It intentionally does **not** create commits, push branches, open pull requests, or merge code yet.

## Requirements

- Windows 10 or 11;
- Python 3.11 or newer;
- Git;
- Node.js/npm or another supported Codex CLI installation method;
- Codex CLI;
- two ChatGPT accounts with Codex access that you are authorized to use.

## Quick start

### 1. Install Codex CLI

Using npm:

```powershell
npm install -g @openai/codex
codex --version
```

Codex CLI supports signing in with ChatGPT subscriptions. Dual Codex does not require an OpenAI API key for this workflow.

### 2. Clone and install Dual Codex

```powershell
git clone https://github.com/bielxdh3/Dual-Codex.git
cd Dual-Codex

py -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

If `py` selects Python older than 3.11, install a newer Python version or select it explicitly with the launcher available on your machine.

### 3. Create and authenticate both profiles

```powershell
.\scripts\setup-profiles.ps1 -BasePath "$HOME\CodexProfiles"
```

The script opens two browser login flows in sequence:

1. `architect` — planning and review;
2. `executor` — implementation and tests.

Use the intended ChatGPT account in each browser flow. Profile credentials are stored separately under:

```text
%USERPROFILE%\CodexProfiles\architect
%USERPROFILE%\CodexProfiles\executor
```

> [!CAUTION]
> Never commit, upload, or share either profile's `auth.json` file.

### 4. Configure the orchestrator

```powershell
Copy-Item config.example.toml config.toml
notepad config.toml
```

Example:

```toml
[orchestrator]
repository = "C:/Users/YOU/Projects/target-repository"
runs_dir = "runs"
max_correction_cycles = 1
require_clean_git = true
codex_command = "codex"

[architect]
codex_home = "C:/Users/YOU/CodexProfiles/architect"
model = ""
reasoning_effort = "high"
sandbox = "read-only"

[executor]
codex_home = "C:/Users/YOU/CodexProfiles/executor"
model = ""
reasoning_effort = "high"
sandbox = "workspace-write"
```

Use forward slashes in Windows TOML paths. Leaving `model = ""` lets each account use its available default model.

### 5. Run diagnostics

The module form works even when Python's Scripts directory is not on `PATH`:

```powershell
python -m dual_codex.cli doctor
```

A healthy setup reports `[OK]` for the Codex executable, version, both profiles, both logins, and the target Git repository.

### 6. Run a task

Edit `task.example.md` or create another Markdown task file:

```powershell
python -m dual_codex.cli run task.example.md
```

You will see progress for planning, implementation, review, and any correction cycle. Results are stored in a timestamped directory:

```text
runs/20260807T001613Z/
├── task.md
├── plan.json
├── implementation.json
├── diff-0.md
├── review-0.json
└── REPORT.md
```

## Git safety

By default, `require_clean_git = true`. A run stops when the target repository has uncommitted changes so the executor cannot silently mix new work with existing edits.

For a disposable smoke test only, you may set:

```toml
require_clean_git = false
```

Return it to `true` before using Dual Codex on real work.

## Configuration reference

| Setting | Purpose |
|---|---|
| `repository` | Git repository where the agents work |
| `runs_dir` | Directory containing run artifacts |
| `max_correction_cycles` | Maximum executor retry rounds after review |
| `require_clean_git` | Refuse to start with uncommitted changes |
| `codex_command` | Codex executable name or path |
| `codex_home` | Isolated profile directory for each account |
| `model` | Optional explicit model; empty uses the account default |
| `reasoning_effort` | Codex reasoning effort passed to the run |
| `sandbox` | Architect uses `read-only`; Executor uses `workspace-write` |

## Security model

- Architect runs with `read-only` access.
- Executor runs with `workspace-write` access.
- Credentials stay outside the repository in separate `CODEX_HOME` folders.
- `config.toml`, `auth.json`, `.venv`, logs, caches, and run outputs are ignored by Git.
- The prototype never uses `danger-full-access` or bypasses the sandbox.
- The prototype never commits, pushes, opens PRs, or merges automatically.

## Tests

```powershell
python -m unittest discover -s tests -v
```

GitHub Actions runs the test suite on supported Python versions for every push and pull request.

## Roadmap

- [ ] Isolated branch/worktree per task
- [ ] Session persistence and resume
- [ ] Streaming progress and richer logs
- [ ] Usage and duration metrics per account
- [ ] Optional automatic commit
- [ ] Optional draft pull request
- [ ] Additional agent adapters
- [ ] Explicit approval policies for higher-risk actions

## Disclaimer

Dual Codex is an independent community project and is not affiliated with or endorsed by OpenAI. Codex and ChatGPT are trademarks of their respective owner.
