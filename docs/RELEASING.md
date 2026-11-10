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
