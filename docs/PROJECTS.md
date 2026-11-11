# Projects

A project is a local folder you register with Local Agent. Inside a project you can follow one workflow: **select the project → brainstorm → save ideas → record decisions → create tasks → implement → record results.** Each project has an overview and goals, notes and ideas, decisions, tasks and its own conversations.

Projects are optional. Without one, `chat` behaves exactly as before: the classic sessions, `--workspace` and `--data-dir` all work unchanged.

## Try it

```powershell
python -m local_agent project add "C:\work\cake-shop" --name "Cake Shop"
python -m local_agent chat --project cake-shop
```

In the full-screen interface:

1. `f6` opens the project home. Press `e` to write the overview and goals.
2. Press `b` to brainstorm. Describe the problem; the agent restates it, explores options, compares tradeoffs, lists open questions and suggests next steps.
3. When a reply is worth keeping, press `f9` (or **Save…** above the composer) and choose idea, note, decision or task. You edit the draft before anything is saved; it links back to the conversation.
4. `f7` opens the notes. On an idea, `x` turns it into a decision or a task. On a task, `i` starts an implementation conversation.
5. After the work, `/outcome` records what changed, the verification performed and anything unresolved. The task stays open until you set its status to completed (`s`, or the status field in the outcome dialog).

`f5` switches projects at any time; **Classic sessions** in that list leaves projects.

## Where things are stored

| What | Where | Notes |
|---|---|---|
| Project registry | `<data-dir>/projects.json` (default `.agent/projects.json`) | Names, folders, last opened project and per-project settings. Never project content. |
| Notes, ideas, decisions, tasks, overview | `<project>/.agent-project/` | Plain UTF-8 Markdown; edit, diff and commit it like any other file. |
| Conversations and their todo lists | `<data-dir>/state.sqlite3` | Scoped by project id. Memories and skills stay shared, because they are about you. |
| Generated artifacts | `<data-dir>/projects/<id>/artifacts/` | Each project's artifacts are separate. Classic sessions keep `<data-dir>/artifacts/`. |

Registering a folder writes nothing into it. `.agent-project/` and its `README.md` appear when you save the first note. Unregistering (`project remove`, or `d` in the switcher) deletes nothing: the folder and its notes stay, its conversations stay in the data directory, and registering the folder again brings them back.

The registry lives in the data directory, like your settings. If you run chat from different folders with the default `--data-dir .agent`, each has its own registry; pass the same `--data-dir` to share one.

### The knowledge folder

```
.agent-project/
  README.md          explains this layout
  overview.md        overview and goals
  notes/  ideas/  decisions/  tasks/
```

Each file starts with front matter:

```markdown
---
id: task-add-checkout
kind: task
title: Add checkout
status: in-progress
created: 2026-11-10T14:05
updated: 2026-11-10T16:40
related: [decision-use-sqlite, idea-night-delivery]
conversations: [brainstorm-1110-140212, impl-add-checkout]
---

# Add checkout

## Description
...
## Acceptance criteria
- [ ] Pay by card
## Outcome
...
```

- **ids are stable.** File names follow titles and change on rename; ids never do. Link notes with `[[id]]` in the text or list them under `related:`; both survive renames. The notes view shows links, backlinks and missing targets.
- **Kinds:** `overview`, `note`, `idea`, `decision`, `task`. Tasks also have `status: pending`, `in-progress` or `completed`.
- **Lenient reading.** A Markdown file without front matter, with an unknown field, an odd line or invalid UTF-8 still loads; the problem is listed on the note (marked ⚠). Fields Local Agent doesn't use are kept as they are, and Windows line endings are preserved. Files over 1 MB are listed but not loaded. If two files share an id, the second is listed as skipped.
- **Safe writes.** File names are built from titles (letters, digits and hyphens, reserved Windows names avoided) and always stay inside `.agent-project/`. Every write goes to a temporary file that replaces the original atomically.
- **External edits are never overwritten.** Local Agent remembers what each file held when it opened it. If the file changed or disappeared since, saving stops and you choose **Keep editing**, **Reload theirs** or **Overwrite with mine**. The same check protects you when the agent edits a note file through `write_file` while you have it open.

To find a file, press `l` on a note (the path is shown and copied), or run `python -m local_agent note path <note>`.

## Conversations in a project

- Opening a project makes its folder the workspace. File changes and commands still need `/write on` and `/shell on`, and you still approve each one. The switches are remembered per project; a project you haven't set them for starts read-only.
- Each project keeps its own sessions, todo lists, session search and artifacts; nothing from one project reaches another project's requests.
- Conversations have a kind: **chat**, **brainstorm** or **implementation**. Brainstorm and implementation conversations give the agent extra guidance (below).
- When you switch projects, the draft in the composer, queued attachments, the recoverable prompt and the session are kept with the project you leave and come back when you return.
- Switching while a turn is running asks first. If you confirm, the turn is cancelled (nothing from it is saved; file changes or commands it already made stay) and any late output from it is ignored. If you stay, nothing changes.

## Context from notes

Each request in a project can carry project notes. They are chosen in this order, within at most a quarter of the context budget (and no more than half of what the prompt and tools leave):

1. In an implementation conversation, the task being implemented.
2. Notes you selected for this conversation: `c` in the notes view, or `/context add <note>`.
3. The overview, on the first turn of a conversation, in brainstorm and implementation conversations, or when your message shares words with it.
4. Notes linked from the task or the selected notes.
5. Up to three notes whose title, id or text contain words from your message (plain keyword matching; no embeddings or external services).

A **context** line above each reply lists what was sent and why (`selected`, `project overview`, `linked from …`, `matched invoices, sqlite`). `/context` shows the same list with anything that didn't fit or wasn't found. Notes are read from disk for every request, so edits apply to the next message.

