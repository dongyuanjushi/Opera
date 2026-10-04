# 08 — Observability

## Purpose

Make it possible to (a) debug a single bad rollout, (b) understand
cluster-level throughput and bottlenecks, and (c) know when something
is silently broken — without an external monitoring stack the user
doesn't have admin access to set up.

## Metrics (Prometheus exposition)

Phase 0 exposes `/metrics` on the control plane and on each node
agent. No external Prometheus is required (the admin panel has its
own ring buffers), but the format is standard so a Prometheus instance
can scrape it later.

Core series:

| Metric | Type | Labels |
|---|---|---|
| `xrlenv_rollouts_started_total` | counter | template |
| `xrlenv_rollouts_finished_total` | counter | template, status |
| `xrlenv_step_latency_seconds` | histogram | template, backend |
| `xrlenv_sandbox_create_seconds` | histogram | template, backend |
| `xrlenv_sandbox_destroy_seconds` | histogram | template, backend |
| `xrlenv_sandbox_active` | gauge | node, template |
| `xrlenv_node_cpu_pct` | gauge | node |
| `xrlenv_node_mem_pct` | gauge | node |
| `xrlenv_node_disk_pct` | gauge | node |
| `xrlenv_capacity_max` | gauge | node, template |
| `xrlenv_capacity_used` | gauge | node, template |
| `xrlenv_warm_pool_size` | gauge | node, template |    *(phase 1)*
| `xrlenv_warm_pool_misses_total` | counter | node, template |  *(phase 1)*
| `xrlenv_scheduler_decisions_total` | counter | result, reason |
| `xrlenv_queue_depth` | gauge | template, owner |
| `xrlenv_queue_wait_seconds` | histogram | template |
| `xrlenv_admission_total` | counter | result | (`admitted`, `queued`, `rejected_full`, `cancelled_in_queue`, `queue_timeout`) |
| `xrlenv_sandbox_create_failed_total` | counter | template, reason |
| `xrlenv_sandbox_destroy_backlog` | gauge | node |
| `xrlenv_sandbox_phase_seconds` | histogram | template, phase | (`ensure_images`, `create`, `services`, `init`, `setup`, `teardown`) |
| `xrlenv_cancel_latency_seconds` | histogram | reason |
| `xrlenv_image_cache_hit_total` | counter | node, kind | (`hit`, `miss`, `evict`, `evict_failed`) |
| `xrlenv_image_pull_seconds` | histogram | node, template |
| `xrlenv_asset_fetch_total` | counter | result | (`ok`, `network_error`, `checksum_failed`, `timeout`) |
| `xrlenv_disk_pool_in_use_bytes` | gauge | node, pool |
| `xrlenv_disk_pool_budget_bytes` | gauge | node, pool |
| `xrlenv_node_command_stream_up` | gauge | node | (1 if reverse stream healthy) |
| `xrlenv_node_command_inflight` | gauge | node |
| `xrlenv_trajectory_sink_append_failed_total` | counter | sink, reason |
| `xrlenv_trajectory_sink_seal_failed_total` | counter | sink, reason |
| `xrlenv_replay_fetch_total` | counter | sink, result |
| `xrlenv_session_snapshot_seconds` | histogram | template | *(phase 3)* |
| `xrlenv_session_log_hot_bytes_total` | gauge | template, state | *(phase 3 — aggregate; per-session size lives in StateStore queries to avoid Prometheus high-cardinality)* |
| `xrlenv_session_log_hot_bytes_p95` | gauge | template, state | *(phase 3 — p95 across sessions per (template, state))* |
| `xrlenv_session_compaction_failures_total` | counter | reason | *(phase 3)* |
| `xrlenv_function_invocations_total` | counter | template, result |
| `xrlenv_function_invoke_seconds` | histogram | template |
| `xrlenv_function_pool_pending` | gauge | node, template |

### Multi-tenant labels

