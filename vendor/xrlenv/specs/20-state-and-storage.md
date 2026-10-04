# 20 — State and Storage

## Purpose

Many specs reference "the state store," "object storage," "node-local
disk," and "the trajectory" without consistently saying what lives
where, who owns retention, and what survives which failure mode.
This spec is the single canonical answer.

It does not introduce new mechanisms; it consolidates and pins the
ones already declared in specs 03, 04, 08, 09, 15, and 18 so an
implementer can build the storage layer without re-deriving its
shape from scattered hints.

## Storage tiers

Five tiers, ordered by durability:

| Tier | Phase 0 substrate | Phase 1 / later | Survives |
|---|---|---|---|
| `node-local` | local SSD on the node-agent VM | same | node uptime |
| `node-local-cache` | `/var/cache/xrlenv/...`, evictable by spec 15 | same | best-effort |
| `control-plane-local` | SQLite at `~/.xrlenv/state.db` on the control-plane VM | redis (active + warm-standby in phase 1) | control-plane VM uptime |
| `cluster-shared` | optional NFS/EFS or registry mirror | distribution registry mirror, optional 3FS | cluster uptime |
| `object-store` | not required (phase 0); GCS / S3 if available | required for phase 2 trajectory durability and phase 3 cold-tier session logs | total cluster loss |

When this spec uses words like "durable," it means tier
`object-store`. Words like "off-node" mean any tier *not*
`node-local`. "Best-effort" means `node-local-cache`.

## StateStore schema

Two implementations; same logical schema (spec 03):

