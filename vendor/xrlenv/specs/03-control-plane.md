# 03 — Control Plane

## Purpose

The single orchestrator process that:

- Accepts rollout requests from trainers (via the SDK / gRPC API).
- Schedules sandboxes onto nodes using the capacity estimator.
- Holds the authoritative registry of nodes, sandboxes, and rollouts.
- Hosts the read-only admin panel.

Phase 0 is a single Python process per cluster. Phase 1 introduces
external state so the process can be restarted without losing live
rollouts.

## Components

```
control plane (one process, one host)
├── grpc_server          # public API for trainers and CLI
├── coordinator          # rollout lifecycle, deadlines, reward dispatch
├── scheduler            # picks (node, backend) for each new sandbox
├── node_registry        # heartbeats, capacity, hw probe results
├── capacity_estimator   # see spec 10
├── template_catalog     # template.yaml index, image refs
├── state_store          # sqlite v0 / redis v1
└── admin_server         # FastAPI, see spec 13
```

## Design choices: why these pieces?

This section explains the four labels in the box diagram above whose
shapes are not obvious from the name alone. The detailed designs of
the *other* components live in their own sections below (and
cross-referenced specs); this section is the rationale layer.

### "one process, one host" (the architectural choice)

In phase 0 the control plane is literally a single Python program
(`xrlenv-control`) running on one VM. Not a distributed system, not
multiple replicas, not sharded across nodes. All the components in
the box diagram are objects in the same Python process, holding
direct references to each other.

**Why this in phase 0**: it is the simplest thing that works. One
process means no leader election, no consensus protocol, no
split-brain. When a trainer says "start a rollout" exactly one
process decides "place it on node aws-1" and writes that fact down —
no race conditions across replicas to reason about.

**Trade-off**, which is why phases 1/2 are different:

| Phase | Shape | Failure mode |
|---|---|---|
| 0 | One process, one host | If it crashes, in-flight rollout streams fail with `RolloutFailed("control_plane_lost")`; sandboxes on node agents are quarantined and destroyed once the control plane returns and reconciles. Trainers retry from scratch (start idempotency via `request_id` deduplicates retries that race the restart, but does **not** reattach to a sandbox already past its initial step). No new rollouts accepted until restart. |
| 1 | One active + one warm standby, **shared state in redis** | Standby takes over; rollouts continue. |
| 2 | Active-active (sharded by template or node) | Truly fault-tolerant. |
| 3 | (Sessions, spec 18) | Mid-rollout reattach for session-bearing rollouts via `client.attach(session_token)` after standby takeover or restart. |

"One process, one host" is naming a deliberate phase-0 simplification
— not the architecture forever.

```
                  control-plane VM (one of the reserved cloud VMs)
                  ┌──────────────────────────────────────────────┐
                  │  $ xrlenv-control                            │
                  │  ├─ grpc_server         (port 50051)         │
                  │  ├─ admin_server        (port 8080)          │
                  │  ├─ coordinator                              │
                  │  ├─ scheduler                                │
                  │  ├─ node_registry                            │
                  │  ├─ capacity_estimator                       │
                  │  ├─ template_catalog                         │
                  │  └─ state_store         (~/.xrlenv/state.db) │
                  │                                              │
                  │  one Python process,                         │
                  │  asyncio event loop,                         │
                  │  internal components share memory directly   │
                  └──────────────────────────────────────────────┘
```

### `grpc_server`

**What it is**: a server that listens on a network port and accepts
function calls from other programs.

gRPC ("Google RPC") is a protocol for one program to call functions
that live in another program, possibly on another machine. Function
signatures are defined in a `.proto` file (the schema in the next
section); both sides generate code from it; calls feel like local
function calls but travel over the network.

**Why we need one in the control plane**: the control plane has no UI
of its own. Everything that wants to *make* the control plane do
something — the trainer, the CLI (`xrlenv nodes`, `xrlenv up`, …),
the node agents reporting heartbeats — does it by calling a function
on the gRPC server. It is the **front door** of the control plane.

**Why gRPC specifically vs alternatives**:

