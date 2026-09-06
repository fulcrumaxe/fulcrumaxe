---
# Every field below is read by scripts/import-epic-tasks.py. Delete the
# comments once you have filled the values in — they are here for the first
# read, not for the file's whole life.
#
# epic / task    the epic number and the task number within it. `epic` must
#                match the number in the directory name and in epic.md's H1.
#                `task` should match this file's basename (03.md -> task: 3).
#                Together they become the Discussion title and the key that
#                other tasks' `depends_on` resolves against.
epic: 1
task: 1
# title          one line, no trailing period. Quote it — titles with a colon
#                or a leading bracket are not valid unquoted YAML.
title: "<one line — what this task delivers>"
# type           becomes a label verbatim, and is capitalised into the
#                Discussion title as [Feature], [Enhancement], and so on.
#                Common values: feature, enhancement, bugfix, refactoring,
#                testing, spec.
type: feature
# status         not-started | in_progress | completed | superseded.
#                Only not-started and in_progress are imported by default.
#                A task that is already done stays in the file with
#                status: completed — it is history, not clutter.
status: not-started
# estimated_hours  a number, becomes the est-<N>h label. Estimate the whole
#                task including tests. If it is over about 12, it is two tasks.
estimated_hours: 4
# depends_on     task numbers within this same epic, as a list. [] if none.
#                The importer rewrites these into Discussion links after all
#                the tasks in a run have been created.
depends_on: []
# tags           free-form. Keep epic-<N> first so a tag search finds the
#                whole epic, then the areas this task touches.
tags: [epic-1, area]
---

# Task: <same title as the frontmatter>

## Overview

**Type:** <Feature | Enhancement | Bugfix | Refactoring | Testing | Spec> Task
**Target:** <one or two sentences: the outcome, stated as something a user or
operator can observe. Not "add a module" — "the operator can see which sessions
are active and end one of them".>
**Location:** `<primary file>`, `<second file>`

---

## 1. Assessment

<Both lists are checkboxes on purpose. "What exists now" is checked for things
that are already true and unchecked for gaps — so the same list shows the
starting position and what is missing, and a reader can tell at a glance
whether the task has been re-scoped since it was written.>

**What exists now:**
- [x] <something that already works, and is load-bearing for this task>
- [ ] <a gap — something the task description assumes but that is absent>
- [ ] <another gap>

**What needs to be done:**
- [ ] <a concrete deliverable — a table, an endpoint, a screen, a check>
- [ ] <another, with enough detail that someone else could build it>
- [ ] <keep going until the list covers the whole task>

---

## 2. Implementation Plan

**Approach:**
1. <first step>
2. <second step>
3. <...>

**Files to create:**
- [ ] `<path>` — <what goes in it>

**Files to modify:**
- [ ] `<path>` — <what changes and why>

---

## 3. Acceptance Criteria

<Numbered, checkable, and phrased as observable outcomes. Each one should be
something a reviewer can verify by running the thing, not by reading the diff.
If a criterion can only be confirmed by inspecting code, rewrite it.>

**Done when:**
1. [ ] <observable outcome>
2. [ ] <observable outcome>
3. [ ] <observable outcome>

---

## 4. Implementation Notes

<Advisory, not binding. The place for the things that are true about this task
but do not belong in the criteria: a decision already made and why, an
approach that looks obvious and is wrong, a constraint from elsewhere in the
system, a gotcha found while writing the task.

Delete this section rather than leaving it empty.>

---

**Status:** <Not Started | In Progress | Complete>
