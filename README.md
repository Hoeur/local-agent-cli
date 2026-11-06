# Local Agent

A standalone Python CLI agent with persistent conversations, memory, and reusable skills. A named chat sends messages to a configured model provider, runs the tools it asks for, and passes their results back until the model replies. It can read, search and edit files, run shell commands, search the web, keep a task list, search earlier conversations, and remember facts and reusable skills. Every change to your files and every command waits for your approval. SQLite stores completed turns, task lists, memories and skills.

## Run on Windows

Requires Python 3.11 or later and a model with tool calling: local through Ollama, or a cloud model (Claude, OpenAI/Codex, DeepSeek, Alibaba Qwen and others; see [Models and settings](#models-and-settings)). The local development setup uses Ollama with `qwen3:0.6b`. From this directory, run these commands in PowerShell.

```powershell
python -m pip install -e ".[tui]"
python -m local_agent setup --model qwen3:0.6b
python -m local_agent doctor
python -m local_agent chat --session demo                          # chat only
python -m local_agent chat --session dev --workspace . --all       # files, shell, web and learning
```

![Local Agent terminal UI](docs/screenshots/welcome.png)

## Tools

Tools are organized into groups. Only the groups you enable are sent to the model, so a small model sees fewer choices and the request stays small.

| Group | Enabled by | Tools | What they do |
|---|---|---|---|
| core | always | `read_skill`, `todo`, `session_search` | Load a saved skill. Keep a task list for multi-step work; it's saved with the session and shown to the model on later turns. Search earlier conversations in every session. |
| learn | `--learn` | `remember`, `forget`, `save_skill`, `delete_skill` | Propose memory and skill changes, saved at the end of the turn after you approve them. |
| workspace | `--workspace DIR` | `list_files`, `read_file`, `search_files` | List folders, read files with line numbers and paging, search file contents with a regex or names with a glob. Read-only. |
| write | `--write` | `write_file`, `patch` | Create or replace a file, or replace one exact, unique piece of text in a file. You see the diff and approve each change before it's made. |
| shell | `--shell` | `terminal` | Run a command in the workspace (`cmd.exe` on Windows, `/bin/sh` elsewhere) with no input, a timeout (60 s by default, up to 600 s) and capped output. You approve each command. |
| web | `--web` | `web_search`, `web_extract` | Search the web (DuckDuckGo's HTML page) and read a page as plain text. |

`--all` is shorthand for `--learn --write --shell --web`. `--write` and `--shell` need `--workspace`. In the full-screen UI, `/write`, `/shell`, `/web` and `/learn` switch the groups on and off between turns.

### Approvals and safety

- **File changes and commands** are approved one at a time, before they happen. The prompt shows the diff, or the full command. If you decline, nothing changes and the model is told to stop and ask you what to do. Without a terminal to ask, every action is declined.
- **`--yes`** saves memory and skill changes and allows actions without asking, with one exception: commands that look destructive are flagged and always ask. These include recursive deletes, disk formatting, `git push --force` or `reset --hard`, piping a download into a shell, `sudo`, force-killing processes, and changes to the registry or services. The full list is `DANGEROUS` in `local_agent/tools.py`. Without a terminal to ask, flagged commands are declined. This is a safety net for mistakes, not a sandbox. A command you allow runs with your user's permissions and can reach outside the workspace.
- **File tools** stay inside the workspace. Paths must be relative, with forward slashes (backslashes are converted). Hidden files and folders such as `.git` and `.env`, Windows device names, and links that lead outside are refused. Writes go through a temporary file, so a failed write leaves the original intact. `patch` also matches text written with `\n` against files that use Windows `\r\n` line endings, and keeps the file's own line endings.
- **Web tools** fetch pages read-only and follow up to five redirects. They connect directly to checked public addresses on every hop and skip system HTTP proxies. Web content is untrusted data.
- **Prompt injection.** The system prompt tells the model to treat saved context and tool output as data, not instructions. The approval prompts are your real defense, so read what you allow.

### Context budget and compression

Each model request is kept within a character budget, 12,000 by default (roughly 3,000 to 4,000 tokens). This fits Ollama's small default context window, which otherwise drops whatever doesn't fit without warning. With every tool group enabled, the tool definitions alone take about 4,000 characters. For real work with `--all`, raise the model's window and the budget, for example by setting `OLLAMA_CONTEXT_LENGTH=16384` before starting Ollama, then `setup --model qwen3:0.6b --context-chars 48000`. `AGENT_CONTEXT_CHARS` overrides the saved budget. Running setup again resets the budget unless you pass it. Tool output scales with the budget: file reads, search results, command output and web pages each return up to a fifth of it (at least 2,400 characters).

Earlier turns are replayed within up to half of the space left after the system prompt and the new message. When a turn outgrows the budget, the agent compresses context, keeping the system prompt, your message and the newest work:

1. Tool results from earlier rounds of this turn are replaced with a short note that the model can re-run the tool.
2. The oldest replayed turns are dropped.
3. The newest tool results are shortened, keeping their structure and marking them as truncated.

The turn fails only if even that isn't enough. Reasoning blocks such as Qwen's `<think>…</think>` are stripped before a reply is saved, so they never take up space in later requests.

## Terminal UI

In an interactive terminal, `chat` opens a full-screen interface. It includes:

- an animated holographic AI core that reacts to what the agent is doing (thinking, streaming, running tools, waiting for approval, success and error)
- streamed Markdown replies with highlighted code
- live tool-call rows you can expand
- a sidebar with live CPU usage and history, RAM usage, disk free space, app memory, sessions, a file explorer, tools and model details
- a command palette (`ctrl+p`), plus `/` command and `@` file autocomplete
- approval dialogs for memory and skill changes and for each file change and command
- five themes and three motion levels

`esc` cancels a running turn without saving anything, and stops a running command along with its child processes. Press `f1` for every shortcut. `f3` or `/hologram` shows, hides, resizes and switches the hologram's renderer: Braille 3D, ASCII, or Sixel/Kitty graphics with `pip install -e ".[hologram-image]"`.

The UI needs the optional `tui` extra ([Textual](https://textual.textualize.io/) and `psutil`). Without Textual, or with `--plain`, or when input or output is piped, `chat` uses the line-based interface, which keeps the original output format when piped. `--no-stream` requests whole replies from providers that handle streaming poorly. The interface works in Windows Terminal, macOS Terminal, Linux terminals and over SSH, where it defaults to reduced motion. See [docs/TUI.md](docs/TUI.md) for the layout, keys, commands, design system and architecture.

The system monitor stays at the top of the sidebar and refreshes once per second without blocking chat. Its readings stay local. App RAM is the chat process's memory; separately running model servers are reflected in total system RAM. Press `ctrl+b` or `f2` to show the sidebar if it is hidden; terminals below 100 columns hide it automatically. Restart chat after updating the source so Python loads the new monitor. Reinstall `python -m pip install -e ".[tui]"` if the counters show that `psutil` is missing.

| | |
|---|---|
| ![Tool calls and Markdown](docs/screenshots/conversation.png) | ![Approving a saved skill](docs/screenshots/approval.png) |
| ![Command palette](docs/screenshots/palette.png) | ![Daylight theme and an error card](docs/screenshots/daylight.png) |
| ![Approving a command](docs/screenshots/action.png) | |

## Models and settings

The agent can run on a local Ollama model or on a cloud model you pay for by the token. Each saved setup is a *profile* (provider, model, endpoint and budget) in `.agent/config.json`; you can keep several and switch between them.

| Provider | `--provider` | Default model | API key variable |
|---|---|---|---|
| Ollama, on your computer | `ollama` (default) | pass `--model` | none |
| Claude (Anthropic) | `anthropic` or `claude` | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| OpenAI, including Codex models | `openai` or `codex` | `gpt-5.5` | `OPENAI_API_KEY` |
| DeepSeek | `deepseek` | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| Alibaba Model Studio, Singapore / Virginia / Beijing | `alibaba`, `alibaba-us`, `alibaba-cn` | `qwen3-coder-plus` | `DASHSCOPE_API_KEY` |
| Alibaba Coding Plan (monthly subscription) | `alibaba-coding` | `qwen3-coder-plus` | `DASHSCOPE_API_KEY` |
| OpenRouter (one key, many vendors) | `openrouter` | pass `--model` | `OPENROUTER_API_KEY` |
| A reseller or any other endpoint | `custom` | pass `--model` and `--base-url` | `AGENT_API_KEY` |

```powershell
python -m local_agent setup --provider deepseek          # saves the profile and makes it active
python -m local_agent key set deepseek                   # paste the key; it isn't shown or echoed
python -m local_agent doctor                             # checks the key, endpoint and model
python -m local_agent chat --workspace . --write

python -m local_agent setup --provider alibaba --model deepseek-v3.2   # DeepSeek served by Alibaba
python -m local_agent use claude                         # switch profile (creates it from the preset)
python -m local_agent use ollama --model qwen3:4b        # switch and change that profile's model
python -m local_agent --profile anthropic chat           # use a profile once without switching
python -m local_agent providers                          # presets, saved profiles, which keys are set
python -m local_agent models                             # models the active endpoint offers
python -m local_agent key list                           # where each key comes from (never the key)
```

In the terminal UI, `/provider` lists profiles and `/provider <name>` switches; `/model` lists the endpoint's models and `/model <name>` switches model. Both take effect from the next message.

**API formats.** Ollama, DeepSeek, Alibaba, OpenRouter and most resellers use OpenAI's chat-completions format. Claude uses Anthropic's Messages API, with prompt caching on the system prompt and tool list so repeated tool rounds cost less. OpenAI uses the Responses API, which Codex models require; replies are not stored on OpenAI's servers (`store: false`). For a reseller, pick the format they document with `--wire chat|anthropic|responses`, for example `setup --provider custom --name shop --wire anthropic --base-url https://their-host/v1 --model claude-sonnet-5-5`. The base URL stops before `/chat/completions`, `/messages` or `/responses`.

**API keys** are never written to the project. The agent looks for the provider's variable first (`setx DEEPSEEK_API_KEY sk-...`, then open a new terminal), then the key store filled by `key set`, which is `%APPDATA%\local-agent\keys.json` on Windows (`~/.config/local-agent/keys.json` elsewhere; `AGENT_KEYS_FILE` overrides). The store is plain JSON readable by your user account, like most CLI credential files, so don't share that folder. `AGENT_API_KEY` is used only for Ollama and `custom` profiles, so a reseller key is never sent to Anthropic or OpenAI by mistake.

**Budgets and cost.** Cloud profiles default to a 100,000–150,000 character budget (about 25,000–40,000 tokens), because these models have large windows. Every tool round resends the conversation, so a turn with six tool rounds is billed for roughly six requests; lower `--context-chars` to spend less. Claude replies are capped at 8,192 tokens unless you pass `--max-tokens`. Hosted endpoints that are overloaded or rate-limited are retried twice; an exhausted balance or quota is reported at once, along with the provider's own error message.

`AGENT_BASE_URL`, `AGENT_MODEL`, `AGENT_CONTEXT_CHARS` and `AGENT_PROFILE` override the saved settings. For Ollama on another machine, install and start Ollama, then `ollama pull qwen3:4b` and `setup --model qwen3:4b`. Providers that omit tool-call IDs, or send empty arguments for calls that take none, are handled.

## Commands

Use `/exit` (or `ctrl+q` in the full-screen UI) to leave interactive chat, or supply one prompt on the command line. Reuse `--session` to continue a conversation.

```powershell
python -m local_agent chat --session demo --learn 'Remember that I prefer concise replies.'
python -m local_agent chat --session demo --learn 'Save a skill called release-checklist with these steps. Run tests, review the changelog, and verify package checksums.'
python -m local_agent chat --session dev --workspace . --write --shell 'Run the tests and fix the first failure.'
python -m local_agent chat --session research --web 'What changed in the latest Python release?'
python -m local_agent memory                     # list memories
python -m local_agent remember editor vscode     # save one yourself
python -m local_agent forget editor
python -m local_agent skills
python -m local_agent skill release-checklist
python -m local_agent skill release-checklist --delete
python -m local_agent sessions
python -m local_agent history demo
python -m local_agent search 'concise replies'   # search every conversation
python -m local_agent todo dev                   # a session's task list
python -m local_agent delete-session demo
```

- `--learn` enables changes to memories and skills. Before saving, the CLI lists the pending changes and asks for confirmation. Declined changes are discarded, and the saved reply and tool results note that they were not saved. The model receives saved memories and a list of skill descriptions on each turn. It can request the full text of a relevant skill by name.
- `--max-rounds N` sets how many tool rounds a turn may use, from 1 to 50 (default 6). Multi-step coding tasks with `--write --shell` usually need more, for example `--max-rounds 20`. After the last round, the model is asked for a text answer with no tools available.
- Choose a workspace whose contents you are comfortable sharing with the configured provider. Chat history, memories and tool results are included in model requests as needed.
- State lives in `.agent/state.sqlite3`. Use `python -m local_agent --data-dir <directory> chat` for a separate history, memory and skill library. The database is versioned: older data directories are upgraded in place when opened, and a directory written by a newer version is refused rather than damaged.

## MVP behavior

Memory and skill changes are committed together with the completed turn, after you approve them. The task list is saved with the turn too. Provider failures, cancellation or tool-loop exhaustion discard everything the turn staged. File changes and commands you approved have already happened by then and are not undone. A concurrent update to the same session is rejected so its history stays consistent.

The MVP has these limits:

- **Store:** up to 20 memories and 20 skills per data directory, and up to 30 tasks per session.
- **Tool calls:** up to four calls per round. Extra calls in a round get an error result the model can act on.
- **Tool output:** file reads page through files of up to 5 MB, and searches scan up to 5,000 files and return up to 60 matches.
- **History:** up to 20 recent completed turns. Full transcripts remain available through `history`.

These are character limits, so keep the budget within the model's context window. The small local model has limited reasoning and instruction-following ability. Model-generated memories and skills can be inspected with the commands above.

## Install and verify

The agent and the line-based chat run directly from source using Python's standard library. The full-screen UI adds Textual and psutil through the `tui` extra. For a `local-agent` command, install the project into your Python environment.

```powershell
python -m pip install -e ".[tui]"
local-agent doctor
python -m unittest discover -s tests -v
```

Without the extra, the terminal UI tests are skipped and everything else runs. GitHub Actions runs the suite on Windows and Linux for Python 3.11 to 3.13, with and without the extra (`.github/workflows/tests.yml`).

Earlier live verification with Ollama and `qwen3:0.6b` covered these, through interactive chat:

- memory creation, and recall in another session
- skill creation, and loading the saved skill
- reading a workspace file
- session listing and transcript viewing

Test prompts used Qwen's `/no_think` suffix. The tools added for the MVP (writing and patching files, the terminal, web access, the task list and session search) are covered by the automated tests and a scripted run against a local OpenAI-compatible endpoint. They have not yet been run against a live model.

Automated regression checks cover:

- **File tools:** reads with paging, search, writes and patches with approval, and workspace boundaries.
- **Terminal and web:** exit codes, timeouts, cancellation, long output, flagged commands, page extraction, redirects, refused private addresses and search parsing.
- **Task list and search:** todo persistence and session search.
- **Context:** compression, `<think>` stripping, and context budgets.
- **Providers:** missing tool-call IDs, argument validation, and provider response parsing.
- **Storage:** schema upgrades, persistence, rollback, stale session writes, skill storage and store limits.
- **Approval and limits:** disabled learning, change approval, declined-change history, and tool limits.
- **Command line:** the memory, skill, search, todo and session commands.
- **Terminal UI:** streaming, tool rows, the approval and action dialogs, dangerous-command focus, cancellation, errors, commands, autocomplete, file previews, resizing and resumed history.
- **Streaming:** against a local HTTP endpoint.

## Design and scope

The implementation includes:

- the model/tool loop
- toolsets
- the file tools (`read_file`, `write_file`, `patch`, `search_files`)
- the `terminal` tool with dangerous-command approval
- `todo` and `session_search`
- web search and extraction
- persistent sessions and on-demand skill loading
- context compression (protect the head and the newest tail, prune old tool output first)

This project implements its own CLI, provider adapter and data format, with one deliberate difference: compression trims and drops context deterministically instead of asking a second model for a summary.

The following features are not part of this MVP:

- background processes
- browser automation
- sub-agents (`delegate_task`)
- `execute_code`
- scheduled jobs
- messaging gateways
- vision and image generation
- text-to-speech
- MCP servers
- file-based `MEMORY.md` / `USER.md` memory
