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
| Workflow lint | PASS | actionlint 1.7.12 accepted the test and release workflows. |
| Clean Windows wheel install | PASS | Fresh Python 3.11.9 virtual environment imported the package from site-packages. |
| Installed-wheel coding workflow | PASS | Attachment submission, approved writes, declined writes/commands, artifacts, write outcome records, copying, transcript and manual TODO resume. |
| Windows desktop clipboard | PASS | `--native-clipboard` checked both the full reply and a code block with Unicode and indentation through the Windows clipboard API. |
| Live Ollama chat and resume | PASS | `qwen3:0.6b`, through the installed-wheel workflow. |
| Hosted tool calling | PENDING | No hosted provider credentials configured. |
| Brave Search API | PENDING | No Brave key configured. |
| Image generation | PENDING | No image model/endpoint configured. Local vision models do not provide image generation. |
| Linux/macOS desktop terminals | PENDING | No desktop terminal sessions available on the Windows verification host. CI now includes these operating systems, using a headless driver. |
| Actual release publication | PENDING | No new release tag has been pushed. Publication runs only for a future matching tag after validation succeeds. |

The local live tool/vision checks and full regression run are recorded below when complete. Missing credentials are not passing API checks. Headless UI coverage and native clipboard checks do not establish visual acceptance in a desktop terminal.

## Next acceptance work

- [x] Align the source checkout and editable Python installations.
- [x] Validate a clean wheel and the Windows clipboard/session workflow.
- [x] Add reusable CI, macOS coverage, tag/version checks, wheel checksums and tag-triggered GitHub release publishing.
- [ ] Run hosted tool-calling, Brave and image-generation checks once credentials and endpoints are configured.
- [ ] Complete the Linux/macOS native terminal checklist in [RELEASING.md](RELEASING.md).
- [ ] Exercise release publication with the next intentional version/tag; existing releases remain untouched.
