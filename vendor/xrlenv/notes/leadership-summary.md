# XRLEnv — Leadership Summary

*One-page brief. For architecture read `specs/00-overview.md`; for status read `notes/phase-1-to-do.md`.*

## What it is

XRLEnv is the **infrastructure layer for agentic RL training and evaluation**. It
manages agent sandboxes at scale so a training or eval job can dispatch
**hundreds-to-thousands of concurrent, long-horizon rollouts** against real agent
environments (terminal/coding, web, research) — without the job owning any of the
container, image, scheduling, or fault-recovery machinery.

It does two things, and only these two:

1. **Sandboxing** — dedicated, isolated environments an agent acts in (Docker today; microVM planned).
2. **Orchestration** — full lifecycle of those sandboxes across a fleet of cloud VMs, behind one uniform API.

It is deliberately **not** a trainer, model server, or generic code interpreter. The
policy stays trainer-side; XRLEnv is the substrate underneath.

## Why it matters

Agentic RL and eval are bottlenecked on *environment infrastructure*, not models.
Every team re-solves the same hard problems — running thousands of containers
reliably, distributing multi-GB images, isolating rollouts, recovering from node
loss, onboarding each new benchmark. XRLEnv makes that substrate a **shared,
reusable platform**: onboard a benchmark once, run it at fleet scale, reuse it
across training and eval.

## Key features

- **Three-plane architecture** (trainer / control / data) with a clean separation of
  concerns — the trainer knows RL but nothing about sandboxes; the data plane knows
  sandboxes but nothing about RL. This is what lets one platform serve many trainers
  and many benchmarks without coupling.
- **Uniform rollout API** — gym-style `step` lifecycle, hard deadlines, partial-trajectory
  return, and group/batch primitives (`task_key` fairness, `group_id`, `cancel_group`,
  anti-affinity, per-task run caps) that trainer adapters build on.
- **Drop-in benchmark onboarding** — real upstream harnesses (SWE-bench, terminal-bench,
  harbor-family) plug in through *their own* extension points via a `docker.from_env()` →
  `xrlenv.from_env()` swap. No forking the benchmark, no rewriting it into our shape.
- **Fleet-scale image distribution** — per-node LRU image cache, image-affinity scheduling,
  three pluggable distribution modes (registry-digest / per-node-local / shared-storage),
  control-plane-driven builds, and an optional cluster-wide registry mirror.
- **External plug-in mechanism** — third parties `pip install` a new benchmark with zero
  source-tree edits (Python entry-points + namespace packages).

## Platform qualities — the "-ilities"

These are the properties that separate a demo from production infrastructure.

### Fault tolerance
- **Self-healing transport.** Every control→node link is a single outbound bidirectional
  gRPC stream. On disconnect the node reconnects with exponential backoff + jitter and
  **replays unacked commands/replies by sequence** — in-flight work resumes instead of
  being lost. An **idempotency cache** (keyed per command, LRU + TTL) means a replayed
  command executes **exactly once**, never twice.
- **Layered garbage collection.** Five independent GC layers (per-sandbox TTL, node-startup
  sweep, control-plane reconcile-on-reconnect, run-dir rotation, image-cache prune) — each
  layer **assumes the others can fail**, so no leaked sandboxes, orphans, or runaway disk.
- **Node-loss recovery.** When a node drops, the control plane reconciles on reconnect and
  seals affected rollouts as `failed/sandbox_lost` rather than hanging; stale/unrostered
  nodes are auto-pruned on startup. Graceful **drain** lets a node leave cleanly.
- **No inbound listener on data-plane VMs** → cleaner failure reasoning and better firewall
  posture; warm-standby failover on shared state is the phase-2 step.

### Robustness
- **Admission queue that gates load, not the caller.** Any *requested* concurrency (4, 100,
  arbitrarily high) is safe by design — the queue paces work to real capacity so **throughput
  doesn't collapse under oversubscription**. Fail-fast acquire returns capacity-exhausted
  *before* an upstream harness times out, so a task runs once cleanly instead of being
  half-set-up and cancelled.
- **Node is the final authority on its own resources** — slots free only on node-confirmed
  destroy; the scheduler retries elsewhere on rejection, never the other way around.
- **"Concurrency is a trigger, not a cause" discipline.** A failure that appears only under
  load is treated as *surfacing* a latent bug (race / oversubscription / admission hang) or a
  non-hermetic benchmark dependency — the fix is always the root cause, never lowering
  concurrency. This has repeatedly isolated real platform bugs from benchmark-side flakiness.
- **Sandboxed Docker-in-Docker at scale** via sysbox, with a node-level create/destroy
  governor that prevents FUSE wedges under load — hosting multi-service and DinD benchmarks
  other platforms can't.

