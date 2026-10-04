# Worktree collaboration

Use in-thread read-only agents for repository mapping, architecture, security, privacy, observability, test-gap, and documentation review. Use one separate Git worktree per writing agent, named `<work-item>-<zone>`, for example `AURA-0127-core`, `AURA-0127-tests`, and `AURA-0127-integration`.

The work item chooses the smallest useful parallelism class:

- `local`: one writer with optional read-only support.
- `standard`: up to eight open spawned subagent threads or several disjoint worktree writers.
- `fanout`: up to fifteen independent worktree chats with a declared dependency graph, integration owner, and non-overlapping writable paths.

There is one contract author until the contract is fixed, one writer per service migration chain, and one Integration Maintainer. Writers may not share a worktree or edit the same files. Shared lockfiles, generated clients, central contracts, databases, fixtures, and ports require serialization or an explicit upstream/downstream assignment.

Assign each worktree a distinct application/test port range, database name or isolated database instance, and temporary-data root. Dependency download caches may be shared when the package manager supports concurrency; build outputs, mutable test state, and databases must be isolated. Limit simultaneous full suites according to host CPU, RAM, disk, GPU, and port pressure. Workers run targeted checks; the integration worktree runs the final repository-wide suite.

Each worker commits only its reviewed scope and supplies a validated handoff. The Integration Maintainer applies commits in declared dependency order, returns semantic conflicts to owners, runs aggregate checks, and cleans stale worktrees and large disposable build outputs after successful integration.