| Alternative | Why we don't use it |
|---|---|
| Raw HTTP / REST JSON | No bidi-streaming. The `Rollout` call needs the trainer to send actions while the server streams observations on the same connection — gRPC does this natively. |
| Direct Python imports | Doesn't work across machines. The trainer is on a GPU host; the control plane is on a separate VM. |
| WebSockets | Lower-level than we need; would have to invent the framing, error codes, retries, and code generation that gRPC gives for free. |

**Concrete shape**: `xrlenv/api/orchestrator.proto` defines the
methods (`Rollout`, `ListNodes`, …); a generated `OrchestratorServicer`
class is what `grpc_server` hosts. The trainer SDK's `Client` is a
thin wrapper around the generated stub, so trainer code that does
`async with client.rollout(...)` is calling the gRPC server here.

### `state_store`

**What it is**: a place to record persistent facts about the cluster
— which sandboxes exist, which nodes are alive, which rollouts ran,
what events happened. Survives the control plane being restarted.

**Why we need it** — three reasons:

1. **Crash recovery**. If `xrlenv-control` is killed (oom, deploy,
   kernel panic) and restarts, RAM is gone. But there are still real
   sandboxes running on real cloud VMs, and there are trainer
   processes whose `Client` connections will reconnect — note that
   in phase 0 a *new client connection* is what reconnects, **not**
   a mid-rollout stream (the previous rollout streams have already
   been terminalized as `failed` by the failure handler; trainers
   retry under a fresh `request_id` or read the cached terminal
   status). The control plane must reconstruct "what was the
   world's state before I died" from disk — that is what the
   state store is for.
2. **Audit / debug**. An operator at 3 PM asks "why did rollout X
   fail at 4 AM?" The events log in the state store has the answer
   (scheduler decision, sandbox creation, error message); RAM has
   long since dropped that information.
3. **Cross-process coordination** (phase 1+). When a warm-standby
   control plane is added, both processes need to look at the same
   source of truth — that is the shared state store (redis).

**Phase 0 implementation: SQLite**. A single file
(`~/.xrlenv/state.db`) the Python process reads/writes via the
`sqlite3` library. Zero deployment cost — no extra service to run;
just a file on disk. Sub-millisecond writes; sub-microsecond reads.
Sufficient for one control-plane process.

**Phase 1 implementation: Redis**. A separate key-value store running
as its own process (one VM, or shared with the control-plane VM).
Network-accessible so multiple control-plane processes can share it.
Has primitives we use (sorted sets for the events log, hashes for
entities, pub/sub for change notifications).

**What's stored vs not**:
- *Stored*: small structured records — `sandboxes` table (id, node,
  template, status, started_at), `rollouts` table (id, template,
  status, reward, …), `events` (append-only log).
