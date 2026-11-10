# Stabilization results

Verification date: 2026-11-10 (Asia/Bangkok).

## Project workspace (unreleased)

Work on top of v0.3.3: project registry, per-project conversations, Markdown notes, brainstorming, context selection, decisions, tasks and implementation handoff ([PROJECTS.md](PROJECTS.md)). Checked in a Linux sandbox with Python 3.10 and Textual 8.2.8 (the packages from the checkout's virtual environment), not on the Windows host.

| Check | Result | Evidence or limit |
|---|---|---|
| Full regression suite | PASS with environment errors | 220 tests ran: no failures; two optional image-renderer checks skipped. Two errors are environmental: `test_docs` and `test_release` import `tomllib`, which Python 3.10 lacks (the project requires 3.11). They need a rerun on Python 3.11+. |
| New project tests | PASS | 25 core tests (`tests/test_projects.py`) and 11 headless UI tests (`tests/test_tui_projects.py`). |
| Headless previews | PASS (headless only) | Project home, notes, editor, switcher, save dialog and chat with a context line rendered to text at 120 × 36, 80 × 24 and 60 × 18 and were inspected. Clipping found at 80 and 60 columns was fixed. |
| Windows native terminal | PENDING | Not run. Try the commands in [PROJECTS.md](PROJECTS.md#try-it) in Windows Terminal, including `f5`–`f9`, the editor's `ctrl+s` and a narrow window. |
| Live model with project context | PENDING | Scripted providers only; no live Ollama or hosted model was used for these checks. |

## v0.3.3 acceptance follow-up

Reviewed release: `v0.3.3`, commit `2af4ebd62867b194ab61b6a33504edb886a4771e`. The earlier v0.3.2 stabilization record is retained below; its live-provider results are historical, not new v0.3.3 checks.

| Check | Result | Evidence or limit |
|---|---|---|
| Local regression suite | PASS | 184 tests ran with zero failures and two optional image-renderer checks skipped. |
| Published wheel integrity | PASS | The previously downloaded release wheel matches its SHA256SUMS entry and v0.3.3 metadata. SHA-256: `1fb67821b1988ea53ff3b289a9108ea4bc753fd26112cae2461ef7039ae2ab26`. |
| Installed package provenance | PASS | Python 3.11.9, isolated mode, `.agent/published-v033-env`; imports resolve to that environment's `site-packages`, version 0.3.3. |
| Installed-wheel coding workflow | PASS | Ran `scripts/workflow_check.py --native-clipboard --ollama qwen3:0.6b` in isolated mode from outside the checkout. Attachments, approve/decline, actual workspace writes, artifacts, write outcomes, transcript and manual task resume passed. |
| Windows clipboard | PASS | Both the reply and code block matched the native clipboard, including Unicode and indentation. This run replaced the clipboard contents. |
| Live Ollama chat and resume | PASS | `qwen3:0.6b` answered through the installed-wheel headless UI, and the conversation reloaded after restart. |
| Installed-wheel targeted regressions | PASS | 46 tests, zero failures or skips: provider wire formats and errors/retries, files/artifacts, search, shell cancellation/timeouts, responsive sidebar, narrow attachment layout, and UI cancellation. Hosted, Brave and image responses in these tests are simulated. |
| Release CI and publication | PASS | [Release run 42761221294](https://github.com/Hoeur/local-agent-cli/actions/runs/42761221294) passed all 19 test-matrix jobs, three installed-wheel jobs, build and publish. [v0.3.3](https://github.com/Hoeur/local-agent-cli/releases/tag/v0.3.3) includes the wheel and checksum manifest. |
| Hosted tool calling against a live service | BLOCKED | No supported hosted-provider key is available in the process environment or Local Agent key store; both inspected checkout configurations contain only Ollama profiles. Provider/model selection and credentials are required. |
| Live Brave Search API | BLOCKED | Neither `BRAVE_SEARCH_API_KEY` nor the `brave-search` key-store entry is configured. |
| Live image generation and preview | BLOCKED | No image model, endpoint or dedicated image key is configured. Simulated artifact tests do not establish live generation or visual preview acceptance. |
| Windows native terminal interaction | PENDING | Headless and clipboard checks passed, but no real-terminal interaction was performed. The available computer-use skill prohibits terminal-app automation; a manual acceptance run is required. |
| Linux/macOS native terminal interaction | PENDING | No Linux/macOS desktop terminal was available for this run. CI is headless; installed WSL distributions do not establish desktop terminal acceptance. |

The targeted installed-wheel run loaded `test_providers`, `test_files`, `test_tools.SearchProviderTests`, `test_tools.TerminalTests`, and four `test_tui.AppTests` cases: `test_resize_and_sidebar`, `test_all_attachment_chips_are_reachable_in_a_narrow_terminal`, `test_cancel_discards_the_turn_and_keeps_input_live`, and `test_cancel_closes_the_action_dialog`. Only the tests directory was added to the isolated interpreter's import path.

Remaining live checks and their success criteria are in [RELEASING.md](RELEASING.md#live-provider-acceptance). Missing configuration is not a service failure or a passing result.

## Earlier stabilization record

Base: v0.3.2, commit `0815413`. The following environment notes and check results describe the pre-release run.

## Environment alignment

- The Documents checkout was fast-forwarded by 15 commits to the current main branch before this work.
- Both checkouts contained the v0.3.2 source. The global Python installation still advertised v0.3.1 and pointed at the E: checkout; the Documents virtual environment advertised v0.1.0.
- Both Python installations were refreshed from the Documents checkout with the `tui` extra. Isolated import now resolves to that checkout and reports v0.3.2. Existing running chats must be restarted to load it.
- An old local `build` directory contained removed modules. It was archived outside the build path before rebuilding. Release validation now rejects wheels whose package files differ from the source tree.

## Checks

| Check | Result | Evidence or limit |
|---|---|---|
| Release validation tests | PASS | Five tests cover matching checksums, tag mismatch, stale/missing wheels, metadata/assets and stale package files. |
| Local regression suite | PASS | 183 tests completed, with two optional image-renderer checks skipped in the development virtual environment. The five release-validation tests were also run separately after the final validation change. |
| Workflow lint | PASS | actionlint 1.7.12 accepted the test and release workflows. |
| Clean Windows wheel install | PASS | Fresh Python 3.11.9 virtual environment imported the package from site-packages. |
| Installed-wheel coding workflow | PASS | Attachment submission, approved writes, declined writes/commands, artifacts, write outcome records, copying, transcript and manual TODO resume. |
| Windows desktop clipboard | PASS | `--native-clipboard` checked both the full reply and a code block with Unicode and indentation through the Windows clipboard API. |
| Live Ollama chat and resume | PASS | `qwen3:0.6b`, through the installed-wheel workflow. |
| Live web extraction | PASS | `qwen3:4b` called `web_extract` for `https://example.com`, received the actual page result and reported its title. |
| Live shell tool | PASS | `qwen3:0.6b` called `terminal` with the approved command `echo LOCAL_AGENT_SHELL_OK`, received exit code 0 and reported the actual output. |
| Live web search | PASS (fallback) | `qwen3:0.6b` called `web_search` and received labeled English Wikipedia results after DuckDuckGo blocked automated search. This does not verify unrestricted DuckDuckGo results or Brave. |
| Live image input and cached replay | PASS | `qwen3-vl:2b` identified a generated red square as red, then recalled it after the original PNG was deleted and state reopened. |
| Hosted tool calling | PENDING | No hosted provider credentials configured. |
| Brave Search API | PENDING | No Brave key configured. |
| Image generation | PENDING | No image model/endpoint configured. Local vision models do not provide image generation. |
| Linux/macOS desktop terminals | PENDING | No desktop terminal sessions available on the Windows verification host. CI now includes these operating systems, using a headless driver. |
| Actual release publication | COMPLETE IN FOLLOW-UP | v0.3.3 publication and its validation jobs passed; see the acceptance follow-up above. |

An initial `qwen3:4b` shell check returned an incomplete response with a 1,024-token output cap; the shell check passed with `qwen3:0.6b` and a 2,048-token cap. An initial search prompt mistakenly requested an unsupported `limit` argument; the corrected query-only prompt passed. These smoke results establish the listed combinations, not uniform reliability across models. Missing credentials are not passing API checks. Headless UI coverage and native clipboard checks do not establish visual acceptance in a desktop terminal.

## Next acceptance work

- [x] Align the source checkout and editable Python installations.
- [x] Validate a clean wheel and the Windows clipboard/session workflow.
- [x] Check live Ollama shell execution, web extraction, labeled search fallback, vision input and cached replay.
- [x] Add reusable CI, macOS coverage, tag/version checks, wheel checksums and tag-triggered GitHub release publishing.
- [ ] Run hosted tool-calling, Brave and image-generation checks once credentials and endpoints are configured.
- [ ] Complete the Windows/Linux/macOS native terminal checklist in [RELEASING.md](RELEASING.md).
- [x] Exercise release publication: v0.3.3 passed validation and was published with its wheel and checksum.