- `SqliteStore` — phase 0; `~/.xrlenv/state.db`. Journal mode is WAL by
  default (best reader/writer concurrency) but is selectable via
  `XRLENV_SQLITE_JOURNAL_MODE` (`WAL`/`TRUNCATE`/`DELETE`/`PERSIST`; applied
  before any schema DDL, fail-closed on an invalid/durability-unsafe value).
  **On a network filesystem (Lustre/FSx) set `TRUNCATE`:** WAL's memory-mapped
  `-shm` wal-index faults with a fatal `SIGBUS` there (it crash-looped both
  control planes 2026-07-30); a rollback-journal mode has no `-shm`, trading
  read/write overlap (fine at the CP's low, bounded write rate) for stability.
  The WAL checkpointer is a no-op under a rollback-journal mode.
- `RedisStore` — phase 1; sorted-sets for events, hashes for
  entities.

Logical tables:

```
nodes            (id, hw_probe_json, last_heartbeat_ts, expected_address, status)
sandboxes        (id, node_id, template, version, status, started_at, owner_count,
                  resource_spec_json, network_policy, mounts_json,
                  rollout_id?, session_id?)
rollouts         (id, request_id, template, template_digest,
                  task_key?, group_id?, owner_id?, project_id?, run_id?,
                  init_json, deadline_json,
                  status, reason?, final_reward?,
                  trajectory_sink, trajectory_node_id, trajectory_uri,
                  trajectory_size_bytes,
                  started_at, ended_at)
pending_rollouts (id, request_id, template, init_json, task_key?, group_id?,
                  deadline_json, owner_id?, submitted_ts, queue_partition)
sessions         (id, session_token, template, derive_from_json,
                  state, last_attach_ts,
                  durability_mode,                  # spec 18 §"Durability modes"
                  snapshot_replication_target,      # POLICY: node-local | cluster-shared | object-store
                                                    #   the target the session was started against;
                                                    #   distinct from latest_durable_snapshot_tier (state)
                  lifetime_mode, keep_warm_until,   # spec 18 §"Sandbox lifetime when detached"
                  max_resume_gap,                   # correctness contract
                  snapshot_chain_id,                # backend-opaque chain handle
                  latest_durable_snapshot_seq,      # OBSERVED: log seq of the latest snapshot known durable
                  latest_durable_snapshot_uri,      # OBSERVED: URI of that snapshot in its tier
                  latest_durable_snapshot_tier,     # OBSERVED: node-local | cluster-shared | object-store
                  cold_log_root_uri,                # resolved <session_cold>/logs/<session_id>/ for this session
                  cold_snapshot_root_uri,           # resolved <session_cold>/snapshots/<session_id>/ for this session
                  degraded,                         # bool — true if max_resume_gap was violated
                  degraded_reason,                  # string — populated when degraded=true
                  hot_log_size,
                  owner_id?, project_id?, run_id?)  # phase 3
session_snapshots (session_id, seq, snapshot_id, tier,
                   uri, created_at, durable)       # phase 3 — one row per snapshot taken
command_log_hot  (session_id, seq, ts, action_json, result_json,
                  duration_s)                      # phase 3 (bounded; idempotent field removed — see spec 18)
events           (ts, kind, rollout_id?, sandbox_id?, node_id?, payload_json)
idempotency      (request_id, rollout_id, ttl_ts)
audit            (ts, identity, scope, action, target, result, payload_json)
revocations      (token_digest, revoked_ts, reason)
```

Phase-0 fields like `owner_id` / `project_id` / `run_id` default to
`"default"`; spec 03 multi-tenant hooks (M12) and spec 19 admin
scopes consume them in later phases. Adding columns later is a
schema migration; baking the columns in now costs nothing and
avoids the rewrite.

Approximate sizes (rule of thumb, sized for sqlite phase 0):

| Table | Row size | Phase-0 row count budget | Notes |
|---|---|---|---|
| nodes | ~2 KB | 100 | hw probe is the bulk |
| sandboxes | ~1 KB | 10000 | rotated to events on destroy after `retention_days` |
| rollouts | ~2 KB | 100000 | locator only; trajectory body lives elsewhere |
| pending_rollouts | ~1 KB | 16384 (queue cap) | drains as scheduler admits |
| events | ~512 B | 5M | rotated by spec 09 layer 4 |
| idempotency | ~256 B | 100000 | TTL-trimmed |
| audit | ~512 B | bounded by retention policy | persisted longer than events |
| sessions | ~2 KB | low (phase 3) | one per long-lived session; widened to carry `durability_mode`, `snapshot_replication_target` (policy), `lifetime_mode`, `keep_warm_until`, `max_resume_gap`, `latest_durable_snapshot_*` (observed), `cold_log_root_uri`, `cold_snapshot_root_uri`, `degraded` flag, and the multi-tenant fields |
| session_snapshots | ~256 B | one per snapshot taken; bounded by `snapshot_every_k_steps` × session length, then compacted at session end | phase 3 — `(session_id, seq → snapshot_id)` mapping |
| command_log_hot | ~5 KB | bounded by snapshot cadence | spec 18 hot tier |

Total at phase-0 budget: ~25–40 GB raw, well within a single VM's
disk. Phase 1 redis fits in a few GB after compression.

## Run-artifact layout (canonical)

Per-rollout directory on the node that ran it:

```
~/.xrlenv/runs/<YYYY-MM-DD>/<rollout_id>/
├── meta.json              # ALWAYS present; carries TrajectoryLocator (template digest, task_key, group_id, deadlines, status, reason, locator)
├── trajectory.jsonl       # OPTIONAL; present only when `platform-jsonl` is in the active sink chain (spec 08)
├── coordinator.log        # control-plane events scoped to this rollout
├── node.log               # node-agent events scoped to this rollout
├── stub.log               # in-sandbox stub stdout/stderr
├── env_adapter.log        # EnvAdapter logs
├── network.log            # phase 1; per-sandbox egress audit (spec 19)
└── blobs/                 # spec 14 BlobRef sidecars (screenshots, large files)
    └── <blob_id>.<ext>
```

`meta.json` is the canonical locator handed to spec 17's
trajectory viewer; it is always present, even when the body is
held off-node by a native sink. `trajectory.jsonl` is body-only
and present only when `platform-jsonl` is active (alone or as
part of a `multi:[platform-jsonl, ...]` chain).

Phase 2 mirrors this directory layout (including the optional
`trajectory.jsonl` and `blobs/` when present) to object storage
at `<bucket>/runs/<YYYY-MM-DD>/<rollout_id>/...` after seal.
Native trainer-sink archival is sink-specific and does not flow
through this mirror.

## Trajectory locator

The `rollouts` row carries a locator populated at sink seal:

```python
@dataclass
class TrajectoryLocator:
    sink:            Literal["platform-jsonl", "slime-sample",
                             "verl-dataproto", "multi", "none"]
    node_id:         str | None       # node where the trajectory body lives
    uri:             str | None       # file://, slime://, verl://, gs://, s3://
    size_bytes:      int | None
    children:        list["TrajectoryLocator"] | None  # set when sink=multi
```

Spec 17's viewer and `client.replay` both read this locator and
dispatch to the right `TrajectoryReader` plugin. See spec 08's
durability matrix for which fields are populated under which sink.

## Session log and snapshot storage (phase 3)

Spec 18 defines three storage roles for sessions — hot log, cold
log, and snapshot-chain storage — sized and located independently:

- **Hot log**: `command_log_hot` rows in StateStore, bounded by
  `hot_log_max_entries` (default 10000) and snapshot cadence.
- **Cold**: object-storage chunks at
  `<session_cold>/logs/<session_id>/<snap_lo>-<snap_hi>.jsonl.gz`,
  one chunk per snapshot interval, signed by chunk hash. The
  `<session_cold>` root is the operator-configured
  `object_store.session_cold` URI (canonical config below); the
  `logs/` and `snapshots/` subtrees under that root are pinned
  by this spec.

Snapshots themselves:

- **Cube backend**: chainable overlaybd at
  `/var/lib/xrlenv/snapshots/<session_id>/<seq>.layer`. Layer
  chains compact at session end; phase 3 also archives the
  chain to object storage when configured.
- **Docker backend**: not eligible for sessions (capability flags
  in spec 01 forbid).

Replication and durability mode (spec 18 §"Durability modes"):

| Session `durability_mode` | Snapshot chain location | Survives trainer preemption? | Survives node loss? |
|---|---|---|---|
| `trainer-preemption-only` | node-local only | yes | no |
| `node-loss-resumable` | replicated to `cluster-shared` **or** `object_store.session_cold` (spec 20 storage tier) | yes | yes |
| `forensic-only` | node-local; cold-tier log archive only | n/a | n/a (no resume) |

The control plane refuses to start a `node-loss-resumable`
session on a cluster that has neither a `cluster-shared`
substrate nor a configured `object_store.session_cold` URI.

## Image and asset cache accounting

Spec 15 owns the policy; this section pins the on-disk layout so
spec 10's disk-pool accounting stays consistent.

```
/var/cache/xrlenv/
├── images/                        # docker / cube image refs (managed via runtime)
│   └── <runtime-managed>
├── lazy/                          # eStargz / overlaybd metadata + warmed chunks
│   └── <digest>/...
├── assets/<asset_id>/             # spec 06 assets, post-extract
│   └── (the asset's `extract_to` namespace)
├── pip/, uv/, npm/                # shared package caches (spec 06; trust-mode-gated)
└── _meta/
    └── pool_accounting.json       # last-known PoolAccounting snapshot
```

`pool_accounting.json` is the persisted form of spec 10's
`NodeDiskAccounting`; the cache manager and the capacity estimator
write/read it during boot so the multi-pool budget survives
restarts without re-derivation.

## Retention / GC matrix

| Artifact | Owner | Default retention | Spec |
|---|---|---|---|
| `nodes` rows | control plane | indefinite (small) | 03 |
| `sandboxes` rows | control plane | 30 days post-destroy, then archived to events | 03 |
| `rollouts` rows | control plane | indefinite (locator only; small) | 03 |
| `pending_rollouts` | control plane | drained on admission / cancel / restart | 03 |
| `events` | control plane | 14 days, configurable via `events_retention_days` (state-retention janitor) | 09 |
| `audit` | control plane | 30 days default, configurable via `audit_retention_days`; not governed by the generic events retention | 19 |
| `raw_rollouts` rows | control plane | terminal rows 14 days, configurable via `raw_rollout_retention_days`; active rows (`acquiring`/`running`) never pruned | 20 |
| `owner_rollout_lifetime` | control plane | **never pruned** — the janitor folds each pruned `raw_rollouts` row's `(owner, status)` tally here (same transaction as the delete) so the `/users` scoreboard stays cumulative across GC; bounded by #owners × #statuses | 20 |
| `idempotency` | control plane | TTL `idempotency_ttl_s` (default 300) | 02 |
| `command_log_hot` | control plane (phase 3) | bounded by snapshot cadence | 18 |
| `session_snapshots` | control plane (phase 3) | rolled into chain compaction at session end; cold-tier copy retained under `cold_snapshot_root_uri` (which resolves under `<session_cold>/snapshots/`) | 18 |
| Run artifact dir | node agent | 14 days (`retention_days`) | 09 |
| Platform-jsonl trajectory body | node agent | tied to run dir (`retention_days`, default 14); phase-2 mirror to object store | 08 / 09 |
| Native sink trajectories (Slime / verl) | trainer process | per trainer's retention; lost on trainer crash unless mirrored via `multi:[platform-jsonl, native]` (the adapter default — spec 11 / 12) | 08 / 11 / 12 |
| Image cache layers | image cache mgr | LRU; phase-1 lazy chunks GC'd by snapshotter | 15 |
| Asset cache | image cache mgr | LRU; sha256-quarantined finals dropped after 7 days | 15 / 19 |
| Cold-tier session log | object storage | 90 days default; configurable | 18 |
| Cold-tier snapshots | object storage | 30 days default; configurable | 18 |

Each owner runs its own GC loop; the table is the source of truth
when an audit asks "is data X still recoverable on day Y?"

The `state.db` tables above (`audit`, `events`, terminal `raw_rollouts`) are
swept by the **state-retention janitor** (`StateRetentionJanitor`) — a 24 h loop
sibling to the run-dir janitor. The spec-19 `audit` trail is the largest growth
source, but only when `auth.token_used` success-auditing is enabled (it is OFF
by default — see spec 19); with it off, `audit` is dominated by low-volume
`auth.denied`. This loop keeps `state.db` bounded regardless.
`DELETE` frees pages for reuse (bounding growth) but does **not** shrink the
file; the operator reclaims accumulated space with a one-time `VACUUM`
(`xrlenv db vacuum`, control plane stopped). `xrlenv db prune` triggers the
sweep on demand.

## Path defaults by host role

Single source of truth so specs stop sprinkling slightly different
paths:

```
control-plane VM
  ~/.xrlenv/state.db                          # SqliteStore (phase 0)
  ~/.xrlenv/secrets/<role>.token              # spec 19 tokens, mode 0600
  ~/.xrlenv/audit/<YYYY-MM>.jsonl             # rotated audit log (phase 1)
  ~/.xrlenv/admin-cache/                      # admin panel ring buffers / tsdb
  ~/.xrlenv/templates/                        # registered templates (spec 06)

node VM
  ~/.xrlenv/runs/<date>/<rollout_id>/         # per-rollout run dirs (spec 08)
  /var/cache/xrlenv/                          # image / asset / package caches
  /var/lib/xrlenv/scratch/<sandbox_id>/       # per-sandbox writable scratch
  /var/lib/xrlenv/snapshots/<session_id>/     # phase-3 Cube snapshot chains
  /etc/xrlenv/node.yaml                       # bootstrap config (spec 09)
  /etc/xrlenv/image-pins.yaml                 # operator pin list (spec 15)
  /etc/xrlenv/mount-allowlist.yaml            # per-spec-19 mount allowlist

operator workstation
  ~/.xrlenv/cli.yaml                          # endpoint + token references
  ~/.xrlenv/replay-cache/                     # local cache for `xrlenv replay`
```

Operators may override every path through `XRLENV_HOME`,
`XRLENV_NODE_HOME`, `XRLENV_CACHE_HOME`, and `XRLENV_SCRATCH_HOME`
environment variables; defaults shown above are what bootstrap
scripts (spec 09) and admin docs assume.

## Object storage integration

Phase-0 controllers and node agents speak only `file://` URIs.
Phase 2 promotes `gs://` and `s3://` to first-class — every
`TrajectoryLocator.uri` may be a remote URI, and the Trajectory
Viewer (spec 17) plus `client.replay` honor it through the same
`TrajectoryReader` plugin.

Configuration lives in `~/.xrlenv/storage.yaml` on the control
plane:

```yaml
object_store:
  trajectories: "gs://my-cluster/xrlenv/runs/"             # mirrored on seal (platform-jsonl bodies + run-dir artifacts)
  session_cold:  "gs://my-cluster/xrlenv/sessions/"        # session cold tier root (single config; subtrees below)
  audit_archive: "gs://my-cluster/xrlenv/audit/"
  retention:
    trajectories_days: 365
    session_cold_days: 90
```

`session_cold` is a single configuration knob; the spec pins two
**fixed subtrees** under it so log archive and snapshot archive
never collide:

```
<session_cold>/logs/<session_id>/<snap_lo>-<snap_hi>.jsonl.gz
<session_cold>/snapshots/<session_id>/<seq>.layer            # phase 3, when chain replication is enabled
<session_cold>/snapshots/<session_id>/manifest.json          # session_snapshots cold-tier index
```

The control plane writes to both subtrees; readers (resume,
trajectory viewer historical mode) consult `manifest.json` for
the snapshot chain and the log chunks under `logs/`. Operators
who prefer separate buckets can override either subtree via
`object_store.session_logs` and `object_store.session_snapshots`
explicitly; when both are unset, the `session_cold/{logs,
snapshots}` defaults apply.

When configured, the seal hook in the platform-jsonl sink performs
a single-shot upload after closing the local file; the local file
is preserved for `retention_days` so warm replay still hits node
disk.

Failures: object-store upload failures are logged and retried with
exponential backoff up to 24 h; the rollout still seals as
`finished` / `truncated` / `cancelled` / `failed` on the local
record. The locator's `uri` only flips to the remote form once
upload succeeds; until then it points at the local file.

## Cross-references

- StateStore mechanics → [03](03-control-plane.md)
- Node-local artifacts → [04](04-node-agent.md), [08](08-observability.md), [09](09-deployment-and-gc.md)
- Cache layout → [15](15-image-cache-management.md), [10](10-capacity-estimator.md) for the disk pools
- Session storage → [18](18-sandbox-sessions-and-resume.md)
- Token / audit storage → [19](19-security-model.md)
- Multi-tenant fields baked in early → spec 03 / 19 phase ladders

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: SQLite StateStore; node-local run dirs; default
  paths above; no object storage required.
- **Phase 1**: redis StateStore; pool-accounting persistence;
  signed audit log archive.
- **Phase 2**: object-store mirror for trajectories and audit
  archive; multi-tenant filters consume the `owner_id` /
  `project_id` columns.
- **Phase 3**: cold-tier session logs and snapshot chain archive
  in object storage; hot tier sized by snapshot cadence.
