# Agent teams

A team is a small group of agents that work on one project task together: a **coordinator** plans and synthesizes, an **implementer** changes files, and **reviewers** or **researchers** inspect and critique, read-only. Each agent names one of your saved provider profiles and a model, so a team can run on a single model or mix providers and models. One agent remains the default; teams are opt-in per project.

| Mode | How |
|---|---|
| Single agent (default) | Nothing to set up. `/team off` returns to it. |
| Team on one provider and model | Use a built-in preset as it is: every agent uses your active profile. |
| Team on different providers | Give agents different profiles, e.g. Claude to plan, DeepSeek to implement, local Ollama to review. |
| Team on different models of one provider | Same profile, a different `--model` per agent. |

## Quick start

Same-model team (every agent on your local Ollama model):

```powershell
python -m local_agent setup --model qwen3:4b --name local
python -m local_agent project add . --name "My App"
python -m local_agent team init solo-model --workflow implement --profile local --project my-app
python -m local_agent team check solo-model --online --project my-app
python -m local_agent team run solo-model "Add a --version flag and a test for it" --project my-app
```

Mixed-model team (coordinator on Claude, implementer on DeepSeek, reviewer on local Ollama with a larger model):

```powershell
python -m local_agent setup --provider anthropic --name claude
python -m local_agent key set claude
python -m local_agent setup --provider deepseek --name deepseek
python -m local_agent key set deepseek
python -m local_agent team init mixed --workflow implement --profile local --project my-app
python -m local_agent team assign mixed coordinator --profile claude --project my-app
python -m local_agent team assign mixed implementer --profile deepseek --model deepseek-chat --fallback local:qwen3:8b --project my-app
python -m local_agent team assign mixed reviewer --profile local --model qwen3:8b --project my-app
python -m local_agent team check mixed --online --project my-app
python -m local_agent team run mixed --task "add version flag" --project my-app
```

In the full-screen interface: `f8` opens the teams screen; choose a team with `enter` and your next message starts a team run after a preflight summary. `g` on a task in the notes browser (`f7`) starts a team implementation run for that task.

## Roles and tools

| Role | Does | Tools by default |
|---|---|---|
| Coordinator | plans, delegates, tracks progress, synthesizes | read |
| Implementer | makes approved changes in the workspace | read, write, shell, todo |
| Reviewer | inspects changes, finds problems, evaluates acceptance criteria | read |
| Researcher | investigates, proposes and critiques approaches | read |

Tool groups: `read` (list, read and search files), `write` (write and patch files), `shell` (run commands), `web` (search and read pages), `todo`. Only implementers may have `write` or `shell`; a shell can change files, so read-only roles never get one. These rules are enforced in code: the agent engine only offers and dispatches an agent's allowed tools, and `team assign` refuses to give a read-only role write or shell. Agents have no tool to create agents, widen their tools or approve anything.

## Presets and workflows

| Preset | Agents | Workflow |
|---|---|---|
| `brainstorm` | coordinator, proposer-a, proposer-b | independent proposals → each critiques the others → synthesis |
| `implement` | coordinator, implementer, reviewer | coordinator's plan → work (dependency order) → review → at most `revision_rounds` revise-and-review cycles → synthesis |
| `review` | coordinator, reviewer-a, reviewer-b | independent read-only inspections → consolidated findings |

Agents report through structured tools (`submit_plan`, `submit_result`, `submit_review`, `submit_synthesis`) whose arguments are validated; the run's state never depends on parsing prose. A plan that fails validation (unknown agent, cycle, too many subtasks, overlapping parallel areas) is returned to the coordinator as an error; if no valid plan arrives, an explicit default plan (one implementation subtask) is used and the record says so. An agent that answers in plain text is recorded as an unstructured result; an unstructured review is never counted as approval.

## What each agent sees

