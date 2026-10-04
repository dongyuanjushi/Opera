# 04 — Node Agent

## Purpose

`xrlenv-node` is the per-host daemon that owns the local sandbox runtime.
One instance per machine (laptop, GCP VM, AWS EC2). It talks to the
control plane over a single **outbound bidi gRPC stream** — nothing
inbound — so cloud firewall rules stay simple (just allow egress to
the control plane).

### Reverse command stream

The node agent dials the control plane on startup and keeps one
long-lived bidi gRPC stream open. The full message envelope,
correlation rules, idempotency keys, flow-control caps, and
reconnect/replay protocol live in **spec 21**; this spec only
covers the node-side side effects.

Summary of message types (canonical list in spec 21):

- **Node → control plane**: `NodeHello`, `Heartbeat`,
  `SandboxEvent`, `StatsReport`, `LogTail`, `TrajectoryChunk`,
  `CommandReply`.
- **Control plane → node**: `ControlHello`, `CreateSandboxCommand`,
  `DestroySandboxCommand`, `FetchTrajectoryCommand`,
  `ImageDirectiveCommand`, `DrainCommand`, `GCCommand`,
  `StatsRequest`, `HealthProbe`, `CommandCancel`, plus
  `SnapshotCommand` (phase 3) and `InvokeCommand` (function-call).

This is the only transport between the two — there is no inbound
listener on the node, ever (invariant 7, spec 00). Spec 09's
`nodes.yaml` is operator metadata, not a dial list.

**`NodeHello` runtime advertisement (§5.3) is docker-ready-gated.** The hello
advertises `supported_runtimes` (the OCI runtimes docker has registered) so the
scheduler can place a `container_runtime` request (e.g. `sysbox-runc`). Because
the hello is sent once per connection, the node must not enumerate runtimes
against a not-yet-ready daemon: an agent that starts seconds after a docker
restart (the redeploy race) would fall back to a conservative `{runc}` and
advertise that for the whole connection — invisible to the runtime filter until a
manual restart. Two guards: (1) the node waits, bounded, for `docker info` to
answer before the first hello (the conservative fallback is never cached, so the
probe re-reads once docker recovers); (2) because the systemd unit is
`Requires=docker.service` (ordering) and **not** `BindsTo=`, a docker restart
that happens *after* the agent is up (e.g. installing a runtime) does not re-probe
the agent — so the deploy restarts the agent on runtime-pool nodes after the
runtime is confirmed present. Together the node advertises the real runtime set
regardless of daemon/agent start ordering.

### Local-only HTTP

The node agent does expose a **local-only** `/healthz` and
`/readyz` on `127.0.0.1` for systemd / k8s readiness checks
(useful when running under a cloud-init unit that wants to know
when the node is online). Slime / external pollers reach health
through the control plane's gRPC API, which proxies to the
node-agent stream — they do not reach the node directly. See
spec 11.

## Process shape

- Distribution: a wheel installed via `pip` plus a systemd unit on
  Linux, or `xrlenv node serve` for laptop/dev use.
- Connects to control plane on startup, sends a `RegisterNode` message
  containing hardware probe results, available backends, and a node
  identity (host fingerprint).
- Reconnects with exponential backoff on disconnect; preserves local
  sandbox state across reconnects.

## Responsibilities

```
xrlenv-node
├── BackendDriver(s)      # one per supported runtime; e.g. docker driver
├── HardwareProbe         # detect cores, mem, disk, net, kvm, gpu
├── SandboxTable          # local truth: id → handle, template, started_at
├── WarmPoolManager       # phase 1
├── ResourceMonitor       # ring buffer of cpu/mem/disk/net samples
├── GarbageCollector      # orphan detector
└── HealthServer          # 127.0.0.1-only /healthz for systemd/k8s; external probes go via control plane
```

## Hardware probe

Run at startup and every 5 minutes (cheap):

```python
@dataclass
class HardwareSpec:
    vcpus: int
    cpu_model: str
    mem_bytes: int
    disk_bytes_free: int
    disk_total_bytes: int
    net_mbps: int                      # link speed, not measured throughput
    has_kvm: bool                      # /dev/kvm exists and accessible
    has_gpu: bool
    gpu_model: str | None
    kernel_version: str
    os_release: str                    # "ubuntu 22.04" / "amzn 2023" / "darwin"
    cloud: Literal["gcp", "aws", "local", "unknown"]
    instance_type: str | None          # "e2-standard-4", "m6i.xlarge", ...
    page_size: int
```

Cloud detection: cheap metadata-service probes
(`http://metadata.google.internal/`, `http://169.254.169.254/`) with a
short timeout. Falls back to `unknown` on local laptops.

This dict is sent to the control plane on register and on probe-loop
delta. Capacity estimator (spec 10) consumes it.

## Resource enforcement

