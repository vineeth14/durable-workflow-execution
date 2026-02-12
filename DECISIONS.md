# Decisions

## What Libraries/Tools Did I Evaluate?

- **Temporal**: Production-grade but heavy. Requires a Temporal server (Docker), SDK setup, and understanding its programming model (activities, workflows, task queues). Overkill for a 2-day prototype — most of the time would be spent on setup, not demonstrating understanding.
- **Inngest**: Serverless-oriented, event-driven. Would abstract away the durability mechanics that the assignment asks me to demonstrate.
- **DBOS**: Interesting transactional approach but relatively new. Would tie the implementation to their specific patterns.
- **BullMQ**: Redis-backed job queue. Good for task queues but doesn't naturally model multi-step workflows with dependency ordering.

## Why Did I Choose What I Chose?

I chose **SQLite + custom execution loop** because:

1. It makes the durability guarantees explicit and inspectable (you can literally watch the DB via the live viewer)
2. Zero infrastructure — `uv sync && uv run python main.py` and it works
3. The code directly demonstrates understanding of the concepts: idempotency, atomic commits, crash recovery, state machines
4. SQLite WAL mode handles concurrent reads from the API while background threads write

Other choices:

- **FastAPI (sync)**: Lightweight, great for prototyping. Sync-only (no async/await) keeps the code simple and avoids threading pitfalls with SQLite.
- **Vanilla JS frontend**: No build step, no npm, no React. Three HTML files with inline JS. The assignment says "functional and clear is sufficient" — a build toolchain would add setup friction for no benefit.
- **Polling over WebSockets**: The frontend polls every 1.5s. For a demo with <10 concurrent users, this is simpler and sufficient. WebSockets/SSE would be better at scale.
- **Daemon threads**: Background execution uses Python daemon threads. They're simple but die with the process — which is actually what we want, since the recovery system handles restarts.
- **Immediate retries**: Failed steps retry immediately with no backoff. Fine for simulated tasks, but real external APIs would need exponential backoff.
- **Sequential execution**: Steps run one at a time even when dependencies would allow parallelism. This keeps the execution model simple and the durability guarantees straightforward.

## What Would I Do Differently With More Time?

- **Migrate to Postgres** — unlock concurrent writes, distributed deployment with multiple worker processes, and advisory locks for run ownership. The SQL is nearly identical; the migration is mostly connection and transaction management.
- **Parallel step execution** — the topological sort already identifies independent steps. A thread pool would dispatch all steps whose dependencies are satisfied after each completion. Atomic per-step writes mean crash recovery semantics don't change.
- **Separate worker processes** — decouple execution from the API so they scale independently and workflows keep running if the API restarts.
- **WebSocket/SSE** — SSE would be the simpler upgrade: push step status changes as they occur, browser reconnects automatically. I went with polling because it eliminated connection lifecycle bugs and was sufficient for the demo.
- **Configurable retry policies with exponential backoff** — initial delay, backoff multiplier, max delay, and jitter to avoid thundering herds against struggling external services.
- **External idempotency key propagation** — pass the step's idempotency key to external services (most payment APIs support this natively) to prevent duplicate calls on recovery.
- **Evaluate Temporal or Inngest** — for a production system needing sub-step checkpointing, saga-pattern rollbacks, and multi-node workers, you're essentially rebuilding what Temporal provides. Inngest would be worth evaluating as a lighter-weight alternative.

## What Are the Limitations?

**Single-node, in-process execution.** SQLite is file-based and doesn't support network access, so the system can't distribute work across multiple machines. All workflow execution happens inside the API process — if that process is down, no workflows run. Scaling beyond one server would require migrating to Postgres and introducing a separate worker process that polls for pending runs.

**No parallel step execution.** Steps run sequentially in topological order even when they have no dependency relationship. The ecommerce pipeline takes roughly 25 seconds sequentially but could complete in approximately 15 seconds with parallelism. This was a deliberate simplicity trade-off — sequential execution makes crash recovery trivial (iterate steps in order, skip completed ones) and eliminates concurrent write contention in SQLite.

**No sub-step durability.** Checkpointing happens at the step boundary. If a step takes 5 minutes and crashes at minute 4, the entire step re-executes from scratch. For the simulated tasks in this system (1-5 seconds each), this is a non-issue. For production workloads with long-running steps, this is where a framework like Temporal adds real value — it checkpoints after every side effect within a step.

**External side effects may duplicate on recovery.** The atomic transaction model guarantees that internal database writes and step completion are committed together. But if a step makes an external API call (HTTP request, email send) and crashes after the call succeeds but before the transaction commits, recovery will re-execute the step and repeat the external call. Real payment or notification integrations would need to pass idempotency keys to the external service.

**No authentication or multi-tenancy.** Any client can create workflows, start runs, view all data, and reset the database. A production system needs user authentication, per-user workflow isolation, and role-based access controls.

**Polling-based UI updates.** Each open browser tab makes an API request every 1.5 seconds. This works fine for a demo but doesn't scale — 100 users watching runs means 67 requests per second just for status polling. WebSockets or SSE would push updates only when state actually changes.
