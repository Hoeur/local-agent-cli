# Release procedure

## Check the installation

From outside either checkout, use isolated mode to see the package the interpreter actually loads:

```powershell
python -I -c "import sys, local_agent, importlib.metadata as m; print(sys.executable); print(local_agent.__file__); print(m.version('local-agent-cli'))"
```

Running Python inside a checkout normally loads that checkout's source, which can hide an editable installation pointing somewhere else. Install from the intended checkout with `python -m pip install -e ".[tui]"`. For an isolated development environment, use that checkout's `.venv` interpreter explicitly. Restart running chats after updating.

## Validate a wheel locally

Use a clean checkout or archive the old `build` directory before building. Setuptools can otherwise include modules left behind by an earlier source version.

```powershell
python -m pip install build
python -m build --wheel --outdir dist
python scripts/release_check.py
```

The check requires exactly one wheel in `dist`, with the project version in its filename and metadata, the terminal UI assets, and exactly the current package files. It writes `dist/SHA256SUMS` only after validation. Keep old release wheels elsewhere.

Install the wheel in a fresh virtual environment, then run the existing workflow script with that environment's interpreter, from outside the source tree:

```text
<clean-python> -I <checkout>/scripts/workflow_check.py
<clean-python> -I <checkout>/scripts/workflow_check.py --ollama qwen3:0.6b
```

On Windows, `--native-clipboard` also checks the desktop clipboard and replaces its contents. The script tests approvals and declines, project writes, separate artifacts, attachment submission, copying, and transcript/task-list resume. `--ollama` adds a live greeting and session resume; it does not establish live tool-calling or vision support.

## Publish a stable release

1. Update `project.version` in `pyproject.toml` and the versioned installation examples in the README, then merge the validated change.
2. Push a new tag named `vMAJOR.MINOR.PATCH`, matching the project version.
3. The `release` workflow runs the reusable test matrix and installed-wheel workflow on Windows, Linux and macOS. It then builds and validates the release wheel, checks the tag/version match, and uploads the wheel plus `SHA256SUMS` as a workflow artifact.
4. Only after those jobs pass, the publishing job verifies the downloaded checksum and creates the GitHub release with the wheel, manifest and generated notes. Only this job receives `contents: write`.

Existing releases are not overwritten. A rerun after a successful publication fails at release creation; inspect the existing release rather than replacing its assets. A manually dispatched workflow validates and stores artifacts without publishing, even when dispatched against a tag. Tags are not created by this workflow, and publishing does not upload to PyPI.

Reference: [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows), [workflow artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data), and [`gh release create`](https://cli.github.com/manual/gh_release_create).

## Native terminal acceptance

Run this checklist separately in Windows Terminal/PowerShell, a Linux desktop terminal, and macOS Terminal. Record the OS, terminal, Python version, package version, model and date. CI uses Textual's headless driver and cannot verify these desktop interactions.

- Start the installed wheel in a fresh test directory with a separate data directory; check the model and enabled tool groups.
- Resize across 80, 100 and 120 columns; show/hide the sidebar and check that the composer remains usable.
- Submit an attachment, approve a harmless project write, decline another, and verify the actual files.
- Copy a reply and a code block into a text editor; check line endings, indentation and Unicode.
- Cancel a slow reply and a harmless long-running shell command; check responsiveness and process exit.
- Quit, restart the session, and check transcript and task-list resume.

Hosted tool-calling, Brave search and image generation need their own configured credentials. Use disposable input and state for live checks, record the actual tool results, and distinguish missing configuration from a failing implementation. Never commit keys or provider configuration files.

## Live provider acceptance

Run against the installed release wheel with a separate data directory and a disposable workspace. Record the package version, provider, model, endpoint host, date and actual tool results. Keep prompts and test files synthetic. Do not record key values or authorization headers.

### Hosted tool calling

1. Select the provider/model to verify and configure a dedicated acceptance profile using `local-agent --data-dir <acceptance-data> setup --provider <provider> --model <model>`. Custom providers also require `--base-url` and the appropriate `--wire`.
2. Store credentials with `local-agent key set <provider>` or the provider's supported environment variable. Run `doctor` with the same data directory to check connectivity; a successful doctor result alone does not prove tool calling.
3. Put a newly chosen marker in `marker.txt` inside the disposable workspace. Start chat with that workspace and ask the model to call `read_file` for `marker.txt` and report the marker. Pass only if the recorded tool call reads the file and the final response includes its actual contents.
4. Restart the session and confirm the transcript reloads. Exercise streaming and non-streaming requests if both modes are part of the acceptance scope. Cover error/retry behavior with the deterministic provider tests; do not deliberately exhaust a live account's quota.

### Brave Search

1. Configure `local-agent key set brave-search` or `BRAVE_SEARCH_API_KEY`, then select Brave with `local-agent --data-dir <acceptance-data> web-search brave`.
2. Start the acceptance chat with `--web`. Ask it to call `web_search` with only `query` set to `Python programming language`.
3. Pass only if the actual tool result identifies Brave Search and includes at least one usable result URL. DuckDuckGo or Wikipedia fallback does not satisfy this check. Record authentication, quota or network failures separately.

### Image generation and preview

1. Configure the selected chat profile with `--image-model <model>` and, if needed, `--image-base-url <base-url>`. The image service must accept this client's `/images/generations` request and return inline image bytes in `data[0].b64_json`.
2. For a separate image endpoint, configure `AGENT_IMAGE_API_KEY` if it requires authentication. The chat key is reused only when the normalized chat and image base URLs are identical. Do not assume a vision model supports generation.
3. Request one image named `acceptance.png`: a red square centered on a white background. Pass generation only if the actual `generate_image` tool succeeds, the saved artifact contains decodable image bytes and the filename/type are correct.
4. Open the artifact through the UI or `local-agent --data-dir <acceptance-data> artifact <id> --open`, inspect its appearance, then restart and confirm it remains available. Record visual preview acceptance separately from successful file creation.

### Native terminal handoff

Use the native terminal checklist above on each OS. Start from a fresh directory, configure a model in a separate data directory, and run `local-agent --data-dir <acceptance-data> chat --session acceptance --workspace <disposable-workspace> --write --shell`. Keep approval prompts enabled. For the cancellation check, approve only a harmless sleep command, cancel it and verify its process exits. Record each checklist item as PASS, FAIL or NOT RUN, including terminal name/version and terminal dimensions. Headless rendering and Windows clipboard API checks are supporting evidence, not substitutes for this run.
