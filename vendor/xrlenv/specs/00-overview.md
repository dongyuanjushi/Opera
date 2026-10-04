# 00 — Overview

## Purpose

XRLEnv is the infrastructure layer for **agentic RL training**: it manages
agent sandboxes at scale so a consumer can dispatch hundreds-to-thousands of
concurrent long-horizon rollouts against agent environments (terminal/coding,
web search, deep research) without coupling to any single consumer framework.

It does two things, and *only* these two things:

1. **Sandboxing** — create dedicated, well-isolated environments an agent can
   act in (Docker container, CubeSandbox microVM).
2. **Orchestration** — manage the full lifecycle of those sandboxes across
   compute nodes (laptop, reserved cloud VMs, eventually k8s) and expose them
   to a consumer through a single uniform API.

## What this is *not*

- Not a trainer. We do not own gradient steps, optimizers, or model weights.
- Not a model server / inference router. The policy lives consumer-side; the
  SDK passes whatever `policy.act(obs)` callable the user supplies.
- Not a generic "code interpreter as a service" (E2B, Daytona, Modal). We
  borrow E2B's primitives but speak RL semantics on top.

## Three planes

```
Consumer plane  (GPU host, runs policy, consumes trajectories)
       │
       ▼   gRPC bidi-stream (rollout RPC)
Control plane   (orchestrator: schedule, registry, capacity, admin panel)
       ▲
       │   outbound bidi gRPC stream from each node (spec 21);
       │   no inbound listener on the node side (invariant 7)
Data plane      (xrlenv-node daemons running BackendAdapters)
       │
       ▼   in-sandbox stub protocol (uds / vsock)
Sandboxes       (Docker containers, CubeSandbox microVMs)
```

The split is deliberate. The consumer plane knows about RL but nothing about
sandboxes. The data plane knows about sandboxes but nothing about RL. The
control plane is the only thing that knows both, and only in the narrow shape
of "schedule this template, return a rollout session."

## Phase ladder

The phases describe **platform capability progression**, not which agent
benchmarks gate which phase. The agent types listed in each phase below
are *example workloads we'll exercise the platform with* at that point —
not features that block the phase from shipping. Any feature added in a
later phase is, in principle, useful for any agent kind; the listing
just reflects the order we plan to add them.

### Phase 0 — initial platform end-to-end
Capabilities: Docker backend, gym/step API on top of E2B-compatible
primitives, EnvAdapter layer for wrapping benchmark Environment classes,
sqlite state, GCP+AWS reserved VMs (manual provisioning), read-only
admin panel, static + online-refined capacity estimator, live-updating
admin panel, **per-node image cache manager with LRU eviction +
operator-driven `xrlenv warmup` CLI** (spec 15), **idempotent rollout
start, consumer-side heartbeat + idle TTL, group/batch primitives
(`task_key`, `group_id`, `cancel_rollout`, `cancel_group`, group
anti-affinity, per-node `max_runs_per_task`) — engine-specific
over-request / filter loops live in trainer adapters, not core,
per-phase deadline overrides, bounded concurrent destroy, graceful
drain, per-rollout log directories, trajectory viewer (cross-node
locator + sink-aware fetch + step-by-step rollout inspection in the
admin panel — basic version per spec 17)**.
Example workloads: terminal-bench, SWE-bench, OSWorld (representative of
the terminal/coding/GUI agent space).

### Phase 1 — broader platform capabilities
Capabilities: CubeSandbox backend (stronger isolation, chainable
overlaybd snapshots), redis state store, sandbox warm pools, egress
allowlist networking, snapshot / restore, **Slime + verl trainer
adapters**, additional built-in EnvAdapters as needed,
**consumer-driven `client.warmup(...)` SDK and image-affinity
scheduling** (spec 15), shared package cache mounts, **benchmark
analysis & cross-task image consolidation** (`xrlenv analyze`,
plan.yaml, equivalence smoke — spec 16), **Function-Call execution
mode** (`client.invoke`, spec 01), **lazy / on-demand image loading**
(eStargz + overlaybd, spec 15).
Example workloads: search agents, web-browsing agents.

### Phase 2 — scale and depth
Capabilities: k8s / MIG / ASG-based node autoscaling, sandbox
checkpoint / branch, durable trajectory storage (object store),
predictive capacity estimator, multi-tenant isolation.
Example workloads: hours-long deep research / research agents.

### Phase 3 — resilience and extreme density
Capabilities:
- **Sandbox sessions with preemption-safe resume** (spec 18) —
  long-lived sandbox sessions outlive consumer processes; off-node
  command log; step-level cached-result replay anchored on a
  snapshot-cadence correctness contract (no per-call idempotency
  markers); recovery from spot-VM eviction without losing in-flight
  rollouts.
  Phase 0 / 1 / 2 must preserve the design hooks listed in spec 18
  to make this slot in cleanly without rewrite (stub middleware
  seam; sandbox-vs-rollout lifetime decoupling in the data model;
  GC by owner-refcount; snapshot as first-class).
