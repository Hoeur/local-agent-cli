# Project knowledge

This folder is the project's knowledge base for Local Agent. Everything here is ordinary UTF-8
Markdown: edit it in any editor, review it in diffs, and commit it with the rest of the project.

- `overview.md` — what the project is and its goals
- `notes/`, `ideas/`, `decisions/`, `tasks/` — one file per entry

Each file starts with front matter between `---` lines. Keep the `id` line as it is: links such as
`[[decision-use-sqlite]]` and the `related:` list refer to ids, so renaming a file or changing its
title does not break them. Tasks also have `status: pending`, `in-progress` or `completed`.
Lists are written like `related: [idea-offline-mode, note-api-limits]`.

Local Agent checks that a file is unchanged before saving over it, so edits made here while the app
is open are never silently overwritten.