- The user's request and the task's acceptance criteria, marked as authoritative.
- Project notes chosen for its assignment within its own context budget: the task, the overview, notes you selected and matching notes (see [PROJECTS.md](PROJECTS.md#context-from-notes)), never another project's.
- Only the results of the subtasks its assignment depends on, as structured records, plus the recorded changes and command evidence for reviewers, revisions and the synthesis. It never sees other agents' transcripts.
- Instructions that notes, tool output and other agents' results are reference material, and that verification means checks the run observed.

## Verification, results and disagreements

The run records approved file changes and command exit codes. The final result shows the latest exit code of each command in each workspace and the coordinator's account. Parallel runs also report the checks observed on the integration worktree. With no checks, the result says the changes are unverified. Unresolved issues collect failed or skipped subtasks, open issues, high and medium review findings, unmet criteria and early stops. Disagreements include the coordinator's list plus those the run detects: reviewers with different verdicts or criteria, and an implementer reporting "done" while a reviewer requested changes.

The combined result is added to the conversation the run started from. When the run is for a project task, the task's outcome log gets a dated entry: the summary, observed verification, unresolved issues, run id, team and each agent's provider/model. The task's status changes only from pending to in-progress when the run starts; completing it is always your decision.

## Approvals

Only the implementer can ask to change files or run commands, and every request goes through the usual approval dialog, which names the agent, its role and model, the project, the assignment and the workspace. Requests are serialized: one dialog at a time, so an answer can never apply to another agent's request. `--yes` on the command line, like in chat, allows actions without asking except commands flagged as dangerous.

## Limits

| Limit | Default | Meaning |
|---|---|---|
| `concurrency` | 2 | agents working at the same time |
| `local_concurrency` | 1 | agents at once on local model servers (Ollama, localhost), which share one machine |
| `max_subtasks` | 6 | subtasks in a plan |
| `delegation_depth` | 1 | fixed: only the coordinator plans; agents can't delegate further |
| `agent_rounds` | 8 | tool rounds per agent per assignment |
| `revision_rounds` | 1 | revise-and-review cycles after the first review (0–3) |
| `request_timeout` | 180 | seconds to wait for one model reply |
| `run_minutes` | 30 | the whole run |
| `max_requests` | 80 | model requests in the whole run |
| `max_tokens` | 0 | reported plus estimated tokens in the run (0: no limit) |

`python -m local_agent team limit NAME KEY VALUE` changes one. When a limit is reached, the run stops scheduling, stops its agents and ends as failed with a partial record.

## Providers, models and fallback

Every agent gets its own provider object, built from its profile when the run starts, so agents never share request state. Keys stay in the key store or environment; team files, notes, prompts and run records only name profiles. `team check --online` (or `c` on the teams screen) asks each distinct model whether it answers.

An agent may name a fallback with `--fallback PROFILE[:MODEL]`. If the provider fails before any action has been approved for the assignment, the agent can retry on its fallback once per run. After an approval, the assignment stops so you can inspect any effects before retrying. The run panel, notifications, record and final result show each switch. Usage is attributed per agent. Missing counts are estimated from the request, tool definitions, reply and tool calls.

## Cancellation, failures and interrupted runs

`esc`, **Stop** or `Ctrl+C` cancels a run: no new work is scheduled, streaming replies stop at the next chunk, running commands are stopped with their child processes, and an agent waiting for approval is released. Late output from a cancelled run, or from a run of a project you switched away from, is ignored. Cancelling does not undo file changes or commands that already completed. The record keeps everything up to that point.

A failed assignment marks its dependents skipped and the run continues to a synthesis that reports it. Run records are saved under `<data-dir>/projects/<project-id>/runs/` while the run works. A run whose process stopped (a crash or closing the app) is marked **interrupted** the next time runs are listed; nothing is replayed.

## Parallel writers (Git projects)

To add a second implementer to the built-in implementation team in the current project, run:

```powershell
python -m local_agent team parallel implement on
python -m local_agent team add-writer implement writer-b
```

The new writer inherits the primary implementer's profile and model. Use `--profile` and `--model` to override them.

For an `implement` team with two or more implementers that may write, `team parallel NAME on` (or `w` on the teams screen) gives each writer its own Git worktree and branch, `local-agent/<run>/<agent>`, all starting from the project's current commit. Your working tree, including uncommitted changes, is not touched. The coordinator must give each writer separate paths. After the work, each branch is committed, files changed by more than one writer are reported, and the branches are merged into `local-agent/<run>/integration`; a branch that conflicts is not merged, the integration is marked failed, and every branch and worktree is kept for inspection. Reviews, revisions and checks then run on the integration worktree.

Nothing reaches your branch until you run `python -m local_agent team apply RUN`, which merges the integration branch only when your working tree has no uncommitted changes to tracked files, and never pushes. Worktrees live outside the project, next to the key store (`%APPDATA%\local-agent\worktrees` on Windows), and are removed only once their contents are committed on their branches.

Parallel writers need the project folder to be the top of a Git repository with at least one commit; otherwise the preflight explains why and one writer remains available.

## Where things are stored

| What | Where |
|---|---|
| Team presets | `<data-dir>/projects/<project-id>/teams.json` (no keys) |
| Run records | `<data-dir>/projects/<project-id>/runs/<run-id>.json` |
| Each agent's transcript | conversation `<run-id>.<agent>` in a separate team scope; `team result RUN --agent ID` prints it |
| Combined result | a turn in the conversation the run started from |
| Task outcome | the task's Markdown file |

A broken `teams.json` is reported when you open teams; single-agent chat keeps working.

## Commands

| Command | What it does |
|---|---|
| `team list` · `team show NAME` | Teams with their mode and problems · agents, models, tools, limits and instructions |
| `team init NAME --workflow W [--profile P] [--model M]` | Save a team from a preset |
| `team assign NAME AGENT [--profile P] [--model M] [--tools T] [--fallback P[:M]] [--instructions TEXT]` | Change one agent |
| `team add-writer NAME AGENT [--profile P] [--model M]` | Add an implementer to a parallel team |
| `team limit NAME KEY VALUE` · `team parallel NAME on\|off` · `team delete NAME` | Limits, parallel writers, delete a saved team |
| `team check NAME [--online]` | Validate profiles, models, keys and tools; optionally ask each model |
| `team run NAME [REQUEST] [--task TASK] [--session S] [--yes] [--check]` | Run a team |
| `team runs` · `team result RUN [--agent ID]` · `team apply RUN` | Recent runs · a record or one agent's transcript · merge a parallel run |

All take `--project NAME` (default: the project containing the current folder, else the last opened).

In the full-screen interface: `/team` (or `f8`) opens the teams screen; `/team NAME` and `/team off` switch modes; `/team run REQUEST`, `/team runs` and `/team result RUN` work like the commands above.

| Teams screen | Keys |
|---|---|
| Teams list | `enter` use this team · `s` single agent · `n` new team from a preset · `l` limits · `w` parallel writers · `c` check models · `r` run now · `d` delete |
| Agents list (`tab`) | `e` edit the agent's profile, model, tools, fallback and instructions |

The run panel in the conversation has one row per agent with its role, provider/model, state, current action, elapsed time and token usage; press `enter` on a row (or `ctrl+o` for all) to see its latest output and tool activity.

## Limits of this version

- Agents work through the same tool loop as a single agent; small local models may not call the structured tools reliably, in which case results are recorded as unstructured and the default plan is used.
- A model request already in flight can't be interrupted on providers that don't stream; cancellation then takes effect when it returns or times out (`request_timeout`).
- Parallel writers need Git. Integration merges whole branches: a branch that conflicts is left unmerged, the integration is marked failed and the reviews are skipped, so resolve it on the kept branches.