- **Extreme density (aspirational)** for 100k+ sandboxes per
  cluster: KSM / page-cache deduplication across similar microVMs;
  memory overcommit with reclamation; container-runtime tuning
  (crun / youki, cgroupv2 spinlock mitigation); 3FS-equivalent
  shared distributed filesystem for image layers. Most of these
  require admin-level deploy environments or substantial
  engineering work and are out of scope of the current phase plan;
  documented here so the architecture isn't seen to ignore them.

Phase 1 / 2 capabilities get us comfortably to the ~10k concurrent
sandboxes range; phase 3's preemption-safe sessions enable spot-VM
training; the density items would be needed to scale an order of
magnitude beyond that.

## Design invariants

These are cross-spec rules that any future change must preserve.
Each one bakes in a decision other specs depend on; violating one
silently corrupts other parts of the system.

1. **Sandbox identity is distinct from rollout identity.**
   `sandbox_id` and `rollout_id` are separate columns and separate
   lifecycles. Phase 0 happens to destroy the sandbox at rollout
   finish, but no schema, query, or code path may assume
   `sandbox_id == rollout_id`. Spec 18 sessions break the equation;
   designs that bake it in have to be rewritten when sessions land.
2. **Capacity is released only on node-confirmed destroy.** The
   scheduler treats a sandbox slot as occupied until the owning
   node has acknowledged destroy. "Destroy enqueued" is not "slot
   free." Operators tuning iteration cadence size
   `max_concurrent_destroys` (spec 04) accordingly.
3. **A trajectory is immutable after seal.** Once a sink emits
   `seal(...)`, the record set for that rollout is frozen. Late
   reward updates from an `external_final` reward service write a
   *new* record set keyed by `rollout_id` + `re-seal`, never
   mutate the original.
4. **A template manifest is immutable by `(name, version, digest)` for the duration of a training run.** The control
   plane pins the digest at run start; later edits to the same
   `(name, version)` are visible only on the next run. Spec 16
   `plan.yaml` consolidations follow the same rule.
5. **`ResolvedInstance` is immutable for the life of a rollout.**
   The instance resolver runs once at admission; the resulting
   image refs, asset refs, mounts, and per-instance resource spec
   are pinned through cancel/retry/resume.
6. **Node agent is the final authority on local resource availability.** The scheduler's view (last heartbeat) and the
   node's view (live cgroup state) can disagree under load. The
   node may reject any placement with `OverCapacity`; the
   scheduler retries elsewhere. Never the other way around.
7. **Outbound-only node transport.** All control-plane → node
   communication rides the bidi gRPC stream the node initiates.
   No spec may add an inbound listener on the node-agent
   without an explicit phase note and a corresponding update to
   spec 04 / spec 07 / spec 09.
8. **State store holds metadata; blobs live on disk or object store.** Trajectory bodies, snapshot artifacts, command-log
   chunks, and image layers never go into SQLite/Redis. The state
   store carries locators only. Exception: the spec 18 hot-tier
   command log lives in StateStore by design and is bounded by
   `hot_log_max_*` (see spec 18 size compaction).
9. **`task_key` is fairness; `instance_id` is identity.**
   `task_key` (spec 02) is opaque to the platform — used only
   for anti-affinity and `max_runs_per_task`. `instance_id`
   (spec 06 resolver output) is the cache key the image manager,
   asset manager, and replay machinery key off of. Code that
   conflates them breaks custom consumers that hash prompts into
   `task_key`.
10. **Reward mode is declared in the manifest and validated at template register.** A template that pairs an
    `env_step`-only EnvAdapter with `mode: in_sandbox_final` (or
    vice versa) is rejected at registration, never at runtime.
    Spec 02 RewardContract + spec 14 `supported_reward_modes`.

## Authoritative phase matrix

This is the **single source of truth** for which capability lands
in which phase. Per-spec phase ladders only add local detail; if
they disagree with this matrix, the matrix wins.

