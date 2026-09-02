# documentation binding

A work item in flight keeps its working documents in `<worktree>/.local/impl/`: the design spec
(`spec.md`), the plan (`plan.md`), the task briefs and reports (`task-N-brief.md`,
`task-N-report.md`), and the progress ledger (`ledger.md`). Git ignores that directory, and its
contents go with the worktree. What survives an item is its commits and its pull request.

Kept documentation is `README.md` and `docs/architecture.md`. The readme says what baton is, how to
run it, and how to configure it. The architecture document says how it works. Update a kept document
whenever a change affects what it describes. The enumeration is the rule, and the set is closed.
Only the operator adds a kept document.

## House style

The workspace root's `documentation` topic holds the house style, and the doc-reviewer judges
against it. This binding adds nothing to it and does not restate it.
