# Epic template

Two files. Copy them into a new epic directory, fill in the angle-bracket
placeholders, delete the guidance comments.

```
epics/epic-<N>-<slug>/
  epic.md      <- copy of epic.md from this directory
  01.md        <- copy of NN.md, renamed to a zero-padded task number
  02.md
```

`epic.md` is the overview a human reads. The numbered files are the tasks, and
they are what `scripts/import-epic-tasks.py` reads to create one GitHub
Discussion per task.

## The part that has to be exact

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