| Layer / capability | Phase 0 | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|---|
| **Sandbox backends** (spec 01) | Docker, local-process-debug | CubeSandbox; chainable overlaybd snapshots; lazy-load (eStargz / overlaybd); Function-Call mode | Firecracker direct; k8s-pod | — |
| **Rollout API** (spec 02) | gym/step lifecycle, deadlines, statuses, group primitives, idempotent start, heartbeat + idle TTL, reward modes `env_step` / `in_sandbox_final` / `consumer_final`, replay subject to sink durability | external_final + token_level reward modes; soft-deadline standardized in obs | snapshot/branch first-class; multi-agent rollouts | sessions (start_session / attach / end_session) |
| **Consumer SDK** (spec 05) | `Client`, `rollout`, `batch_rollout`, `replay`, error model | `client.warmup`, `client.invoke`; streaming-trajectory variant; rate-limit awareness | `restore_from=SnapshotID`; multi-agent helpers; client-side trajectory cache | `start_session` / `attach` (defers to spec 18) |
| **Control plane** (spec 03, 21) | single process, sqlite StateStore, fits-and-largest-remaining scheduler; admission queue; reverse stream w/ stream_epoch | redis StateStore; active + warm-standby; warm-pool aware; image-affinity term; fleet reservation for multi-container tasks (opt-in); mTLS in spec 21 | active-active sharding; preemption; priority classes; fleet idle-lending; autoscale on queue depth; SNI proxy egress | session reattach via session_token |
| **Node agent** (spec 04, 21) | Docker driver, basic GC, in-memory ring buffer, outbound-only stream | Cube driver, warm pool, 24h disk ring buffer, signed instance_id | GPU sandboxes (MIG/MPS), pod driver | snapshot stream commands |
| **State / storage** (spec 20) | sqlite StateStore, node-local run dirs, default paths, no object store required | redis StateStore, pool-accounting persistence, signed audit archive | object-store mirror for trajectories + audit; multi-tenant filters consume owner_id/project_id | cold-tier session logs + snapshot chains in object storage |
| **Templates** (spec 06) | terminal-base, swebench-base, osworld-base; instance resolvers; assets block | web-search-base; allowlist plumbing; warm pools; browser service; consolidation plans (spec 16) | deep-research-base; persistent disks | — |
| **Networking** (spec 07) | `none` / `open` policies; per-sandbox isolation; metadata block; default port-forward = control-plane proxy | `egress-allowlist` (DNS + iptables); mTLS; cube networking via eBPF; per-sandbox `network.log` audit | shared subnets for multi-agent; SNI proxy egress allowlist | — |
| **Observability** (spec 08) | `/metrics`, structured stdout logs, per-rollout run dir, debug CLI; durability matrix; phase-0 jsonl mandate for CI | OTel spans; rolling-file logs; admin pulls 24h history from in-process tsdb | trajectories in object store; external Prom/Grafana/Tempo integration | session snapshot / hot-tier metrics |
| **Capacity** (spec 10) | static + online refinement; multi-pool disk; mixed-template packing | Cube overhead constants tuned with measurement | predictive packing; GPU sandboxes; per-cloud network awareness | — |
| **Image cache** (spec 15) | per-node cache w/ LRU, operator pin list, `xrlenv warmup` CLI, `xrlenv images` reporting | `client.warmup` SDK + ImageDirective fan-out; image-affinity scheduling; admin Blobs view; lazy-load three-mode | predictive prefetch; cluster-wide layer dedup; image-aware autoscale | — |
| **Adapters** (spec 11, 12) | nothing consumer-specific; phase-0 SDK shaped for Slime/verl | Slime + verl adapters; SWE-bench smoke as the integration gate | snapshot-aware rollouts; multi-agent; longtail metrics; sglang path for verl | session token derivation already implemented in adapters |
| **Admin UI** (spec 13) | 7 read-only views, SSE updates, 60-min ring buffer; localhost-only by default | basic auth on every bind; 24h history; actions; `/images`, `/forwards`, `/audit` views | OIDC; full audit; multi-tenant filters; richer actions | `/sessions` view |
| **Trajectory viewer** (spec 17) | locator + StateStore columns, `FetchTrajectoryCommand`, platform-jsonl reader, on-disk cache, basic step/action/observation rendering | sink-aware readers (slime-sample, verl-dataproto), token-level overlay, image rendering, lazy-binary cache, comparison view, archival to object store | full object-storage backing; cross-cluster federation; programmatic API | command-log "raw commands" tab |
| **Sessions** (spec 18) | design hooks only (no rewrite later) | design hooks preserved | design hooks preserved | full sessions, command log, cached replay, snapshot cadence, derive_from token |
| **Security** (spec 19) | shared bearer tokens, three scopes, admin-bind guard, mount allowlist, metadata block, image digest pin, asset SHA-256, audit events for token use / mounts / admin / public binds | mTLS; per-consumer tokens with rotation/revocation; signed manifests; userns-remap default; trusted publisher list; per-sandbox network audit | OIDC, RBAC, multi-tenant project scoping, signed audit log, per-template policy bundles, egress proxy with logs | — |
| **Deployment / GC** (spec 09) | shell-script bootstrap (GCP+AWS), static `nodes.yaml`, Docker-only, GC layers 1–5 | cube install in bootstrap; per-node mTLS certs; `xrlenv bootstrap` subcommand; tighter rotation | NodeProviders for MIG/ASG/k8s; autoscale on queue depth | — |
| **Phase example workloads** (descriptive) | terminal-bench, SWE-bench, OSWorld | search agents, web-browsing | hours-long deep research | spot-VM long-horizon rollouts |

