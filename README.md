# Local Agent

A standalone Python implementation of the core Hermes Agent workflow. A named CLI chat sends messages to an OpenAI-compatible model, runs registered tools, and passes their results back until the model replies. SQLite stores completed turns, durable memories, and reusable skills.

## Run on Windows

Requires Python 3.11 or later and a model endpoint that supports Chat Completions function calling. The local development setup uses Ollama with `qwen3:0.6b`. From this directory, run these commands in PowerShell.

```powershell
python -m local_agent setup --model qwen3:0.6b
python -m local_agent doctor
python -m local_agent chat --session demo
```

On another machine, install and start Ollama, then download the model with `ollama pull qwen3:0.6b`. Setup saves the endpoint and model in `.agent/config.json`. To use a different endpoint, pass `setup --base-url https://your-provider.example/v1 --model your-model`. For authenticated endpoints, set `AGENT_API_KEY` in your environment. `AGENT_BASE_URL` and `AGENT_MODEL` override saved settings. The base URL should stop before `/chat/completions`.

Use `/exit` to leave interactive chat, or supply one prompt on the command line. Reuse `--session` to continue a conversation.

```powershell
python -m local_agent chat --session demo --learn 'Remember that I prefer concise replies.'
python -m local_agent chat --session demo --learn 'Save a skill called release-checklist with these steps. Run tests, review the changelog, and verify package checksums.'
python -m local_agent memory
python -m local_agent skills
python -m local_agent skill release-checklist
python -m local_agent chat --session another 'What reply style do I prefer?'
python -m local_agent sessions
python -m local_agent history demo
```

- `--learn` enables changes to memories and skills. The model receives saved memories and a list of skill descriptions on each turn. It can request the full text of a relevant skill by name.
- `chat --workspace .` enables read-only listing and reading of visible workspace files. Choose a directory whose contents you are comfortable sharing with the configured provider. Chat history, memories, and tool results are included in model requests as needed.
- State lives in `.agent/state.sqlite3`. Use `python -m local_agent --data-dir <directory> chat` for a separate history, memory, and skill library. The latest complete turns are replayed within a fixed character budget.

### Terminal interface

Install the optional terminal UI in your Python environment, then start an interactive chat in Windows Terminal.

```powershell
python -m pip install -e '.[tui]'
python -m local_agent chat --session demo --workspace .
```

The full-screen chat shows recent messages, tool activity, and workspace and learning status. The status panel collapses in narrow windows. Press Page Up or Page Down to scroll, or type `/help`, `/sessions`, or `/exit` at the prompt. Use `chat --plain` for the original line-by-line prompt. Redirected input and one-shot prompts use plain output automatically.

The terminal UI includes a hologram that reacts to model responses and tool activity. Use `--no-animation` for a static core or `--hologram off` to hide it. During chat, `/hologram` lists controls for visibility, size, renderer, rotation, and animation. Command output dismisses the welcome panel so it remains readable in short terminals. Paused rendering still updates to show the latest runtime event.

ASCII and Unicode rendering use the Python terminal UI. For optional WebGPU rendering, install Node.js/npm and run `npm --prefix local_agent/graphics ci` from the project directory; this installs the worker dependencies and a local Bun runtime. Then start chat with `--hologram enhanced`. A compatible local terminal and GPU are required; unavailable or failed graphics fall back to terminal cells. Optional Kitty image rendering is available through `python -m pip install -e '.[graphics]'` and `--hologram image`.

## MVP behavior

The CLI supports saved model settings, connection checks, named sessions, transcript viewing, persistent memory, skill creation and retrieval, and workspace reading. Memory and skill changes are committed together with the completed turn. Provider failures or tool-loop exhaustion discard pending changes. A concurrent update to the same session is rejected so its history stays consistent.

The MVP holds up to 20 memories and 20 skills per data directory. A turn allows six tool rounds and up to four calls per round. Workspace reads return the first 2,400 characters, and directory listings return up to 100 entries. Recent context is limited to 20 completed turns and 48,000 serialized characters. Full transcripts remain available through `history`. These are character limits, so the chosen model must have enough context capacity for the request. The small local model has limited reasoning and instruction-following ability. Model-generated memories and skills can be inspected with the CLI commands above.

## Install and verify

The base module runs directly from source using Python's standard library. For a `local-agent` command, install the project into your Python environment.

```powershell
python -m pip install -e .
local-agent doctor
python -m unittest discover -s tests -v
```

Live verification with Ollama and `qwen3:0.6b` covered memory creation, recall in another session, skill creation, loading the saved skill, and reading a workspace file through interactive chat. Session listing and transcript viewing also passed. Test prompts used Qwen's `/no_think` suffix. Automated regression checks cover persistence, rollback, disabled learning, stale session writes, tool limits, context retention, and workspace boundaries.

## Hermes reference

The implementation follows Hermes's documented model/tool loop, persistent sessions, and on-demand skill loading. The upstream conversation loop, tool registry, and session store were inspected at revision `1fcc57e6d23052e46265efc30b57c7e9a971e08b`. This project implements its own CLI, provider adapter, and data format. [Hermes architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture) · [Upstream source](https://github.com/NousResearch/hermes-agent/tree/1fcc57e6d23052e46265efc30b57c7e9a971e08b)
