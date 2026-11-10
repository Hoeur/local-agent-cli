# Stabilization results

Verification date: 2026-11-10 (Asia/Bangkok). Base: v0.3.2, commit `0815413`.

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
| Actual release publication | PENDING | No new release tag has been pushed. Publication runs only for a future matching tag after validation succeeds. |

An initial `qwen3:4b` shell check returned an incomplete response with a 1,024-token output cap; the shell check passed with `qwen3:0.6b` and a 2,048-token cap. An initial search prompt mistakenly requested an unsupported `limit` argument; the corrected query-only prompt passed. These smoke results establish the listed combinations, not uniform reliability across models. Missing credentials are not passing API checks. Headless UI coverage and native clipboard checks do not establish visual acceptance in a desktop terminal.

## Next acceptance work

- [x] Align the source checkout and editable Python installations.
- [x] Validate a clean wheel and the Windows clipboard/session workflow.
- [x] Check live Ollama shell execution, web extraction, labeled search fallback, vision input and cached replay.
- [x] Add reusable CI, macOS coverage, tag/version checks, wheel checksums and tag-triggered GitHub release publishing.
- [ ] Run hosted tool-calling, Brave and image-generation checks once credentials and endpoints are configured.
- [ ] Complete the Linux/macOS native terminal checklist in [RELEASING.md](RELEASING.md).
- [ ] Exercise release publication with the next intentional version/tag; existing releases remain untouched.