Per-spec phase ladders are local detail; when in doubt, this
matrix is the answer.

## Glossary

- **Template** — a declarative description of an environment kind
  (`terminal-base`, `swebench-base`, …). Names a base image, required backend
  capabilities, resource profile, network policy, init script. See spec 06.
- **Sandbox** — one running instance of a template (a Docker container or
  Cube microVM) with an in-sandbox stub server. See spec 01.
- **Step** — one `(action → observation, reward, done, info)` exchange between
  a policy and a sandbox.
- **Episode / Rollout** — a sequence of steps from sandbox creation to
  termination. The consumer-facing unit. See spec 02.
- **Trajectory** — the recorded sequence of steps from a finished rollout,
  plus metadata (status, reward, timestamps).
- **Node** — a host running an `xrlenv-node` daemon (laptop, GCP VM, AWS EC2).
- **Backend** — a sandbox runtime adapter (`docker`, `cubesandbox`).
- **Capacity profile** — per-(node, template) max concurrency, computed by
  the capacity estimator. See spec 10.
- **Trainer adapter** — a thin shim that exposes XRLEnv to a specific RL
  framework (`adapters/slime.py`, `adapters/verl.py`). Lives outside the
  sandbox.
- **EnvAdapter** — a thin shim *inside* the sandbox that wraps a
  benchmark's Environment class (e.g. OSWorld's `DesktopEnv`,
  terminal-bench's `Terminal`) or a user's custom Environment class,
  exposing a uniform `setup` / `step` / `teardown` protocol. See
  spec 14.
- **Fleet** — a set of containers that form one logical multi-container
  task, admitted together against a declared peak footprint (`fleet_id` +
  `fleet_footprint`). A *third* identity axis beyond invariant 9:
  `fleet_id` is **reservation identity**, distinct from `task_key`
  (fairness) and `instance_id` (identity). **Opt-in** — with no fleet
  declaration, admission is per-container exactly as today. See spec 03
  (reservation + queue), spec 10 (accounting), spec 21 (wire).

## Cross-references

- Sandbox runtime details → [01-sandbox-backend.md](01-sandbox-backend.md)
- RL-shaped API → [02-rollout-api.md](02-rollout-api.md)
- Orchestrator internals → [03-control-plane.md](03-control-plane.md)
- Per-host daemon → [04-node-agent.md](04-node-agent.md)
- Consumer-facing SDK → [05-consumer-sdk.md](05-consumer-sdk.md)
- Templates per phase → [06-templates-and-environments.md](06-templates-and-environments.md)
- Sandbox networking → [07-networking.md](07-networking.md)
- Metrics/logs/trajectories → [08-observability.md](08-observability.md)
- Deploy + GC → [09-deployment-and-gc.md](09-deployment-and-gc.md)
- Capacity estimation → [10-capacity-estimator.md](10-capacity-estimator.md)
- Slime adapter → [11-slime-integration.md](11-slime-integration.md)
- verl adapter → [12-verl-integration.md](12-verl-integration.md)
- Admin panel → [13-admin-panel.md](13-admin-panel.md)
- EnvAdapter layer (wrapping benchmark Environment classes) → [14-env-adapters.md](14-env-adapters.md)
- Image cache management (smart per-node image swap, prefetch, image-affinity scheduling) → [15-image-cache-management.md](15-image-cache-management.md)
- Benchmark analysis & image consolidation (`xrlenv analyze`, plan.yaml, equivalence smoke) → [16-benchmark-analysis.md](16-benchmark-analysis.md)
- Trajectory viewer (cross-node locator + sink-aware fetch + per-rollout inspection UI) → [17-trajectory-viewer.md](17-trajectory-viewer.md)
- Sandbox sessions and preemption-safe resume (long-lived sessions, off-node command log, step-level cached replay with snapshot-cadence correctness contract) → [18-sandbox-sessions-and-resume.md](18-sandbox-sessions-and-resume.md)
- Security model (threat model, identities, auth scopes, sandbox hardening, egress, supply chain, audit) → [19-security-model.md](19-security-model.md)
- State and storage (StateStore schema, run-dir layout, retention matrix, path defaults, object-store integration) → [20-state-and-storage.md](20-state-and-storage.md)
- Node control protocol (outbound bidi stream, message envelope, idempotency, flow control, reconnect/replay) → [21-node-control-protocol.md](21-node-control-protocol.md)
