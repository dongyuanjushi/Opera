# 10 — Capacity Estimator

## Purpose

Compute, per node, *how many concurrent sandboxes of each template
the node can support*, so heterogeneous VMs are utilized fully without
manual tuning. Hand-coded per-node concurrency caps either underuse
big machines or overload small ones; this module solves that problem.

The estimator feeds the scheduler (every placement decision asks
"does this fit?") and the admin panel (capacity matrix, under-
utilization alerts).

## Inputs

1. **Hardware probe** from each node (`HardwareSpec`, see spec 04):
   ```
   vcpus, mem_bytes, disk_bytes_free, net_mbps,
   has_kvm, has_gpu, gpu_model, kernel_version, page_size,
   cloud, instance_type
   ```
   Sent at registration and refreshed every 5 minutes.

2. **Template resource profile** from `template.yaml`:
   ```
   cpu_request, cpu_limit, mem_request, mem_limit,
   disk_request, net_class, gpu_required, snapshot_required
   ```
   Optional: `expected_step_io_mbps` for IO-bound templates.

3. **Backend overhead** — per-runtime constants the estimator knows:
   - Docker: ~50 MB RSS + ~0.05 vCPU per sandbox idle.
   - CubeSandbox: ~128 MB + ~0.1 vCPU per microVM idle.
   These are starting estimates; the online-refinement loop (phase 1)
   tunes them with measured data.

4. **Reserved headroom** (configurable, defaults):
   - 10% CPU
   - 15% memory
   - 5 GB disk minimum free
   for OS, node-agent, kernel buffers, image cache spikes.

## Phase 0 — static estimator (default) + online refinement

Per `(node, template)` pair, compute three independent caps and take
the minimum:

```
cpu_cap  = floor((vcpus * (1 - cpu_headroom) - reserved_overhead_cpu) /
                 (cpu_request + backend_cpu_per_sb))

mem_cap  = floor((mem_bytes * (1 - mem_headroom) - reserved_overhead_mem) /
                 (mem_request + backend_mem_per_sb))

disk_cap = floor( (disk_bytes_free - reserved_disk) / disk_request )

max_concurrent = min(cpu_cap, mem_cap, disk_cap)
```

Plus a hard zero if the node lacks a required capability:
- Template requires `cubesandbox` and node has no `cube` backend → 0.
- Template requires `gpu` and node has none → 0.

## Disk is multi-pool, not a single bucket

A node's disk is consumed by far more than running sandboxes'
writable scratch. The naive
`disk_free - sum(disk_request[t] for running)` formula admits work
the node cannot start and forces the image cache to evict at the
worst time. The estimator therefore tracks **disk pools** and the
per-template request claims a *specific* pool:

| Pool | Owner of the budget | Contents |
|---|---|---|
| `image_cache` | Image Cache Manager (spec 15) | Docker images, eStargz/overlaybd layers, lazy-loaded chunks |
| `asset_cache` | Image Cache Manager (assets extension, spec 15) | qcow2s, model checkpoints, dataset shards |
| `sandbox_writable` | node agent at `create` | per-sandbox writable layers + `{sandbox_scratch}` mounts |
| `session_state` | spec 18 sessions, when present | snapshot chains, hot-tier command logs |
| `run_artifacts` | spec 08 trajectory sinks | per-rollout run dirs (`~/.xrlenv/runs/...`) |
| `reserved` | operator config | OS, swap, headroom |

`HardwareSpec.disk_bytes_free` is the raw free bytes; the
estimator exposes a `NodeDiskAccounting` snapshot:

```python
@dataclass
class NodeDiskAccounting:
    total_bytes: int
    free_bytes:  int        # raw, what `df` reports
    pools: dict[str, PoolAccounting]   # one entry per pool name above

@dataclass
class PoolAccounting:
    budget_bytes:  int      # the cap
    in_use_bytes:  int      # what's currently consumed (refreshed each heartbeat)
    soft_floor:    int      # the cache manager / GC keep this much headroom
```

Defaults (configurable per node):