### Observability
- **Operator-grade metrics.** A rich Prometheus catalog (rollout throughput, step / create /
  destroy latency histograms, capacity utilization, queue depth + wait, admission outcomes,
  image-cache hit rate, per-node hardware) — no external monitoring stack required.
- **Per-rollout debug bundle.** Every rollout owns one directory with its coordinator log,
  node log, in-sandbox stub log, trajectory, and blobs — so "what happened in rollout X" is
  answered in one place, not scattered across global logs.
- **Distributed tracing.** OpenTelemetry spans across the hot path (control plane → node →
  sandbox), off by default with zero hot-path cost, OTLP-exportable in prod.
- **Honest durability.** A published durability matrix states exactly what each trajectory
  sink guarantees after a trainer crash; a `platform-jsonl` durability floor is on by default
  so runs stay replayable. Live admin panel + trajectory viewer for step-by-step inspection.

### Scalability
- **Horizontal by design** — add capacity by appending VMs to `nodes.yaml`; no admin,
  Terraform, or autoscale dependency (a hard constraint of the user's VM-only cloud access).
- **Proven envelope with a clear ladder.** Phase 1 sustains the **100–200 concurrent
  container** target on SQLite (WAL); phase 2's Redis state store lifts that to **500–1k+**,
  and the phase-1/2 stack targets the **~10k concurrent** range.
- **Image distribution scales with the fleet** — affinity scheduling keeps work near cached
  images, and the pluggable distribution modes + optional registry mirror scale past the
  point where naive per-node pulls of hundreds of multi-GB images would dominate.
- **Fleet reservation** admits multi-container tasks against a declared peak footprint so
  large tasks can't deadlock the admission queue against small ones.

### Multi-tenancy
- **Tenant-ready by construction, today.** Every rollout carries `owner_id` / `project_id` /
  `run_id` end-to-end — through the gRPC open message, the SDK `tenant=` arg, every StateStore
  row, admin/CLI filters, and Prometheus labels — defaulting to `"default"` at zero cost. Phase-2
  multi-tenant isolation activates these as enforcement scopes **with no schema migration**.
- **Per-rollout ownership is already enforced.** On the shared-cluster path every command is
  scoped to its rollout and the node-side dispatcher enforces ownership, so **consumer A cannot
  touch consumer B's containers** even when they share nodes.
- **Scoped auth today.** Coarse token scopes plus per-consumer tokens with rotation/revocation
  (shipped); the admin panel is read-only and scoped to its own `owner_id` for viewers, un-scoped
  for operators — the scope comes from the token, not a spoofable request field.
- **Fair-share admission** (`task_key` fairness + per-task run caps) already prevents one tenant
  or one hot task from starving others in a shared queue.
- **Phase 2 completes the story:** OIDC + RBAC + project scoping, per-tenant quotas, and the
  cross-tenant data-isolation guarantees that a fully multi-tenant cluster requires.

## Proof points

- **Broad benchmark coverage carried end-to-end** across SWE-bench Verified, terminal-bench-2,
  harbor-family (TerminalWorld, Long-Horizon Terminal-Bench, seta, coding-bench), deep-swe/pier,
  EvoClaw, and web/browser (WebArena) — spanning the terminal/coding, multi-service, and GUI
  agent space. Oracle-driven correctness gates validate the platform is *faithful* (rewards
  reflect what the upstream solution accomplishes through our plumbing, not our own grading).
- **Runs at real concurrency** on manually-provisioned GCP + AWS VMs (Amazon Linux + Ubuntu)
  with a reproducible one-command bootstrap and no admin/autoscale dependency.

## Status & roadmap

- **Phase 0 (done):** platform end-to-end — Docker backend, gym/step API, SQLite state,
  GCP+AWS VMs, admin panel, capacity estimator, first benchmark onboarded.
- **Phase 1 (near exit):** operationally-honest cluster substrate for agentic-benchmark
  evaluation — drop-in harness onboarding, fleet-scale image distribution, external plug-in
  packaging, observability + auth + security hardening. Exit is a short checklist
  (OTel/auth/bootstrap/mTLS/scale-gate).
- **Phase 2 (next):** Slime + verl trainer adapters, Redis state store (500–1k concurrent),
  CubeSandbox microVM backend, warm pools, k8s/autoscale.
- **Phase 3 (aspirational):** preemption-safe sessions for spot-VM training; extreme density.

**The ask/opportunity:** XRLEnv is the reusable environment substrate that lets us scale
agentic RL and eval without every team rebuilding the same infrastructure. It is proven on
real benchmarks today, fault-tolerant and observable by construction, and one hardening
checklist away from its phase-1 exit.
