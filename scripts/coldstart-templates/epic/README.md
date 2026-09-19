# Epic template

Two files. Copy them into a new epic directory, fill in the angle-bracket
placeholders, delete the guidance comments.

```
epics/epic-<N>-<slug>/
  epic.md      <- copy of epic.md from this directory
  01.md        <- copy of NN.md, renamed to a zero-padded task number
  02.md
```

`epic.md` is the overview a human reads. The numbered/lettered files are the
tasks. `epic.md` and `README.md` are the two reserved non-task names in an
epic directory — nothing else in it is skipped when a task file is read.

## Schema v1

New task files should use schema v1, validated by `backend/task_file.py`:

```
python3 backend/task_file.py validate epics/epic-<N>-<slug>/<TASK>.md
```

Exits 0 when the file is valid, 1 otherwise — one line printed per problem.
`scripts/import-epic-tasks.py` does not read schema v1 yet (a separate
follow-up task teaches it to); today it still reads the v0 field set
documented further down in this file.

| Field | Required | Allowed values |
|---|---|---|
| `schema_version` | yes | `1` |
| `epic` | yes | integer ≥ 1 (the parent Discussion number) |
| `task` | yes | string matching `^[A-Za-z0-9][A-Za-z0-9-]*$`, equal to the filename stem |
| `title` | yes | non-empty string |
| `type` | yes | `feature`, `bug`, `doc`, `infra`, `process` or `security` |
| `status` | yes | `draft`, `ready`, `superseded` or `completed` (authoring states only) |
| `estimated_hours` | yes | number > 0 and ≤ 40 |
| `complexity_points` | yes | 1, 2, 3, 5 or 8 |
| `planned_prs` | yes | integer ≥ 0 (0 means operational, and needs `planned_prs_reason`) |
| `planned_prs_reason` | yes when `planned_prs` is 0 | non-empty string |
| `milestone` | yes | `stage-1`, `stage-2`, `launch` or `post-launch` |
| `security_review` | yes | `true` or `false` |
| `depends_on` | yes (may be `[]`) | each item is a same-epic task ID (`H08`), a cross-epic `<epic>.<task>` (`2.H13`), `D#<n>` or `#<n>` |
| `acceptance_files` | yes when `status: ready` and `planned_prs` ≥ 1 | non-empty list of repo-relative paths |
| `repo` | no | `owner/name`; defaults to the importing repo |
| `discussion` | no | integer — this task's Discussion already exists, so never import it |
| `tags` | no | free-form list |
| `priority` | no | free-form |
| `parallel` | no | free-form |
| `conflicts_with` | no | free-form |
| `parent_task` | no | for a task split out of another one |
| `supersedes` | no | for a task that replaces an earlier one |
| `created` | no | ISO 8601 date |

Any other key in a v1 file is an error (catches typos such as
`estimate_hours`). The body below the frontmatter is Markdown, same as in v0.
A task migrated from elsewhere should start its body with a `Source: ...`
line (content, not itself validated).

`epic.md` may carry its own frontmatter with a single `parent_discussion: <N>`
field; it never carries task fields.

A file with no `schema_version` field is a v0 file — see the next section.
`validate` never fails a v0 file just for lacking v1-only fields; it prints a
`warning:` line for each one instead.

## Schema v0 (legacy — what the importer reads today)

Everything in a task file above the second `---` is YAML frontmatter, and the
importer parses these fields out of it:

| Field | Required | What it becomes |
|---|---|---|
| `epic` | yes | the `epic-<N>` label, and half the Discussion title |
| `task` | yes | the other half of the Discussion title |
| `title` | yes | the Discussion title text |
| `type` | yes | a label, and the `[Type]` prefix on the title |
| `status` | yes | decides whether the task is imported at all |
| `estimated_hours` | yes | the `est-<N>h` label |
| `depends_on` | yes (may be `[]`) | Discussion cross-links, filled in after import |
| `tags` | yes (may be `[]`) | free-form, not currently turned into labels |
| `parent_task` | no | for a task split out of another one |
| `supersedes` | no | for a task that replaces an earlier one |

A task whose Discussion title reads:

```
[Feature] epic-7.3 — Login history and session management
```

came from `epic: 7`, `task: 3`, `type: feature`, and that `title`.

Only `status: not-started` and `status: in_progress` are imported by default.
A completed task keeps its file with `status: completed` — the file is the
record of the work, not a queue entry to be deleted once done.

Everything below the frontmatter is free-form Markdown and becomes the
Discussion body verbatim. The section order in `NN.md` is a convention worth
keeping, not something the importer enforces.

## What the task template does and does not cover

The four sections in `NN.md` are the ones a task carries while it is open.
They are not the whole life of the file.

A **`## Completion Summary`** is the usual fifth section, added when the task
is finished rather than written up front: files changed, what was actually
implemented, time spent against the estimate, how it was verified, what went
sideways, and the commit. It is absent from the template because a
not-started task has nothing to put in it — not because the section does not
exist. Add it when you close the task out.

Two things in the template are deliberately stricter than common practice,
and are choices rather than requirements:

- **Numbered section headings** (`## 1. Assessment` … `## 4. Implementation
  Notes`). Plenty of real task files use unnumbered headings and read fine.
  Numbering them makes "see section 3" mean something in a review comment.
- **The trailing `**Status:**` line.** It duplicates the `status:` field in
  the frontmatter, which is the one the importer actually reads. It is there
  so the state is visible to someone reading the rendered file rather than
  the source.

Neither is enforced anywhere and dropping either breaks nothing. They are
written down here so the next person changing the template knows they are
looking at a decision rather than an accident.

## Checking a file before you import it

The importer will tell you what it would create without touching GitHub:

```
python3 scripts/import-epic-tasks.py <repo-path> --repo <owner/name> --dry-run
```

It prints one line per task it found and the title it would use. A task file
missing from that list is a task file the importer could not read — almost
always a frontmatter problem: a `title` containing a colon and no quotes, a
`---` fence that never closes, or a `status` outside the imported set.

## Numbering

Task files are zero-padded and sequential within their epic: `01.md`, `02.md`,
… `10.md`. The number in the filename should match the `task:` field, and the
epic number in the directory name should match the `epic:` field in every task
under it. Nothing checks this for you, and a mismatch is only visible once the
Discussion titles come out wrong.