- *Not stored*: trajectory bodies — those live in the per-rollout
  run directory (canonical layout in spec 08 / spec 20:
  `~/.xrlenv/runs/<date>/<rollout_id>/`). The exact body file
  depends on the active sink: `trajectory.jsonl` is present only
  when `platform-jsonl` is in the sink chain; for `slime-sample`
  / `verl-dataproto` the body lives in the trainer's native
  buffer (the locator's `uri` points off-node). `meta.json` plus
  the `TrajectoryLocator` it carries is the always-present
  pointer that turns the row into something readable. Putting
  megabytes per rollout into sqlite would be wasteful regardless.
  **Rule of thumb: state store = metadata + locator; disk /
  object storage / native trainer buffer = body.**

Concrete recovery example: on restart, the coordinator queries the
state store for `sandboxes WHERE status='running'`. For each row, it
asks the corresponding node agent "is this sandbox still alive?" and
reconciles. The whole recovery dance only works because the state
store remembered the sandboxes existed.

### `admin_server`

**What it is**: an HTTP server that serves the browser-based admin
dashboard (the `/`, `/nodes`, `/sandboxes`, `/rollouts`, `/capacity`,
`/health`, `/images` pages from spec 13).

It's a separate listener from `grpc_server` because they serve
different audiences:

| Server | Listens on | Talks to | Why distinct |
|---|---|---|---|
| `grpc_server` | port 50051 | machines (trainers, CLI, node agents) | typed binary protocol, bidi-stream |
| `admin_server` | port 8080 | humans via browsers | HTTP+HTML+SSE; what browsers natively speak |

**Why bundled inside the same control-plane process** (instead of a
separate service):

1. It reads exactly the data the control plane already has. Node
   registry, sandbox table, rollout history, capacity matrix — those
   are in-memory Python objects in the same process. The
   `admin_server` reads them directly. No extra hop, no extra
   network round-trip.
2. No extra deploy unit. The user has VM-only access (no admin to
   set up Grafana / Prometheus); bundling means one wheel, one
   process, dashboard included.
3. Live updates are cheap. The `admin_server`'s SSE stream pushes
   events from the in-process event bus the coordinator already
   publishes to. A separate-process dashboard would have to poll
   gRPC, which is wasteful and laggy.

**Concrete shape**: `xrlenv/admin/server.py` is a FastAPI app. Boot
roughly looks like:

```python
async def main():
    state   = await StateStore.open(...)
    catalog = TemplateCatalog.load(...)
    nodes   = NodeRegistry()
    cap     = CapacityEstimator(...)
    coord   = RolloutCoordinator(state, nodes, ...)

    grpc  = GrpcServer(coord, nodes, catalog, cap, state)    # port 50051
    admin = AdminServer(coord, nodes, catalog, cap, state)   # port 8080

    await asyncio.gather(grpc.serve(), admin.serve())
```

Both servers hold references to the *same* in-memory components.
They are "two ways into the same brain" — gRPC for machines, HTTP
for humans.

## gRPC API

```proto
service Orchestrator {
  // Bidi-stream: trainer sends StepRequests, server returns StepResults,
  // closes with TrajectorySealed at end-of-rollout.
  // The opening RolloutClientMsg carries:
  //   request_id   : str   (idempotency key; same id -> same rollout)
  //   task_key     : str?  (prompt / problem identifier; for affinity / cap)
  //   group_id     : str?  (binds rollouts that share a training-iteration
  //                         group; used for anti-affinity and CancelGroup;
  //                         XRLEnv treats both as opaque tags)
  //   deadline     : Deadline (soft / hard / per-phase overrides)
  //   idle_ttl_s   : float  (default 120)
  rpc Rollout(stream RolloutClientMsg) returns (stream RolloutServerMsg);

  // Trainer-side heartbeat keeping a rollout alive without sending a step.
  rpc Heartbeat(HeartbeatRequest) returns (Empty);

  // Cancellation primitives. The trainer (or its adapter) decides when
  // to cancel; XRLEnv does not auto-cancel based on group counts or
  // filter outcomes — those are engine-specific policy.
  rpc CancelRollout(CancelRolloutRequest) returns (Empty);
  rpc CancelGroup(CancelGroupRequest) returns (CancelGroupReport);

  rpc ListNodes(Empty) returns (NodeList);
  rpc ListSandboxes(SandboxFilter) returns (SandboxList);
  rpc ListRollouts(RolloutFilter) returns (RolloutList);

  rpc RegisterTemplate(TemplateManifest) returns (Empty);
  rpc Capacity(Empty) returns (CapacityMatrix);

  rpc Replay(ReplayRequest) returns (stream Step);
  rpc Healthz(Empty) returns (Health);
}
```

The CLI (`xrlenv ...`) is a thin client over this API.

### Idempotency cache

The coordinator maintains a `(request_id → rollout_id)` LRU with TTL
`idempotency_ttl_s` (default 300 s); the table is persisted in the
StateStore so it survives control-plane restart. A `Rollout` open
whose `request_id` matches a live entry **does not** transparently
reattach to a running rollout. Behavior is keyed by the matched
rollout's lifecycle state (spec 02 transient states):

| Matched rollout state | Behavior on retry |
|---|---|
| `queued` (in admission queue) | the SDK opens a fresh stream that consumes the same queued rollout when it is admitted; no second sandbox created |
| `starting` (sandbox being built) | reject with `RolloutInProgress`; caller backs off and retries; idempotency key remains valid |
| `running` *and* the original stream is still alive | reject with `RolloutInProgress` (the original holder is the stream owner; we never let two streams drive one rollout) |
| `running` *and* the original stream is gone (phase-0 control-plane crash, network partition) | the rollout has already been terminalized as `failed` / `reason="control_plane_lost"` by the failure handler; the cache returns the **terminal** trajectory, not a live stream — caller treats it as a finished failure and decides whether to retry under a new `request_id` |
| terminal (`finished` / `truncated` / `cancelled` / `failed`) | return the sealed trajectory directly |

Phase-3 sessions (spec 18) are the only path that supports
mid-rollout reattach; they use `session_token`, not `request_id`.

### Idle reaper

A coordinator background task runs every 10 s. For each rollout, if
`now - last_touch_ts > idle_ttl_s`, the rollout is hard-cancelled and
its sandbox destroyed. `last_touch_ts` is bumped on every step,
heartbeat, or stream message. Distinct from the hard TTL (spec 02).

## Scheduler

Algorithm, phase 0:

1. For each candidate node, ask
   `capacity_estimator.fits(node, currently_running, template)`.
2. Filter to nodes whose available backends ⊇ template's required backends.
3. Among feasible nodes, pick the one with the largest *remaining* capacity
   for this template (maximizes packing tightness while leaving headroom for
   the same template again).
4. Tie-break by lowest current sandbox count.

**Placement** is stateless: on each placement attempt the scheduler
queries fresh node-registry / capacity state and decides
independently — no in-memory placement queue. **Admission** state
(`pending_rollouts`) *is* persisted in the StateStore so a
control-plane restart preserves queued work; the scheduler reads
the queue and re-runs placement against current capacity. The two
should not be confused: placement = stateless decision; admission =
persisted queue. Decisions are logged to the events table for
debugging.

Group / task-aware constraints (phase 0; algorithm-agnostic):
- **Per-node per-task cap**: the scheduler refuses placement when
  doing so would push `(node, task_key)` count above the node's
  `max_runs_per_task` (default 4). Algorithm-driven fairness; not
  tied to any specific over-request scheme.
- **Group anti-affinity**: when placing a member of a group with
  existing members, prefer nodes that don't yet host a sibling.
  Hint, not a hard constraint — a saturated cluster will pack
  siblings on the same node when capacity demands it.

Both are *primitives* — the platform applies them whenever
`task_key` / `group_id` are set on the request. The trainer decides
when to set them and when to call `CancelGroup`; XRLEnv does not
auto-cancel based on group counts or filter outcomes.

### Admission queue and backpressure

When all candidate nodes return `fits=False`, the scheduler does
not immediately fail the request — `batch_rollout` calls in
particular submit thousands of starts at once and would see
spurious failures during normal capacity peaks. Pending requests
land in an admission queue:

```python
@dataclass
class PendingRollout:
    rollout_id:   str
    request_id:   str
    template:     str
    init:         dict
    task_key:     str | None
    group_id:     str | None
    deadline:     Deadline
    submitted_ts: float
    owner:        str       # trainer identity (multi-tenant phase 2)
```

Queue rules:

- **Per-trainer cap**: `queue_max_per_trainer` (default 4096).
  Submissions past the cap raise `CapacityExhausted("queue_full")`
  to the SDK so the caller can backoff.
- **Per-template cap**: `queue_max_per_template` (default 2048),
  to stop one runaway template from starving others.
- **Global cap**: `queue_max_global` (default 16384), a hard upper
  bound on what the control plane will hold in memory + state
  store.
- **Ordering**: submission FIFO within a `(template, owner)`
  partition; round-robin across partitions when nodes free up.
  Group anti-affinity and `task_key` fairness still apply at
  placement time, not at queue time.
- **Cancellation while queued**: `cancel_rollout(rollout_id)` and
  `cancel_group(group_id)` operate on queued rollouts identically
  to running ones — the rollout is removed from the queue and a
  trajectory sealed with `status="cancelled"`,
  `reason="consumer_cancelled" | "group_cancelled"`. No sandbox is
  ever created; capacity is not consumed.
- **Queue timeout**: `Deadline.queue_timeout_s` (default 300).
  Rollouts that exceed it without admission seal as `failed` /
  `reason="queue_timeout"`.
- **Idempotency**: queued rollouts are visible to the
  `(request_id → rollout_id)` cache (spec 02). A retry with the
  same `request_id` returns the existing queued rollout, never
  enqueues a duplicate.
- **Persistence**: the queue lives in the StateStore so a
  control-plane restart does not lose pending work. On restart,
  queued rows are re-validated against current capacity and either
  re-admitted, re-queued, or sealed `failed` /
  `reason="control_plane_lost"` per the phase-0 crash semantics
  in this spec.

Image cache (spec 15) and warmup directives see queued rollouts
through the same `(node, template, instance_id)` projection used
for placement — Layer 1 prewarm therefore covers queued work.

### Scheduler refinements (phase 1)

Phase 1 adds:
- Warm-pool awareness (prefer node with a matching warm sandbox).
- **Image-affinity term**: prefer nodes that already have the
  template's image (and per-instance image, when applicable) cached.
  See spec 15. Capacity always wins over affinity when both nodes
  have the image; affinity wins when capacity is comparable. This
  avoids forcing a fresh image pull on a node when another node has
  it cached and free capacity.
- Multi-region affinity (template-data co-location hints).

Phase 2 adds:
- Priority classes, preemption, autoscale request via `NodeProvider`.

### Fleet reservation — multi-container tasks (opt-in, phase 1)

**The problem.** Some consumers run one logical task as a *fleet* of
containers acquired at different times, not a single container — typically a
**lead** container with a small footprint, held for the task's whole life,
plus one or more larger **companion** containers acquired later. Under the
default per-container admission the small lead containers admit greedily —
all N fit — and the cluster then has no room for their larger companions,
which pile up in the admission queue. Throughput *collapses* as concurrency
rises: the leads hold capacity their own companions need. This is not an
accounting error (a 2-vCPU lead + a 16-vCPU companion = an 18-vCPU footprint
is correct); it is an **ordering / reservation** gap — the platform admits a
task's first container without guaranteeing the rest can run.

This mechanism is **generic**. The core knows nothing about *why* a fleet
has the shape it does — it parses a generic fleet declaration (below) and
reserves; it never names or assumes container roles ("lead"/"companion"
above are just exposition). Deciding *when* to declare a fleet and *what*
footprint to request is entirely consumer-side (a benchmark harness's shim,
a trainer adapter). Fleets are a raw-container primitive; no consumer is a
platform special case.

**The fix (opt-in, additive).** A consumer that will acquire a fleet
declares it up front so admission reserves the whole task footprint
atomically. With **no fleet declaration the scheduler admits each
container independently, exactly as today** — the faithful default;
tb2.1 / SWE-bench and every single-container consumer are unchanged. The
platform never *infers* that a task is a fleet.

**Contract.** On the **first** acquire for a fleet the consumer sets:
- `fleet_id` — an opaque id shared by every container in the fleet
  (spec 21 acquire field). A *third* identity axis, distinct from
  `task_key` (fairness, invariant 9) and `instance_id` (resolver
  identity): `fleet_id` is **reservation identity**.
- `fleet_footprint` — the fleet's **peak** hold, a
  `{cpu_request, mem_request}` budget (peak, not an exact container list
  — simpler, and mis-declaration fails loud, below).

Admission then:
1. Gates on the **whole `fleet_footprint`** fitting on **one** node
   (single-node fleet, MVP — keeps a fleet's containers co-located so any
   container-to-container data movement stays node-local rather than
   bouncing through the control plane). If it doesn't fit, the *fleet*
   queues — same admission queue / backpressure / timeout as a rollout,
   at fleet granularity. 100k fleets → admit what fits, queue the rest at
   zero cost.
2. **Reserves** `fleet_footprint` on the chosen node and pins the
   `fleet_id` there (spec 10 accounting).
3. Places every later acquire carrying the same `fleet_id` on the reserved
   node and **charges it against the reservation, not the node's free
   pool** — the node's free capacity drops by the footprint once, not by
   each container in turn.

**Reservation is accounting, not enforcement.** `fleet_footprint` governs
*admission* only. Each container keeps its own cgroup quota (e.g. a
`--cpus 2` lead alongside a `--cpus 16` companion); an 18-vCPU reservation
caps no single container. Reservation (scheduler) and cgroup limit (node
execution) are separate axes — do not conflate them.

**Over-budget fails loud.** A companion acquire whose running-plus-new
draw would exceed `fleet_footprint` is rejected with `FleetOverBudget`
(spec 21) — the consumer under-declared. The scheduler never silently
grows a reservation; the consumer declares a larger footprint next time.
Over-declaration is safe: it wastes reserved capacity but stays correct.

**Lifecycle.** The reservation releases when the fleet's **final**
container is node-confirmed destroyed (invariant 2 — "destroy enqueued" is
not "released"). Leak protection: a `fleet_id` with no live containers and
no acquire within `fleet_reservation_ttl_s` (default 600) is reclaimed by
the raw-GC reconciler (spec 09), so a crashed consumer can't strand a
reservation.

**Fairness.** A fleet counts as **one** scheduling unit for `task_key`
fair-share and `max_runs_per_task`, not N containers — otherwise a
fleet-heavy tenant would look like many independent tasks. `fleet_id`
carries no fairness meaning; `task_key` stays fairness metadata and may be
set independently.

**Hard reservation, no lending (v1).** The reserved-but-idle capacity — the
headroom an admitted fleet holds before its larger companions arrive — is
**not** lent to other work in v1. Lending it would require
preemption, priority classes, and reclaim semantics (phase 2, alongside
the preemption line above); v1 stays dead simple and predictable. The cost
is real (parked capacity on a promise) and the tradeoff is deliberate: ~N
fully-runnable fleets beats k·N thrashing half-fleets.

**Node authority (invariant 6) — MVP limitation.** A fleet is pinned to
one node for its life; it cannot be relocated once its lead container is
placed. If
the reserved node later rejects a companion with `OverCapacity`
(reservation drift under measurement error), the CP fails the fleet rather
than moving it. Softening this to a whole-fleet re-placement is a phase-2
follow-on.

Capacity accounting for the reservation is spec 10; the wire fields and
`FleetOverBudget` are spec 21.

## NodeRegistry

- Each node agent opens a gRPC stream to the control plane on startup
  (outbound only — see spec 04).
- Heartbeats every 5 s carry: current resource usage, list of running
  sandbox IDs, hw probe deltas (rare).
- Node marked dead after 15 s of missed heartbeats. Its sandboxes are
  marked `status="node_lost"` and their rollouts fail with
  `RolloutFailed("node_lost")`.
- Node reconnect: registry reattaches; sandboxes still alive on the node
  agent (which holds local state across control-plane restarts) get
  reconciled.
- The registry mirrors register/deregister/heartbeat into the `nodes`
  table (spec 20) as a persistent shadow so out-of-process readers (the
  `xrlenv nodes` CLI, admin `/nodes`) see the fleet without gRPC. Rows are
  normally never deleted — attachment history surfaces a flapping node —
  with **one exception: startup reconciliation against the roster.** On
  every `xrlenv up`, after loading `nodes.yaml`, the control plane prunes
  `lost` rows whose node_id is absent from the roster: a decommissioned
  host, or (commonly) an IP-derived node_id orphaned by a cluster reboot.
  `connected` rows and rostered node_ids are always kept, and an
  empty/failed-to-load roster prunes nothing (it must never nuke the whole
  registry). Because `nodes.yaml` is generated from `clusters.yaml` (the
  deploy source of truth) and a redeploy always bounces `xrlenv up`, this
  keeps the registry from accumulating dead rows reboot-over-reboot.
  Rollout history in `raw_rollouts` is left intact (separate GC).

## TemplateCatalog

- Loads `xrlenv/templates/<name>/template.yaml` at startup.
- Operators can add a template at runtime: `xrlenv template register
  ./path/to/template.yaml`.
- Catalog enforces backend-capability validation (a template that
  declares `snapshot_required: true` won't accept a Docker-only node).

See spec 06 for the manifest schema.

## Multi-tenant data-model hooks (phase 0; activated phase 2)

Phase 0 is single-tenant by assumption — one user, one operator,
one cluster. Phase 2 introduces multi-tenant isolation. Retro-
fitting tenant identity onto an existing schema would force
migrations on every table that already exists, plus rewrites of
admin filters and quotas.

To avoid that rewrite, the phase-0 schema **already carries** the
tenancy columns; they just default to a sentinel value. Spec 20
canonical schema lists them; this section names how they're
populated:

| Field | Source in phase 0 | Source in phase 2 |
|---|---|---|
| `owner_id` | `"default"` | trainer identity (auth scope) |
| `project_id` | `"default"` | optional client-supplied label, validated against the operator's project list |
| `run_id` | client-supplied (`request_id`'s prefix when present) or `"default"` | wandb run id / verl experiment id |

These flow through:

- gRPC `Rollout` open carries `owner_id` / `project_id` /
  `run_id` in the opening message; the SDK populates them from
  the `Client` constructor's `tenant=...` arg (default
  `"default"`).
- StateStore rows for `rollouts`, `sandboxes`, `pending_rollouts`,
  `sessions`, `events`, `audit` carry the three columns.
- Admin panel queries (spec 13) and CLI listings (`xrlenv
  rollouts`) accept `--owner` / `--project` / `--run` filters.
  In phase 0 only the `default` value exists; in phase 2 these
  become enforcement-grade scopes.
- `xrlenv_rollouts_*` and other per-rollout metrics carry
  `owner_id` and `project_id` as labels (cardinality is bounded
  by operator config).

Phase 0 implementation cost: the columns and the SDK arg. Phase 2
turns these into authz scopes; nothing in phase 0 needs to be
rewritten when that lands.

## StateStore

Thin abstraction with two implementations:

- **`SqliteStore`** (phase 0). Single-file DB at
  `~/.xrlenv/state.db`. Tables follow the canonical schema in
  spec 20: `nodes`, `sandboxes`, `rollouts`, `pending_rollouts`,
  `events`, `idempotency`, `audit`, `revocations` (plus the
  phase-3 `sessions` and `command_log_hot` tables when sessions
  ship). WAL mode by default; `TRUNCATE` on a network filesystem to avoid the
  WAL `-shm` mmap SIGBUS (see spec 20). Good up to a few thousand rollouts/hour.
  The `rollouts` row carries the `TrajectoryLocator` populated at
  seal (spec 20 schema) — used by the Trajectory Viewer (spec 17)
  to find the right node and reader for any sealed rollout.
- **`RedisStore`** (phase 1). Same logical schema; sorted-sets for
  events, hashes for entities. Multi-process control plane safe.

Trajectory bodies are not in the state store — they're written
into per-rollout run directories on the node that ran the
rollout (canonical layout in spec 20). When the active sink is
`platform-jsonl` the body is `trajectory.jsonl` in that
directory; for `slime-sample` / `verl-dataproto` the body lives
in the trainer's native buffer and the directory still carries
`meta.json` plus the locator. Phase 2 mirrors the run directory
itself (and any `trajectory.jsonl` inside it) to GCS/S3; native
sink archival is sink-specific and does not flow through the
same mirror. The `rollouts` row stores only the
`TrajectoryLocator` populated at seal.

## Failure handling

- **Sandbox hard TTL** (default 1 h, override per template). Past TTL
  the coordinator forces destroy.
- **Hung rollout**: trainer disconnects → coordinator cancels the
  rollout, destroys the sandbox.
- **Control-plane crash** (phase 0): state store is the recovery
  source for *metadata*, not in-flight rollouts. On restart:
  rebuild node registry; reconnect node agent streams; for each
  sandbox the state store knows about, query its node — sandboxes
  whose owning rollout stream is gone (always true in phase 0
  after a control-plane crash, because the bidi `Rollout` stream
  was anchored to the dead process) are destroyed during
  reconcile. Trainers see their `Rollout` streams fail with
  `RolloutFailed("control_plane_lost")` and retry; the
  `(request_id → rollout_id)` idempotency cache (also in the state
  store) deduplicates retries that race the restart by returning
  the *terminal* status of the prior rollout, never by reattaching
  to a still-running sandbox. Phase-3 sessions (spec 18) add
  proper mid-rollout reattach via `session_token`; phase 0 does
  not.
- **Node-agent crash**: node reconnects, GC runs, orphans destroyed.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: single-process, sqlite, scheduler is fits-and-largest-
  remaining. Control-plane restart **reconciles** node and sandbox
  state from the StateStore but does **not** resume in-flight
  rollouts — active phase-0 rollouts terminalize as `failed` /
  `reason="control_plane_lost"` and trainers retry from scratch
  under a new `request_id` (or the same one, which returns the
  cached terminal status). See "Failure handling" above.
- **Phase 1**: redis-backed; multi-process control plane (active +
  warm standby); warm-pool aware scheduling.
- **Phase 2**: NodeProvider integration (k8s, MIG, ASG); preemption;
  priority classes; autoscale on queue depth.
