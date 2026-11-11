---
id: task-multi-agent-teams-with-same-or-mixed-provider-mode
kind: task
title: Multi-agent teams with same or mixed provider models
status: in-progress
created: 2026-11-10T15:17
updated: 2026-11-10T08:54
---

# Multi-agent teams with same or mixed provider models

## Description
Extend project chats with optional agent teams. Keep single-agent mode as the default. First deliver a coordinator, one implementer and one reviewer with a single workspace writer; then add parallel implementation using isolated Git worktrees. Teams can use one provider/model for all roles or different configured provider profiles and models per role. Reuse existing project context, tasks, approvals and outcome records.

## Acceptance criteria
- [x] Support single-agent, same-provider/model team, and mixed-provider/model team modes.
- [x] Save project team presets for brainstorming, implementation and review, with provider profile, model, instructions and permitted tools per role.
- [x] Implement coordinator task decomposition, explicit handoffs, bounded execution and synthesis of findings and disagreements.
- [x] Give each agent its own provider instance and bounded context; share selected project notes and explicit results without leaking other projects or credentials.
- [x] Start with one writing implementer and read-only research/review agents; preserve existing file and shell approvals.
- [x] Show a team run panel with role, provider/model, status, current action, elapsed time, available token usage and expandable output.
- [x] Enforce configurable concurrency, round, time and usage limits; support low concurrency for local models and explicit visible fallback behavior.
- [x] Cancel runs safely, stop scheduling new work, handle running commands and ignore late results after cancellation or project switching.
- [x] Record the coordinated result, verification evidence, unresolved issues and agent contributions against the originating project task; retain user control of task completion.
- [x] Add a later phase for parallel writers in isolated Git worktrees, conflict handling, integration review and verification before applying the combined result.
- [x] Test routing, context isolation, approvals, cancellation, provider failures, concurrency limits, persistence and single-agent compatibility; document setup and headless/native validation limits.

## Outcome
### 2026-11-10

**Outcome:** Implemented agent teams: project-scoped presets (brainstorm, implement, review) with a profile, model, tools, instructions and limits per agent; a bounded orchestrator (structured plans, results and reviews; dependencies; concurrency and local-model limits; bounded revision cycles; synthesis with observed verification, unresolved issues and disagreements); one provider object per agent with visible fallback; single-writer approvals that name the agent and are serialized; cancellation, limits, interrupted-run records; task outcomes; parallel writers in Git worktrees with integration and `team apply`; the `team` CLI, team mode (f8), preflight and run panel in the terminal UI; docs/TEAMS.md.
**Verification:** Sandbox only (Linux, Python 3.10, Textual 8.2.8, Git 2.34), scripted models: 255 tests passed in two runs (2 optional image checks skipped), including 25 orchestration tests (repeated 3x) and 6 headless team UI tests; headless text renders inspected at 120x40 and 80x24.
**Unresolved:** Not run: live providers (no model server or key reachable), Windows native terminal, Git worktrees on Windows (paths with spaces, %APPDATA%), and the suite on Python 3.11+ without the tomllib stand-in.

### 2026-11-11

The review fixes passed the Windows Python 3.11.9 suite with 260 tests passing and two optional image checks skipped. The checks cover applying a merge over ignored files, fallback after an approved command, failed planning, worktree-specific verification, token estimates, adding a writer, cancelling an approval and rendering the final team reply. The installed-wheel coding workflow also passed. Live team providers and visual acceptance in Windows Terminal remain open.
