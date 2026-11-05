# Local Agent

A standalone Python CLI agent with persistent conversations, memory, and reusable skills. A named chat sends messages to an OpenAI-compatible model, runs registered tools, and passes their results back until the model replies. SQLite stores completed turns, durable memories, and reusable skills.

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

The sidebar also shows a live system monitor: CPU usage and recent history, logical CPU count, used/total RAM, free disk space on the workspace drive (or the state drive when no workspace is selected), and the chat app's RAM usage. It refreshes once per second in a background worker and pauses sampling when the sidebar is hidden. Short windows prioritize these readings over the sidebar hologram; the sidebar scrolls when needed and collapses below 80 columns or 18 rows. Reinstall `python -m pip install -e '.[tui]'` to include the `psutil` dependency. Unavailable counters show `N/A`. App RAM refers to the chat process; a separate model server is reflected in total system RAM. Metrics stay in the local UI and are not sent to the model or saved in chat history. Collection uses the documented [psutil CPU, memory, and disk interfaces](https://psutil.readthedocs.io/stable/).

ASCII and Unicode rendering use the Python terminal UI. For optional WebGPU rendering, install Node.js/npm and run `npm --prefix local_agent/graphics ci` from the project directory; this installs the worker dependencies and a local Bun runtime. Then start chat with `--hologram enhanced`. A compatible local terminal and GPU are required; unavailable or failed graphics fall back to terminal cells. Optional Kitty image rendering is available through `python -m pip install -e '.[graphics]'` and `--hologram image`.

## Attachments and artifacts

Attach local images or UTF-8 text files to a message with repeated `--attach` options:

```powershell
python -m local_agent chat --session reports --attach .\notes.txt 'Summarize these notes and create report.md.'
python -m local_agent chat --session vision --attach .\photo.png 'Describe this image and create a Markdown report.'
```

Image input needs a vision-capable chat model at your configured endpoint. The example `qwen3:0.6b` setup is for text chat; select a suitable model before attaching images. PNG, JPEG, WebP, and GIF inputs are sent as base64 image content parts. Text attachments are included as labeled data in the message. The selected provider receives these files with the conversation.

In interactive chat, use `/attach PATH` to queue a file, `/attachments` to inspect queued files, and `/detach` to clear them. Paths with spaces may be quoted. Files are read when you submit a message. Queued files clear after a successful turn and remain available for retry after a failure. Image snapshots are cached under `.agent/attachments/`, so completed conversations can replay them after the original file moves. Base64 image data is kept out of the SQLite transcript.

Ask the agent to create an artifact by filename, such as `report.md`, `table.csv`, or `dashboard.html`. The `create_artifact` tool writes HTML, Markdown, plain text, CSV, JSON, and SVG. Generated files receive unique IDs and live under `.agent/artifacts/`; creating another file with the same name makes a new version. The workspace remains read-only. Ordinary turn failures and interrupted plain chat remove files created by the failed turn; completed artifacts remain available across sessions. Abrupt process termination can leave provisional files on disk.

```powershell
python -m local_agent artifacts
python -m local_agent artifact ARTIFACT_ID
python -m local_agent artifact ARTIFACT_ID --open
```

The equivalent chat commands are `/artifacts`, `/artifact ID`, and `/open ID`. Opening a preview is an explicit user action. HTML and SVG render in a sandboxed browser frame with inline scripts allowed, separate from the parent page; external resources, network requests, forms, and local file access are restricted. Use self-contained HTML with inline CSS and JavaScript for interactive artifacts. Text, Markdown, CSV, and JSON previews show their source; images display as images. This first version does not export PDF, DOCX, or XLSX, execute generated programs, edit uploaded images, or show a built-in graphical canvas.

### Optional image generation

Image generation is separate from chat and remains disabled until an image model is configured:

```powershell
python -m local_agent setup --base-url https://your-chat-provider.example/v1 --model YOUR_CHAT_MODEL --image-base-url https://your-image-provider.example/v1 --image-model YOUR_IMAGE_MODEL
```

Set `AGENT_IMAGE_API_KEY` in your environment for an authenticated image endpoint. `AGENT_IMAGE_MODEL` and `AGENT_IMAGE_BASE_URL` override saved image settings. If the image base URL is omitted, it uses the chat base URL. The chat API key is reused only when the two base URLs match; different providers need their own image key. Keys are never saved in configuration.

Then ask for an image, for example, `Generate a bird illustration and save it as bird.png`. The agent exposes `generate_image` only when configured. The endpoint must support `POST /images/generations` with `model`, `prompt`, `n: 1`, and `output_format`, and return image bytes as `data[0].b64_json`. PNG, JPEG, WebP, and GIF output is supported when the chosen image model supports the requested format. URL-only responses are rejected. Model and endpoint support vary; configuring a text model alone does not provide image generation. Request formats follow the official [vision input](https://developers.openai.com/api/docs/guides/images-vision?api-mode=chat) and [image generation](https://developers.openai.com/api/reference/resources/images/methods/generate) API documentation.

Limits: four attachments per message, 5 MB per file, 10 MB of images per message, and 12,000 combined text attachment characters. Image replay is capped at 10 MB per request; older images beyond this budget are replaced with a textual omission notice. Text artifact content is capped at 24,000 characters, image artifacts at 10 MB, and artifact reads at 12,000 characters. All files are local to the chosen `--data-dir`.

## MVP behavior

The CLI supports saved model settings, connection checks, named sessions, transcript viewing, persistent memory, skill creation and retrieval, workspace reading, file attachments, generated artifacts, and optional image generation. Memory and skill changes are committed together with the completed turn. Provider failures or tool-loop exhaustion discard pending changes. A concurrent update to the same session is rejected so its history stays consistent.

The MVP holds up to 20 memories and 20 skills per data directory. A turn allows six tool rounds and up to four calls per round. Workspace reads return the first 2,400 characters, and directory listings return up to 100 entries. Recent context is limited to 20 completed turns and 48,000 serialized characters. Full transcripts remain available through `history`. These are character limits, so the chosen model must have enough context capacity for the request. The small local model has limited reasoning and instruction-following ability. Model-generated memories and skills can be inspected with the CLI commands above.

## Install and verify

The base module runs directly from source using Python's standard library. For a `local-agent` command, install the project into your Python environment.

```powershell
python -m pip install -e .
local-agent doctor
python -m unittest discover -s tests -v
```

Live verification with Ollama and `qwen3:0.6b` covered memory creation, recall in another session, skill creation, loading the saved skill, and reading a workspace file through interactive chat. Session listing and transcript viewing also passed. Test prompts used Qwen's `/no_think` suffix. Automated regression checks cover persistence, rollback, disabled learning, stale session writes, tool limits, context retention, and workspace boundaries.

Attachment and artifact checks cover cached image replay, text and binary limits, file boundaries, failed-turn cleanup, versioning, preview isolation, and terminal UI retry. A local HTTP test endpoint verifies the CLI through multimodal requests, artifact creation, image generation, and saved history. These checks do not establish live vision or image generation support for your provider.
