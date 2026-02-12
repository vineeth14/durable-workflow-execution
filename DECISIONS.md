# Workflow System Evaluation

## What Libraries/Tools Did I Evaluate?

### Temporal

Provides sub-step checkpointing, saga-pattern rollbacks, multi-node worker pools, and battle-tested crash recovery. The downside? Serious operational overhead. I'd need to run a Temporal server (usually Docker), learn their SDK's programming model (activities vs workflows vs task queues), and work through a pretty steep learning curve. For a 2-day prototype, I'd burn most of my time on setup. Don't get me wrong—for production systems with long-running steps and gnarly failures, Temporal is the right call.

### Inngest

Serverless and event-driven with a really clean developer experience. Handles durable execution, retries, and step-level state automatically. The problem? It abstracts away the exact mechanics this assignment wants me to demonstrate—idempotency, atomic commits, crash recovery.

### BullMQ

Solid Redis-backed job queue, great for task distribution and retry logic. But BullMQ models individual jobs, not multi-step workflows with dependency graphs. I could layer workflow orchestration on top, but then I'm basically writing the same coordination logic I wrote anyway—plus now I'm managing Redis. SQLite gave me zero-dependency persistence without all the extra moving parts.

---

## Why Did I Choose What I Chose?

I went with **SQLite + custom execution loop** because:

- It makes durability guarantees explicit and inspectable - you can literally watch the DB state change via the live viewer
- Zero infrastructure - just `uv sync && uv run python main.py` and you're running
- It eliminates accidental complexity while maintaining flexibility - step-level state machines, write-ahead idempotency keys, atomic commit boundaries for business logic, and a startup recovery routine. All complexity is essential (crash recovery, idempotency, atomicity).

---

## What Would I Do Differently With More Time?

**Use Temporal** - Temporal would allow for sub-step checkpointing, saga-pattern rollbacks, and multi-node workers. At that point it would be smarter to just adopt it than to maintain a custom engine with those features.

**Migrate to Postgres** - Migrating to Postgres would unlock concurrent writes, distributed deployment with multiple workers, and advisory locks for run ownership. The SQL would stay pretty much the same; it's mostly about connection and transaction management.

**Parallel step execution** - The topological sort already knows which steps are independent. A thread pool could fire off all steps whose dependencies are met. The atomic per-step writes mean crash recovery semantics don't change at all.

**Separate worker processes** - Decoupling execution from the API would allow them to scale independently, so workflows could keep running even if the API restarts.

**WebSocket/SSE** - SSE would actually be simpler: it could push step status changes as they happen, and the browser would reconnect automatically. I went with polling because it keeps the connection lifecycle simple and was enough for the project scope.

**Configurable retry policies with exponential backoff** - This would include initial delay, backoff multiplier, max delay, and jitter so we're not hammering struggling external services all at once.

**External idempotency key propagation** - The system could pass the step's idempotency key to external services to prevent duplicate calls on recovery.

---

## What Are the Limitations?

#### Single-node, in-process execution

- SQLite is file-based with no network access, so I can't distribute work across multiple machines. All workflow execution happens inside the API process—if that process goes down, no workflows run. Scaling beyond one server means migrating to Postgres and running separate worker processes that poll for pending runs.

#### No parallel step execution

- Steps run sequentially in topological order even when they're totally independent. The ecommerce pipeline takes ~25 seconds sequentially but could finish in ~15 with parallelism. This was a deliberate trade-off—sequential execution makes crash recovery dead simple (just iterate steps in order, skip the completed ones) and avoids any concurrent write headaches in SQLite.

#### No sub-step durability

- Checkpointing happens at step boundaries. If a step takes 5 minutes and crashes at minute 4, the whole thing re-runs from scratch. For the simulated tasks here, it should be fine. For production workloads with long-running steps, this is where Temporal would be useful—it checkpoints after every single side effect within a step.

#### External side effects might duplicate on recovery

- The atomic transaction model guarantees that internal database writes and step completion commit together. But if a step makes an external API call (HTTP request, email) and crashes after the call succeeds but before the transaction commits, recovery will re-execute the step and repeat that external call. Real payment or notification integrations would need to pass idempotency keys to the external service.

#### No authentication or multi-tenancy

- Any client can create workflows, start runs, view all data, and nuke the database. Production obviously needs user authentication, per-user workflow isolation, and role-based access controls.

#### Polling-based UI updates

- Each open browser tab hits the API every 1.5 seconds. Works fine for a demo, but doesn't scale. WebSockets or SSE would only push updates when state actually changes.