Notes are sent as reference material in the saved-context data, with an instruction that they never override the agent's rules or your message. Other projects' notes are never searched.

## Decisions and tasks

- A **decision** has sections for what was decided, why, and the alternatives considered, plus links to related notes and conversations.
- A **task** has a description, acceptance criteria (a checklist), a status and an outcome log.
- **Converting:** `x` on an idea or note drafts a decision or task linked back to it; on a decision it drafts a task.
- **Project tasks and session todo lists** are different things. A project task is durable work you own, kept in Markdown with its status. A session's todo list (`/todo`) is the agent's working checklist for one conversation. When you start implementing a task, the implementation conversation's todo list begins with the task's acceptance criteria, but ticking items there never changes the task. Only you change a task's status.

## Implementation handoff

`i` on a task (or `/implement <task>`, or `chat --project NAME --task <task>`) opens the task's implementation conversation, `impl-<task>`:

- the conversation is linked to the task, and a pending task becomes in-progress (you started it);
- the agent receives the task, its acceptance criteria, the overview and the linked notes and decisions, with the project folder as its workspace;
- a suggested first message is placed in the composer for you to review and send;
- file-write and shell approvals work exactly as in any other conversation.

Afterwards, `/outcome` (or `o` on the task) records a dated entry with the outcome, the verification performed and unresolved issues, linked to the conversation. A model saying "done" changes nothing; completing a task is always your choice. In the full-screen interface, completing a task that has no recorded outcome asks first.

## Agent teams

A project task can also be implemented by a team of agents on the same or different models: `g` on a task, `/team` in chat, or `python -m local_agent team run NAME --task TASK`. The run records its outcome on the task like `/outcome` does, and leaves completion to you. See [TEAMS.md](TEAMS.md).

## Commands

| Command line | What it does |
|---|---|
| `project add FOLDER [--name NAME]` | Register an existing folder |
| `project list` | Registered projects (`·` marks the last opened one) |
| `project show [NAME]` | The project home as text |
| `project rename NAME NEW` · `project move NAME FOLDER` · `project remove NAME` | Rename, point at a moved folder, unregister |
| `note list [--kind K]` · `note search WORDS` · `note show NOTE` · `note path NOTE` | Read notes |
| `note new KIND TITLE [--text TEXT \| --text -]` · `note rename NOTE TITLE` | Write notes |
| `task list [--all]` · `task new TITLE [--criterion C]...` · `task status TASK STATUS` | Tasks |
| `task outcome TASK --outcome TEXT [--verified TEXT] [--unresolved TEXT] [--status S]` | Record an outcome |
| `chat --project NAME [--brainstorm \| --task TASK]` | Chat in a project |
| `chat --no-project` | Classic sessions, even if a project would be reopened |
| `sessions`, `history`, `search`, `todo`, `delete-session` with `--project NAME` | Project conversations |

`note` and `task` commands use `--project`, else the registered project containing the current folder, else the last opened project. Notes are named by id, title or a unique part of either.

**Which project `chat` opens.** `--project` opens that one. An interactive `chat` started without `--session`, `--workspace` or `--project` reopens the project containing the current folder, else the last opened project; the header and sidebar show which one. `--no-project`, `--session`, `--workspace` or a one-shot prompt keeps the classic behavior, so scripts never change scope on their own.

In the full-screen interface: `/projects`, `/project [name|none]`, `/notes [kind|words]`, `/note <id>`, `/create <kind> [title]`, `/tasks`, `/decisions`, `/find <words>`, `/brainstorm [topic]`, `/save [kind] [title]`, `/context [add|remove|clear] [note]`, `/implement <task>`, `/outcome [task]`. In the line-based chat: `/project`, `/notes`, `/tasks`, `/decisions`, `/find`, `/note`, `/task`, `/context`, `/save` (shows the draft and asks before saving; edit the file afterwards) and `/outcome` (asks for each field).

## Keys

| Where | Keys |
|---|---|
| Anywhere | `f5` projects · `f6` project home · `f7` notes · `f9` save the latest reply |
| Projects | `enter` open · `a` register · `r` rename · `m` folder moved · `d` unregister · `h` home · `esc` back |
| Project home | `c` chat · `b` brainstorm · `n` note · `t` task · `r` continue the latest conversation · `k` notes · `j` tasks · `d` decisions · `e` overview · `p` projects |
| Notes | `enter` open · `e` edit · `n` new · `r` rename · `c` use in this chat · `x` convert · `i` implement · `o` outcome · `s` next status · `l` locate · `/` search · `tab`/`shift+tab` filter · `esc` back |
| Editor | `ctrl+s` save · `esc` cancel (asks if there are unsaved changes) |

Below 100 columns the home stacks its columns and hides the action buttons (the key row lists the same actions), the notes view hides the preview (`enter` opens a note), and dialogs stack their choices.

## Compatibility

- The data directory's database moves from schema 3 to 4 the first time a new version opens it. Existing conversations and todo lists become the classic, project-less scope, with their order kept. The upgrade runs in one transaction: if it fails, the old data is left exactly as it was, and running it again is safe. As before, a directory upgraded by a newer version is refused by older versions rather than damaged, so keep a copy of `.agent/` if you might go back.
- `--workspace`, `--data-dir`, sessions, memories, skills, approvals, cancellation and the classic interface are unchanged when no project is open.

## Limits

- Up to 2,000 note files per project are read; notes over 1 MB are not loaded.
- Retrieval is keyword matching (titles weigh most), so phrase a question with the words your notes use, or select notes explicitly.
- Outside the full-screen interface, reviewing a saved reply means reading the draft and editing the file afterwards.
- There is no graph view, sync or sharing; the knowledge folder is meant to be shared through Git.