- Docker driver applies `cpus`, `memory`, `pids-limit`, `blkio-weight`
  per `ResourceSpec`. Bind-mounts (`ResourceSpec.mounts`) attach as
  `-v host:sandbox[:ro]`.
- Cube driver passes the spec through to the hypervisor; mounts attach
  as virtio-fs shares with hypervisor-enforced read-only when
  requested.
- Node agent refuses to oversubscribe: if a `create` request would exceed
  the capacity estimator's per-template cap, the agent rejects with
  `OverCapacity` and the scheduler retries elsewhere.
- The node agent validates each `MountSpec.host_path` against the
  allowlist defined in spec 19 (always-denied system paths, default
  allowed prefixes, operator-extensible). Violations raise
  `MountDenied` at create time — never at runtime, never silently.
- This means the scheduler's view and the node's view can disagree
  briefly under load — the node has the final say.

## Image cache (delegated to the Image Cache Manager — spec 15)

The node agent owns the local Docker / Cube image store but delegates
the *policy* (what to keep, what to evict, what to pre-fetch ahead of
time) to a co-located Image Cache Manager described in spec 15. The
node agent's role is mechanical:

- Report the image set, total disk, and per-image size to the control
  plane on heartbeat (so the cluster-wide view exists).
- Execute pulls / removes when the cache manager asks; never pull
  speculatively without a directive.
- Refuse to pull when free disk is below `min_free_disk_bytes`
  (default 10 GB or 5% of total, whichever is larger) until eviction
  has run.
- Keep a daily `docker image prune --filter until=24h` as a final
  janitor — but only over images not on the manager's keep-list.

The `xrlenv warmup <templates|instances>` CLI sends a directive to
the cache manager via the control plane; see spec 15 for semantics.

## Service supervision (delegated to the in-sandbox stub)

The node agent itself does not manage individual services inside
sandboxes — that lives in the in-sandbox stub (spec 01). Specifically
the stub:

- Runs the topological sort + health-gated launch of
  `spawn_services(...)`.
- Injects `XRLENV_SERVICE_PORTS_JSON = {name: port}` into every spawned
  process's environment so co-located services discover each other
  without hard-coded ports.
- Implements the `restart` policy (`never` / `on_failure` / `always`)
  by watching child processes and re-launching as configured. Restart
  events are emitted as stub log lines and surface as
  `service.restarted` events on the node-agent log stream.

The node agent only sees aggregate sandbox state (CPU/mem/disk via
`stats`, alive/dead via `/healthz`); it does not poll per-service
liveness.

## Warm pool (phase 1)

- Per-template `(min_idle, max_idle)` set in template manifest.
- On scheduler hit: take from the warm pool if available, else create.
- On scheduler miss for warm template: refill async up to `min_idle`.
- Idle sandboxes have a 30-min TTL; reclaimed if not consumed.
- Pool size capped by capacity estimator (warm pool counts toward
  utilization).

## Garbage collection

Three triggers:

1. **Startup GC**: enumerate all live sandboxes via the backend driver
   (`docker ps -a -f label=xrlenv`), compare to the persisted local
   sandbox table; destroy any present in the runtime but absent from
   the table (recovers from a crash mid-create).
2. **Control-plane reconcile**: when control plane reconnects after an
   outage, it sends the canonical sandbox list. Any sandbox the node
   knows about that the control plane no longer recognizes is destroyed.
3. **TTL sweep**: every 60 s, destroy sandboxes past their hard TTL.

All destroys go through a **bounded-concurrency executor**
(`asyncio.Semaphore(max_concurrent_destroys)`, default 8). WHY:
tearing down 200 sandboxes in parallel can wedge the Docker daemon and
starve in-flight calls; bounding concurrency keeps the daemon healthy
and gives the GC predictable throughput. Configurable per node.

## Graceful shutdown / drain

On `SIGTERM` (e.g. `systemctl stop xrlenv-node`, `xrlenv drain
<node>`), the node agent runs a documented drain protocol:

1. Mark itself unschedulable in the next heartbeat (control plane
   stops placing new sandboxes here).
2. Stop accepting new sandbox-create RPCs; respond `OverCapacity` to
   any straggler request.
3. For each running sandbox, allow it to finish naturally up to
   `drain_timeout_s` (default 300 s). The control plane decides
   whether a rollout is allowed to keep running or should be
   hard-deadlined; the node agent does not unilaterally kill live
   work.
4. After `drain_timeout_s`, force-destroy any remaining sandboxes
   through the bounded-concurrency executor.
5. Await pending destroys, flush the local sandbox table to disk,
   close the gRPC stream, exit cleanly.

This is the documented counterpart to operator-driven node removal in
spec 09. Without it, restarting a node leaves orphans the control
plane has to GC.

## Trajectory fetch

