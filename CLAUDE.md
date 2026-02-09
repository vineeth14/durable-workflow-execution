# Claude Code Instructions

## Before Starting Any Phase

1. **Read TECH_SPEC.md** — contains full requirements, database schema, API spec, state machines, and design details.
2. **Read PROGRESS.MD** — shows what has been built so far, current active phase, file statuses, and verification checkpoints.

## Branching Strategy

- Each phase lives on its own branch (e.g., `phase-1/setup-and-database`).
- Merge to main when the phase is complete.

## Tooling

- This is a **uv project**. Use `uv add` for dependencies, `uv run` to execute scripts.
- Database: SQLite, file-based (`workflow.db` in project root).
- Backend: FastAPI (synchronous Python, no async/await).