```yaml
disk_pools:
  reserved:         5GB
  run_artifacts:    20GB        # rotated by spec 09 layer 4
  sandbox_writable: 50% of remaining free
  session_state:    10% of remaining free        # phase 3
  image_cache:      35% of remaining free
  asset_cache:      5% of remaining free
```

The estimator's `disk_cap` is computed against `sandbox_writable`
specifically:

```
disk_cap = floor( (sandbox_writable.budget - sandbox_writable.in_use)
                 / disk_request )
```

The Image Cache Manager (spec 15) is given `image_cache` and
`asset_cache` budgets directly and runs eviction inside them —
running sandboxes are never starved by image churn. The two
pools share their accounting with the estimator so an admin-panel
operator can see "node has 200 GB free, but 0 of it is admittable
for new sandboxes because image cache is at budget." Spec 13's
admin panel surfaces `binding_constraint` values
`disk:sandbox_writable`, `disk:image_cache`, `disk:asset_cache`,
`disk:session_state` so the right knob is obvious.

Operator override (e.g. SWE-bench-heavy clusters that need more
image cache): `xrlenv node-disk-pools <node> --image-cache 50%
--sandbox-writable 35%` repartitions live; the cache manager
honors the new budget on the next eviction tick.

## Mixed-template packing

When scheduling onto a node that's already running templates A and B,
the estimator answers "does template C fit?" by computing remaining
capacity along each axis. CPU and memory are simple sums; disk is
read from the multi-pool accounting introduced above:

```
remaining_cpu  = vcpus - sum(cpu_request[t] for t in running) - cpu_headroom_overhead
remaining_mem  = mem   - sum(mem_request[t] for t in running) - mem_headroom_overhead

# Disk: the only pool a new sandbox can consume from is sandbox_writable.
sw = NodeDiskAccounting.pools["sandbox_writable"]
remaining_disk = sw.budget_bytes - sw.in_use_bytes
                 # image_cache, asset_cache, session_state, run_artifacts,
                 # and reserved are accounted in their own pools and never
                 # subtracted from sandbox_writable.
```

`disk_request` for template C is checked against `remaining_disk`
exclusively; image / asset / session footprints are out of the
scheduler's loop because their pools are managed by the cache
manager (spec 15) and the session lifecycle (spec 18) respectively.
The scheduler treats the node as a multi-dimensional knapsack
and picks the node where the new sandbox fits and remaining
capacity stays most balanced across cpu / mem / `sandbox_writable`.

## Fleet reservation accounting (multi-container tasks)

Spec 03 admits a *fleet* — a multi-container task declared with a
`fleet_id` + `fleet_footprint` — against its whole peak footprint at once,
reserved on a single node. The estimator carries that reservation as a
first-class term so `fits()` and remaining-capacity reflect it.

An open fleet on a node reserves `fleet_footprint` (peak `cpu_request` /
`mem_request`). Its member containers draw **from** that reservation for
**cpu and mem**; they are **not** summed on top of it. So remaining capacity
subtracts each open fleet's footprint once — not the footprint plus its
containers (disk is the one exception — see below):

```
remaining_cpu = vcpus
              - sum(cpu_request[c] for c in running non-fleet containers)
              - sum(fleet_footprint.cpu_request for f in open fleets on node)
              - cpu_headroom_overhead
# mem is identical. A fleet's member containers are covered by its footprint
# term above for cpu + mem and are NOT double-counted here.
```

`fits()` gains two fleet-aware answers:
- **Opening a fleet**: the whole `fleet_footprint` must fit on one node
  (single-node reservation, MVP — the footprint cannot be split across
  nodes).
- **A companion of an open fleet**: fits iff the fleet's member containers
  so far plus the new one stay within `fleet_footprint` — an accounting
  check against the reservation, not the node's free pool (the capacity
  was already reserved when the fleet opened). Over budget →
  `FleetOverBudget` (spec 03 / 21).

**Hard reservation (v1)**: a fleet's reserved-but-idle capacity is
unavailable to other placements even while the fleet holds only its small
lead container — no lending / preemption in v1 (spec 03).