Trajectories live on the node that ran the rollout. The node agent
hosts the `TrajectoryReader` plugin set (one per
`TrajectorySink` — `platform-jsonl`, `slime-sample`,
`verl-dataproto`); each reader normalizes its sink's storage shape
into the cross-sink wire format. The control plane fetches via the
spec-21 `FetchTrajectoryCommand` on the existing reverse stream;
the node demuxes by `command_id`. Range, `include_binary`, and
summary-vs-body all live as fields on the command.

See spec 17 for the viewer plumbing and reader contract, spec 08
for the sinks the readers shadow, spec 14 for the `BlobRef` schema
governing binary fields, and spec 21 §"Trajectory fetch
multiplexing" for the wire details.

## Per-task concurrent cap

The node agent tracks `(task_key → count)` for currently running
sandboxes. On a `create` request carrying a `task_key`, if the count
is at or above `max_runs_per_task` (default 4, configurable), the
agent rejects with `OverCapacity` and the scheduler retries on a
different node. This is the data-plane half of the group anti-
affinity / GRPO fairness constraint described in spec 02 / spec 03.

## Acquire retry semantics (node-local saturation recovery)

Container create is bounded by a per-node create gate (a semaphore, default 4),
with a **tighter separate cap for sysbox** (non-`runc`) creates (default 1,
`XRLENV_RAW_SYSBOX_CREATE_CONCURRENCY`) because sysbox-fs pre-register is far
slower than a plain runc create. A create that fails with a *transient
busy-daemon* fault — a 5xx or timeout, e.g. `pre-register with sysbox-fs …
DeadlineExceeded` under a create burst — is **retried in place** with bounded
exponential backoff rather than failing the acquire. Rules that keep this safe:

- **Retryable set.** Only 5xx / timeout are retried. A clean dead-daemon
  `ConnectionError` and 4xx request faults (409/404) are terminal — a backoff
  can't revive a down daemon, so it fails fast (the control plane then marks the
  node unhealthy sooner). A down-daemon fault still feeds the AIMD admission
  limiter; it just isn't looped on locally.
- **AIMD accounting.** At most **one** health error is recorded per acquire that
  saw any health fault (not one per attempt) — the limiter edge-triggers on new
  errors, so counting each attempt would collapse the node's admission limit to
  the floor from one burst.
- **No duplicate-container leak (fail closed).** An ambiguous timeout may have
  actually spawned the container. Every raw container carries a unique
  `xrlenv.rollout_id` label, so before each retry the node reaps any container
  wearing this acquire's label. If that reap cannot be *confirmed* clean (the
  list failed, or a found orphan won't remove), the node does **not** create
  again — it fails closed and the caller re-submits with a fresh id; raw-GC reaps
  the residue.
- **Bounded wait.** The retry series is capped by the attempt count, a per-retry
  ceiling, a hard wall-clock total (~45 s), **and** the caller's remaining
  acquire wire budget, anchored at acquire entry. The control plane stamps the
  *effective* acquire timeout onto every remote acquire — the caller's explicit
  `acquire_timeout_s` when given, else the 600 s default the CP's `_send_and_wait`
  ceiling enforces — so even a default acquire arrives with a node-side deadline.
  A fail-fast caller fails fast on the node too; the node never replies to a
  command the control plane has already timed out. (A purely local, in-process
  acquire has no wire deadline and is bounded by the ~45 s hard cap alone.)

Cross-node re-admit through the `AdmissionQueue` (rebalancing a saturated node's
overflow onto a different node) is a **phase-1+ follow-on** for when there are
multiple dedicated sysbox nodes; see `notes/deferred_audit_todos.md`. Until then
recovery is node-local.

## Resource monitor

A 60-min ring buffer of `(timestamp, cpu_pct, mem_pct, disk_pct, net_mbps)`
samples at 5 s resolution (720 entries). Pushed to the control plane in
the heartbeat. Powers the admin panel's sparklines and the under-
utilization detector.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: Docker driver only; no warm pool; basic GC; in-memory
  ring buffer for resource samples.
- **Phase 1**: CubeSandbox driver; warm pool; ring buffer extended to
  24 h on disk for the admin panel's history view.
- **Phase 2**: GPU sandboxes (driver awareness, MIG/MPS plumbing); pod
  driver for k8s mode.

## Cross-references

- Reverse-stream protocol → [spec 21](21-node-control-protocol.md)
- Mount allowlist + sandbox hardening → [spec 19](19-security-model.md)
- Per-node paths and run-dir layout → [spec 20](20-state-and-storage.md)
- Trajectory readers → [spec 17](17-trajectory-viewer.md), [spec 08](08-observability.md)
- Disk pool budgets the GC and image cache cooperate over → [spec 10](10-capacity-estimator.md), [spec 15](15-image-cache-management.md)