Rollout and admission metrics — concretely
`xrlenv_rollouts_started_total`, `xrlenv_rollouts_finished_total`,
`xrlenv_admission_total`, `xrlenv_queue_depth`,
`xrlenv_queue_wait_seconds`, `xrlenv_step_latency_seconds` — also
carry `owner_id` and `project_id` labels populated from the
multi-tenant hooks in spec 03 / spec 20. In phase 0 both
default to `"default"` (cardinality 1) so the labels are free;
phase 2 expands cardinality bounded by the operator's project
list. Other metrics (per-node hardware gauges, per-image cache
counters) do **not** take tenant labels.

## Structured logs

JSON logs to stdout. Every record carries `ts`, `level`, `event`,
`rollout_id?`, `sandbox_id?`, `node_id?`. Events worth recording:

- `sandbox.create`, `sandbox.create.failed`, `sandbox.destroy`
- `rollout.start`, `rollout.step`, `rollout.finish`, `rollout.truncate`,
  `rollout.fail`
- `scheduler.placed`, `scheduler.no_capacity`
- `node.heartbeat.late`, `node.disconnected`, `node.reconnected`
- `gc.orphan_destroyed`, `gc.ttl_destroyed`

Phase 0 writes to stdout (captured by systemd-journal on cloud nodes);
phase 1 ships a structured-log file rotation per node.

## Per-rollout artifacts (logs, trajectory, debug bundle)

Every rollout owns a directory on the node that ran it. Spec 20
holds the canonical layout; every entry below is always present
*except* `trajectory.jsonl`, which is only created when the
`platform-jsonl` sink is in effect:

```
~/.xrlenv/runs/<YYYY-MM-DD>/<rollout_id>/
├── meta.json              # canonical locator: template, task_key, group_id, deadlines, status, reason, trajectory_locator
├── trajectory.jsonl       # written ONLY when sink=platform-jsonl (or multi:[..., platform-jsonl, ...])
├── coordinator.log        # control-plane events for this rollout
├── node.log               # node-agent events for this rollout
├── stub.log               # in-sandbox stub stdout/stderr (tail-rotated)
├── env_adapter.log        # EnvAdapter-side logs (e.g., DesktopEnv output)
├── network.log            # phase 1; per-sandbox egress audit (spec 19)
└── blobs/                 # spec 14 BlobRef sidecars (screenshots, large files)
    └── <blob_id>.<ext>
```

When a template uses `slime-sample` / `verl-dataproto` / `none`,
the run dir exists with `meta.json` and the four log files but no
`trajectory.jsonl`; the locator points off-node (Slime data
buffer / verl DataProto / null). See "Trajectory sinks" below
and the durability matrix.

WHY a directory rather than scattered global logs: when an operator
(or `xrlenv attach`) wants to debug "what happened in rollout X," all
the relevant signal is in one place. Mirrors pool_server.py's
`RunContext.default_log_dir` pattern.

The control plane and node agent both emit per-rollout records into
their own `coordinator.log` / `node.log` files for that rollout via a
context-aware structured logger; the global stdout log still gets the
same record (so existing log aggregators keep working). Phase 2 moves
these directories to object storage (`gs://...` / `s3://...`) with
the same layout.

## `platform-jsonl` sink format

The schema below applies **only** to the `platform-jsonl` sink.
Native sinks (`slime-sample`, `verl-dataproto`) write their own
formats; `none` writes nothing. `client.replay` always starts
from the `TrajectoryLocator` (spec 20) and dispatches to the
right `TrajectoryReader` plugin (spec 17) — the JSONL line shape
below is not the universal trajectory schema, just one sink's
on-disk representation. See "Trajectory sinks" below for the
durability matrix.

When `platform-jsonl` is in the active sink chain, the run dir
contains a `trajectory.jsonl`. Each line:

```json
{"ts": 1714077600.123, "step": 0,
 "action": ..., "obs": ..., "reward": 0.0,
 "tokens":          [...],   // optional; filled when adapter has tokens
 "logprobs":        [...],   // optional
 "loss_mask":       [...],   // optional
 "response_length": ...,     // optional
 "info": {...}}
```