**Disk (MVP decision, explicit)**: the fleet footprint is **cpu + mem only**.
Disk stays **per-container, including for fleet members** — a fleet member
contributes its own `disk_request` to the `sandbox_writable` pool exactly like
a non-fleet container. This is the **one documented exception** to "members
contribute no per-container load": cpu + mem are footprint-covered, disk is not.
There is **no** fleet disk footprint and **no** default-0 disk term — that would
make members' real disk invisible (a silent under-count), which we reject.
Rationale: the starvation problem the reservation solves is cpu (companion-slot
exhaustion), not disk, and per-task disk (build trees, `node_modules`) is hard
to declare up front. Folding disk into the footprint is a **phase-2** refinement
for consumers that can reliably declare it.

## API

```python
class CapacityEstimator(Protocol):
    def capacity(self, node_id: str, template_id: str) -> int:
        """Idle node: how many of this template fit?"""

    def fits(self, node_id: str,
             currently_running: dict[str, int],
             candidate_template: str,
             task_key: str | None = None,
             max_runs_per_task: int = 4) -> bool:
        """One more of candidate_template, given what's running, fits?

        Honors two independent caps:
          1. Per-template resource cap (the math above).
          2. Per-(node, task_key) cap from spec 02 (algorithm-driven
             group fairness for GRPO and similar). When `task_key` is
             None, the per-task cap is skipped.
        """

    def report_usage(self, node_id: str, sandbox_id: str,
                     template_id: str,
                     peak: ResourceUsage) -> None:
        """Online refinement (phase 1)."""

    def matrix(self) -> CapacityMatrix:
        """node × template → (max_concurrent, current, binding_constraint)
        for the admin panel and `xrlenv capacity` CLI."""
```

`binding_constraint` is one of `cpu`, `mem`,
`disk:sandbox_writable`, `disk:image_cache`, `disk:asset_cache`,
`disk:session_state`, `disk:run_artifacts`, `gpu`,
`backend_missing` — surfaced so an operator can see *why* a node
has low capacity for a template, and which disk pool to repartition
when disk binds. The plain `disk` label is retired in favor of the
pool-specific labels.

## Phase 0 — online refinement (in addition to static)

The node agent reports rolling p95 of actual `(cpu, mem, disk_io,
net_io)` per template per sandbox in heartbeats. The estimator
maintains EMA estimates and slowly updates the effective `*_request`
profile:

```
effective_request = max(declared_request, ema_p95_observed * safety_margin)
```

`safety_margin = 1.15` initially. New templates use the declared
profile; the system learns their real shape over the first ~50
finished rollouts. Updates are persisted in the state store so a
restart doesn't re-learn from zero.

Operator override: `xrlenv capacity override <template> --cpu 3.5
--mem 6GB` pins values until cleared.

## Phase 1 — Cube backend constants tuned with measurement

Once CubeSandbox lands in phase 1, its overhead constants
(`backend_cpu_per_sb`, `backend_mem_per_sb`) are tuned with measured
data the same way Docker's are tuned in phase 0. Until then the
declared Cube constants are conservative defaults.

## Phase 2 — predictive packing

For long-horizon rollouts (hours), the average resource profile
hides spiky behavior. Phase 2 tracks per-template *duration
distribution* and *peak vs sustained* usage; the estimator computes
expected resource-time integrals so the scheduler avoids packing two
memory-spiky long rollouts onto the same node even if their averages
fit.

## Operator surface

```
$ xrlenv capacity
                                terminal  swebench  osworld  web-search
gcp-1  e2-standard-4  8/4G          4         2         2         -
aws-1  m6i.xlarge     4/16G         4         3         3         -

binding constraints:
  gcp-1, swebench: mem-bound (4 sandboxes × 8GB > 16GB - headroom)
  aws-1, osworld:  cpu-bound (saturated at 3 × 4 vCPU)
```

Admin panel renders the same data graphically (spec 13).

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: static estimator, mixed-template packing, CLI matrix,
  online refinement loop, operator overrides.
- **Phase 1**: Cube overhead constants tuned with measurement once
  Cube backend lands.
- **Phase 2**: predictive packing for long rollouts, GPU sandboxes
  (MIG/MPS), per-cloud network-throughput awareness.