The token-level fields are **optional first-class** rather than buried
in `info`: when the EnvAdapter or trainer adapter has token data
(every LLM-RL setup), it populates them, and downstream tools can
rely on the schema. When the env is non-LLM (a generic shell env, a
classical RL task), the fields are absent. No lost generality;
typed consumability for the LLM-RL case.

Sealed with a `__final__` line containing status and total reward.

`client.replay(rollout_id)` reads the rollout's
`TrajectoryLocator` and, when the locator points at a
`platform-jsonl` body, reconstitutes a normalized `Trajectory`
from this file. For other sinks, replay delegates to the relevant
reader plugin and may return `ReplayUnavailable` per the
durability matrix below.

Phase 2 mirrors the per-rollout directory (including
`trajectory.jsonl` when present) to object storage at
`<bucket>/runs/<date>/<rollout_id>/...` per spec 20. Native sink
archival is sink-specific and does not flow through this mirror.

## Trajectory sinks (pluggable)

The `trajectory.jsonl` format above is the schema for the
**`platform-jsonl` sink**. Trainers like Slime (data buffer +
wandb) and verl (`DataProto` storage + wandb) also record their
own trajectory artifacts in their native shapes.

Recording in *both* formats is a deliberate trade-off, not a
mistake: the platform-jsonl shadow doubles disk and bandwidth at
LLM-token scale, but it buys durability after trainer crash and
a uniform replay path (spec 11 / 12 native sinks are RAM-only
without the shadow). The Slime / verl adapter defaults are
`multi:[platform-jsonl, native]` for that reason. Native-only
runs avoid the overhead and lose the durability — explicit
opt-out per spec 06 `allow_native_only_trajectory_sink`.

The recording layer is therefore pluggable:

```python
class TrajectorySink(Protocol):
    """Receives per-step records and the final seal for one rollout.
    The coordinator drives at most one sink per rollout — `multi:` is
    a single composite sink that internally fans out to its children,
    so there is still exactly one append/seal entry point per rollout."""

    async def open(self, rollout_id: str, meta: dict) -> None: ...
    async def append(self, record: TrajectoryRecord) -> None: ...
    async def seal(self, status: str, final_reward: float, summary: dict) -> None: ...
    async def close(self) -> None: ...
```

Built-in sinks:

| Sink id | Behavior |
|---|---|
| `platform-jsonl` (default) | Writes `~/.xrlenv/runs/<date>/<id>/trajectory.jsonl` exactly as documented above. Read by `xrlenv replay` and by spec 13's rollout-detail view. |
| `none` | Drops every record; the rollout dir is created without `trajectory.jsonl`. For trainers that record exclusively in their own format and want zero platform-side overhead. |
| `slime-sample` (phase 1) | Built by the Slime adapter; converts records to Slime `Sample` shape and writes them into Slime's data buffer. Spec 11. |
| `verl-dataproto` (phase 1) | Built by the verl adapter; emits to verl's `DataProto` convention. Spec 12. |
| `multi: [a, b, ...]` | Fans out to multiple sinks. Useful for "keep platform jsonl AND feed Slime's buffer" during a transition or for debugging. |

Selection is per-template (default), per-rollout (override), or
adapter-installed:

- Template manifest declares a default in
  `observability.trajectory_sink:` (see spec 06).
- A trainer adapter installs its sink at adapter init. The
  adapter default is `multi:[platform-jsonl, <native>]`, **not**
  the bare native sink — see specs 11 / 12. Adapter overrides
  apply unless either:
  - the template sets `trajectory_sink_pin: true` (rare; for
    templates that *require* a specific sink, e.g. smoke-test
    templates that pin platform-jsonl), in which case the
    adapter never overrides; or
  - the operator wants native-only and has set both
    `trajectory_sink: <native>` and
    `allow_native_only_trajectory_sink: true` (spec 06) — the
    explicit opt-out path. Without the second flag, the adapter
    rejects the override and falls back to the multi-sink
    default.
- `client.rollout(..., trajectory_sink="none")` overrides per-call
  for one-off needs.

### Replay implications

`xrlenv replay <id>` reads whichever sink wrote the trajectory:
- `platform-jsonl` → our jsonl, supported.
- `slime-sample` → Slime's buffer, supported when the user has
  Slime installed and the buffer is reachable.
- `verl-dataproto` → ditto for verl.
- `none` → replay returns `ReplayUnavailable("sink=none")`.

The default-jsonl sink remains the recommended choice for
debugging-heavy work and CI, even when running through Slime/verl,
because it gives a uniform replay path.

### Durability matrix

What each sink actually guarantees, so operators can pick honestly:

| Sink | Replay (`client.replay`) | Trajectory viewer (spec 17) | Native raw download | Retention owner | Required deps | Locator fields | Failure mode after trainer exit |
|---|---|---|---|---|---|---|---|
| `platform-jsonl` | yes, durable on node disk | yes | yes (jsonl) | XRLEnv (spec 09 layer 4) | none | `trajectory_uri = file:///.../trajectory.jsonl`, `trajectory_size_bytes` | survives; node-local file |
| `slime-sample` | best-effort | yes if Slime installed and buffer reachable | yes via Slime tools | Slime trainer process | `slime` package | `trajectory_uri = slime://buffer/<rollout_id>`, `slime_buffer_path` | **lost**: Slime's data buffer is per-iteration RAM unless explicitly checkpointed |
| `verl-dataproto` | best-effort | yes if verl installed and DataProto persisted | yes via verl tools | verl trainer process | `verl` package | `trajectory_uri = verl://dataproto/<rollout_id>`, `verl_index` | **lost**: DataProto is Ray actor state unless persisted |
| `multi:[a, b, ...]` | union of children | union of children | union | union | union | composite locator | each child fails independently |
| `none` | `ReplayUnavailable` | unavailable | unavailable | n/a | n/a | (no locator) | n/a |

### Phase-0 requirement: `platform-jsonl` is the durability floor

Phase 0 mandates `platform-jsonl` as the sink for **CI templates**
(`smoke-*`, `ci-*` prefixes) and any template that sets
`observability.trajectory_sink_pin: true`.

For Slime/verl integrations specifically, the adapter default is
`multi:[platform-jsonl, <native>]` — never the native sink alone.
A template that wants native-only must opt out explicitly with
`observability.allow_native_only_trajectory_sink: true` in its
manifest (spec 06); without the flag, the adapter rejects the
override at register and falls back to the multi-sink default.
This keeps the durability floor on by default for the most
common training paths.

This means: a Slime training run that crashes mid-iteration loses
its in-flight `slime-sample` records but can still inspect any
rollouts whose trajectories landed before the crash via `xrlenv
replay` against the platform-jsonl shadow. Operators who want to
opt out of the shadow set the template's
`observability.trajectory_sink: slime-sample` (no `multi:` wrapper)
and accept the durability matrix above.

### Reader plugin requirement

For any sink to be readable by spec 17's trajectory viewer or by
`client.replay`, the node-agent that wrote the rollout must have
the corresponding `TrajectoryReader` plugin available (spec 04
trajectory-fetch RPCs). `platform-jsonl` ships in core; the
slime/verl readers ship with the respective adapters and are only
loaded when the adapter is installed. Trying to view a
`slime-sample` rollout on a viewer host without the slime adapter
returns `ReaderUnavailable("slime-sample")`, never silent
corruption.

## OTel layer split (phase 1 — when we and trainers both trace)

Slime emits `trace_function` / `trace_span` around its generation
and rollout management code. verl has its own tracing. To avoid
double-counting and keep traces readable, we own a *layer* of spans
the trainer doesn't own, and vice versa.

**Env-layer spans (XRLEnv emits)** — the lifecycle of a sandbox and
what runs inside it:

```
Rollout (root, started by SDK)
├── start_rollout
│   ├── scheduler.place
│   ├── backend.create
│   └── env_adapter.setup
├── step (per step)
│   ├── backend.exec | env_adapter.step
│   └── reward.compute (when in-sandbox or external)
└── destroy
    └── env_adapter.teardown
```

**Token-layer spans (Slime / verl emit)** — what the trainer does
with rollouts:

```
GenerateRollout (root in trainer code)
├── generate_and_rm_group (Slime) | rollout_worker.generate (verl)
│   ├── sglang.generate / vllm.generate
│   ├── reward_model.score (token-side, if applicable)
│   └── dynamic_filter.apply (Slime)
├── update_weights
└── recover_engines (Slime)
```

Trace context propagates trainer → SDK → control plane → node so
the two layers nest into one trace. The SDK's `Client` reads any
incoming OTel context and includes it as a parent on the env-layer
spans. The control plane and node agent forward the context unchanged.

### Metrics: Prometheus (us) vs wandb / tensorboard (trainer)

Orthogonal:
- **Prometheus** (us) — platform health: rollout throughput, step
  latency, sandbox create latency, capacity utilization, image cache
  hit rate. The operator's window into "is the cluster healthy?".
- **wandb / tensorboard** (trainer) — model curves: reward over
  iterations, KL, loss, eval accuracy. The researcher's window into
  "is the training working?".

We don't try to push training curves into Prometheus, and we don't
try to scrape sandbox-level metrics into wandb. Each tool stays in
its lane.

## Tracing (phase 1)

OpenTelemetry spans around the hot path. Phase 1.x slice 4 (B7.1,
shipped 2026-05-11) lands eight initial spans on the control plane
and node:

| Span | Location | Key attributes |
|---|---|---|
| `xrlenv.coordinator.dispatch_rollout` | `RolloutCoordinator.start_rollout` | `template`, `task_key`, `group_id`, `deadline_s` |
| `xrlenv.coordinator.build_apply` | `BuildCoordinator.apply` | `dry_run`, `force`, `eager`, `skip_if_present`, `applied_by` |
| `xrlenv.scheduler.place` | `Scheduler.place` | `template`, `image`, `backend`, `node_count` |
| `xrlenv.node.create_sandbox` | `NodeAgent.create_sandbox` | `rollout_id`, `backend`, `image`, `node_id` |
| `xrlenv.node.env_step` | `NodeAgent.env_step` | `sandbox_id`, `node_id` |
| `xrlenv.node.ensure_present` | `ImageCacheManager.ensure_present` | `image`, `deadline_s`, `cache_hit` |
| `xrlenv.node.source_build` | `SourceBuilder.build` | `image_ref`, `source_type`, `skip_if_present`, `timeout_s` |
| `xrlenv.transport.rpc` | `RemoteNodeTransport._send_and_wait` | `command_kind`, `node_id`, `timeout_s` |

Three modes selected at first `get_tracer()` call:

- **Off (default)** — no env var → noop tracer. Hot-path cost stays
  at the level of a dict lookup + a `with` block; the module is safe
  to import without `opentelemetry-*` installed.
- **Console (dev)** — `OTEL_TRACES_EXPORTER=console` → spans
  pretty-printed to stderr. Useful for local debugging; never enable
  in prod (the formatter blocks the emitter thread).
- **OTLP (prod)** — `OTEL_EXPORTER_OTLP_ENDPOINT=http://host:4317`
  → spans export to that OTLP gRPC endpoint via
  `BatchSpanProcessor` (async, never blocks a hot path).

The two env vars are not mutually exclusive — setting both wires
both processors.

Phase 2 extends the catalog with per-step rollouts, in-sandbox
reward.compute spans, and a documented Tempo/Jaeger dashboard.

## Debug tools

- `xrlenv attach <sandbox_id>` — opens an interactive shell *into*
  a live sandbox via the stub's exec endpoint. Reads/writes survive
  the SDK call boundary.
- `xrlenv tail <rollout_id>` — streams the trajectory jsonl as it's
  written.
- `xrlenv events --since 5m` — pulls the events log from the state
  store with filtering.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: `/metrics` endpoint; structured stdout logs; trajectory
  jsonl on local disk; debug CLI.
- **Phase 1**: OTel spans; rolling-file logs; admin panel pulls
  metrics history (24 h) from a small in-process tsdb.
- **Phase 2**: trajectories in object storage; external
  Prometheus/Grafana/Tempo integration with documented dashboards.
