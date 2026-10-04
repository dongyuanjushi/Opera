# 21 — Node Control Protocol

## Purpose

The control plane and node agent talk over a single bidi gRPC
stream the node initiates outbound (spec 04 invariant 7,
spec 19). This spec is the wire-level contract: message shapes,
ordering rules, idempotency, flow control, reconnect behavior,
and how higher-level features (trajectory fetch, image directives,
sandbox lifecycle) frame themselves on the stream.

It exists because most other specs gloss over "the node agent has
an outbound stream" and leave reverse-direction commands implicit.
That works for an example, not for an implementation.

## Stream lifecycle

```
node-agent boot
  ↓
TLS dial control-plane gRPC
  ↓
client opens bidi stream:  rpc NodeControl(stream NodeMsg) returns (stream ControlMsg)
  ↓
node sends NodeHello
control sends ControlHello + bootstrap config
  ↓
steady state: heartbeats, events, commands, replies
  ↓
on disconnect:
  • node enters reconnect loop (exponential backoff, jitter)
  • control marks node "disconnected" after `node_disconnect_grace_s` (default 15)
  • control retains pending commands and unacked replies in StateStore
  ↓
on reconnect:
  • node sends NodeHello with last seen `command_seq` and `event_seq`
  • control replays unacked commands past that seq
  • node replays unacked replies / events past that seq
  • streams converge; node resumes "connected"
```

Every connection is one stream; the node agent does not open a
second connection for any feature. Multiplexing happens at the
message layer.

## Message envelope

All messages on the stream wrap a typed envelope. Both directions
share a single **stream epoch** (the node owns it, fresh per dial)
and carry a sequence number monotonic within that epoch:

```proto
message NodeMsg {
  string stream_epoch = 1;    // node-generated UUIDv7; constant for the lifetime of one TCP/TLS stream; rotates on every reconnect
  uint64 seq          = 2;    // monotonic within stream_epoch; resets to 0 on each new epoch; covers ALL NodeMsg traffic (heartbeats, events, replies, chunks, acks)
  oneof body {
    NodeHello       hello       = 10;
    Heartbeat       heartbeat   = 11;
    SandboxEvent    sandbox     = 12;
    StatsReport     stats       = 13;
    LogTail         logs        = 14;
    TrajectoryChunk traj_chunk  = 15;
    CommandReply    reply       = 16;     // execution completion / failure for a command — NOT transport ack
    Ack             ack         = 90;     // transport-level delivery ack of the OPPOSITE (ControlMsg) direction
  }
}
message ControlMsg {
  string stream_epoch        = 1;    // mirrors the node's current stream_epoch from NodeHello
  string control_instance_id = 2;    // UUID of the control-plane process; stable across reconnects to the same instance, different on takeover
  uint64 seq                 = 3;    // monotonic within stream_epoch on this direction; resets to 0 on each new epoch; ControlHello consumes seq=0 in every new epoch
  oneof body {
    ControlHello              hello         = 10;
    CreateSandboxCommand      create        = 20;
    DestroySandboxCommand     destroy       = 21;
    DrainCommand              drain         = 22;
    GCCommand                 gc            = 23;
    StatsRequest              stats_req     = 24;
    HealthProbe               health        = 25;
    FetchTrajectoryCommand    fetch_traj    = 26;
    ImageDirectiveCommand     image_dir     = 27;
    SnapshotCommand           snapshot      = 28;     // phase 3
    InvokeCommand             invoke        = 29;     // function-call mode (spec 01)
    CommandCancel             cancel        = 91;
    Ack                       ack           = 90;     // transport-level delivery ack of the OPPOSITE (NodeMsg) direction
  }
}

message Ack {
  string stream_epoch = 1;     // the epoch being acked (must equal the receiver's current stream_epoch)
  uint64 up_to_seq    = 2;     // contiguous prefix of the OPPOSITE direction's seq that the sender has durably received
}
```

Two distinct concepts that must not be confused:

- **`Ack`** — transport-level delivery acknowledgement.
  Direction-asymmetric: a `ControlMsg.Ack` acks `NodeMsg.seq`; a
  `NodeMsg.Ack` acks `ControlMsg.seq`. Either side may emit one
  whenever it has durably received a contiguous prefix of the
  opposite direction's stream. It says **only** "I received bytes
  up to this seq"; it makes no claim about whether the
  corresponding command has been executed.
- **`CommandReply`** — execution completion (or failure) for a
  specific command, carried only on `NodeMsg`. It says "the
  command identified by `command_id` finished, here is the
  result." It is *not* a transport ack: the control plane's
  in-memory `Ack` of `CommandReply.seq` is what frees the reply
  from the node's outbox. A node must not consider a command
  durably reported until the control plane has Acked the
  `CommandReply`'s seq.

`ControlHello` is the first `ControlMsg` of every new epoch and
**does** consume `ControlMsg.seq = 0`. Symmetrically, `NodeHello`
is the first `NodeMsg` of every new epoch and consumes
`NodeMsg.seq = 0`. All later `seq` values in either direction are
strictly monotonically increasing within the epoch.

The two identifiers cooperate:

- **`stream_epoch`** belongs to the node — it is fresh for every
  outbound dial (the node generates a new UUID on dial, before
  sending `NodeHello`). Both directions' `seq` numbers are scoped
  to the current epoch. The two directions have **independent
  seq spaces** (a `NodeMsg.seq=42` and a `ControlMsg.seq=42`
  refer to different messages); each `Ack` names which direction
  it acks via the message type that carries it (`ControlMsg.Ack`
  acks `NodeMsg.seq`; `NodeMsg.Ack` acks `ControlMsg.seq`).
- **`control_instance_id`** belongs to the control-plane process
  — stable across reconnects to the *same* process, new on
  restart or replacement. The node uses it to distinguish "I am
  reconnecting to the same control plane I knew" from "this is a
  new control plane; prior in-flight commands belong to a dead
  instance."

There is **no separate sequence space for heartbeats**. They
ride the unified `NodeMsg.seq` like everything else. Earlier
text mentioning a separate "control channel" was wrong; the only
distinction is that heartbeats are not subject to the
`max_inflight_commands` cap below — but they consume the same
seq counter.

`NodeHello` carries the new `stream_epoch` plus
`(prior_stream_epoch, last_seen_control_instance_id,
last_command_seq_seen_in_prior_epoch,
last_reply_seq_acked_in_prior_epoch)` so the control plane can
decide what (if anything) to replay.
`ControlHello` carries `control_instance_id`, mirrors the new
`stream_epoch`, and the control plane's last-acked NodeMsg seq
from the prior epoch.

### Replay model: replay by `command_id`, not by reusing `seq`

When the control plane resends a command after reconnect, it
**does not reuse the prior epoch's `seq`**. The resent command
gets a fresh `seq` in the new epoch but preserves `command_id`
and `idempotency_key` (per "Command / reply correlation" below).
The node's idempotency cache deduplicates by
`idempotency_key`, so:

- Resending a command that the node already executed returns the
  cached `CommandReply` immediately.
- Resending a command the node never saw runs it for the first
  time.

`Ack { stream_epoch, up_to_seq }` covers transport-level
delivery within the current epoch only. It is *not* a contract
that the command has been executed — that is what `CommandReply`
is for.

Prior-epoch seq numbers are used **only** during the
`NodeHello`/`ControlHello` reconciliation to decide what must be
resent. Concretely: each side compares its own outbox of unacked
prior-epoch messages against the peer's `last_..._seq_acked_in_
prior_epoch`; anything not acked is resent under fresh seq
numbers in the new epoch (preserving `command_id` /
`idempotency_key` for commands). Once that resend is dispatched,
prior-epoch ack state is **retired**; the new epoch's acks govern
delivery from then on. This is not "unacked prior-epoch bytes are
implicitly delivered" — they are explicitly resent — it is "the
prior-epoch seq counter has done its job and is no longer
referenced."

## Command / reply correlation

Every `ControlMsg` body carrying an action also carries:

```proto
message CommandHeader {
  string command_id        = 1;     // UUIDv7; control-plane generated
  string idempotency_key   = 2;     // see "Idempotency" below
  google.protobuf.Timestamp deadline = 3;
  uint32 max_retries       = 4;     // soft bound; control may resend
}
```

The node replies with `CommandReply { command_id, status, payload,
error }` on the same stream. The control plane considers the
command "in flight" until it observes either a reply or a
disconnect-then-reconnect-without-reply (handled per "Reconnect
and replay" below).

Commands that produce streaming output (`FetchTrajectoryCommand`)
reply with multiple `TrajectoryChunk`s carrying the same
`command_id`, terminated by a final `CommandReply` with
`status=OK`.

## Idempotency

Every command carries an `idempotency_key`. The node agent
maintains a bounded LRU (`idempotency_cache_size`, default 4096)
of `(idempotency_key → CommandReply)`. A repeat with the same key
returns the cached reply without re-running the action. Default
TTL: 300 s (matches the rollout idempotency window in spec 02 —
one shared knob).

Key derivation per command (control plane is responsible for
producing them deterministically so retries on its side hit the
node's cache):

| Command | Idempotency key derivation |
|---|---|
| `CreateSandboxCommand` | `rollout_id` (one sandbox per rollout in phase 0) |
| `DestroySandboxCommand` | `sandbox_id + ":destroy"` |
| `FetchTrajectoryCommand` | `rollout_id + ":" + range_hash` |
| `ImageDirectiveCommand` | sha256 of the directive's image-set + horizon + deadline |
| `DrainCommand` | `node_id + ":" + drain_epoch` |
| `GCCommand` | `node_id + ":gc:" + epoch` |
| `StatsRequest` | `node_id + ":stats:" + bucket_ts` |
| `HealthProbe` | `node_id + ":health:" + epoch` |
| `SnapshotCommand` | `session_id + ":snap:" + seq` (phase 3) |
| `InvokeCommand` | per-spec-01 `request_id` from the trainer |

Replies on the node side also carry sequence numbers; on
reconnect, the control plane replays unacked commands with the
*same* `command_id` and `idempotency_key`, so the node either
returns its cached reply (if it ran the command) or runs it
fresh (if the disconnect happened before execution).

## Flow control

gRPC's HTTP/2 flow control covers byte-level backpressure; we add
two per-stream knobs on top:

- **`max_inflight_commands`** (default 64): control plane never
  has more than this many unreplied commands outstanding to one
  node. Exceeding pushes commands into the `pending_rollouts`
  queue (or its non-rollout equivalent for image / drain
  directives) until the node frees a slot via `CommandReply`.
- **`max_inflight_traj_chunks`** (default 16): per
  `FetchTrajectoryCommand`, bounds outstanding chunks before the
  control plane sends an `Ack` for the chunk window. Without
  this, a slow viewer would pin the node's RAM.

Heartbeats ride the unified `NodeMsg.seq` space (see "Message
envelope" above — there is no separate heartbeat seq). They are
exempt from `max_inflight_commands` because they are not
commands; they are events that consume one seq each and do not
block the inflight-command counter.

## Reconnect and replay

On disconnect the node agent loses its TLS connection but keeps
its in-memory sandbox table, idempotency cache, and unacked
command/reply queues. Reconnect rules:

1. Exponential backoff: 1s, 2s, 4s, ..., capped at 30s; ±20%
   jitter. Up to `reconnect_max_s` (default 600) before the node
   exits and lets systemd/k8s restart it.
2. The node mints a fresh `stream_epoch` on each dial.
   `NodeHello` carries:
   - the new `stream_epoch`,
   - the `prior_stream_epoch` (or null on first connect),
   - `last_seen_control_instance_id` (or null on first connect),
   - within the prior epoch: `last_command_seq_seen` and
     `last_reply_seq_acked`.
3. The control plane consults `last_seen_control_instance_id`:
   - **Same instance** (`control_instance_id` matches): both sides
     are reconnecting to a continuous logical session. The control
     plane resends any commands it has not yet observed a
     `CommandReply` for, under the **same** `command_id` and
     `idempotency_key` but with **fresh seq numbers in the new
     epoch** (Model A — see "Replay model" above). The node's
     idempotency cache deduplicates by `idempotency_key`.
   - **Different instance** (`control_instance_id` mismatch — the
     control plane was restarted or replaced): the new instance
     does **not** retry the prior instance's in-flight commands.
     It reconciles state via `Heartbeat` and a `GCCommand` and
     decides per-command from the table below.
4. The node resends any `NodeMsg` it has not yet observed an
   `Ack` for, again with **fresh seq numbers in the new epoch**.
   Each direction's `Ack { stream_epoch, up_to_seq }` covers a
   contiguous prefix of the *current* epoch only.
5. After exchange settles, both sides resume the steady state. No
   command is silently dropped, and no command is retried under a
   different `command_id` — every retry is idempotent by
   construction.

### Control-plane replacement: command outcome table

When the control plane process is replaced (`control_instance_id`
mismatch), the new instance must decide what to do with each
class of in-flight command the prior instance had outstanding.
The node side reports the partial state via the next `Heartbeat`
+ targeted `SandboxEvent` messages; the new control plane acts
per this table.

| Command class | Node-side state on reconnect | New control plane action |
|---|---|---|
| `CreateSandboxCommand` (in progress) | sandbox table contains the partially-created sandbox | issue `DestroySandboxCommand` (cleanup); the owning rollout was already terminalized as `failed`/`reason=control_plane_lost` per spec 03 |
| `CreateSandboxCommand` (completed pre-crash, reply lost) | sandbox table has the live sandbox | reconciler sees an unknown sandbox, issues `DestroySandboxCommand` (the owning rollout's stream is dead) |
| `DestroySandboxCommand` (in progress) | partial destroy state | re-issue with the same `idempotency_key`; node's cache returns the cached reply if destroy completed, otherwise resumes |
| `FetchTrajectoryCommand` (in progress) | streaming chunks were aborted | new instance does **not** retry; the operator viewer that requested it is not connected; idempotency-cache entry expires per TTL |
| `ImageDirectiveCommand` (in progress) | pulls may still be running on the node | re-issue if the directive is still relevant (operator's `xrlenv warmup` is still active); the directive's `idempotency_key` (sha256 of content) deduplicates |
| `DrainCommand` (in progress) | node is draining | re-issue with the same `drain_epoch`; idempotency-cache returns the in-progress reply |
| `GCCommand` (in progress) | GC pass partially done | re-issue with a new `epoch` — GC is naturally idempotent |
| `StatsRequest` / `HealthProbe` | n/a (request/reply, no persistent state) | not retried; metric scrapes resume on the new instance |
| `SnapshotCommand` (phase 3, in progress) | snapshot may be partially written | re-issue with the same `(session_id, seq)` key; node's idempotency cache returns the cached `SnapshotID` if completed |
| `InvokeCommand` (in progress) | function-call executor still running | not retried by the new instance; the `request_id`-keyed cache means a trainer-side retry will find the cached reply for `idempotency_ttl_s` |

Net effect: every command is either reconciled (cleanup) or
re-issued under the same idempotency key. There is no scenario
where a control-plane replacement causes a duplicate sandbox, a
duplicate destroy, or a duplicate function-call invocation.

## Command cancellation

Some commands need to be cancellable mid-flight (a
`FetchTrajectoryCommand` while the operator closes the viewer; a
`CreateSandboxCommand` whose owning rollout was just cancelled).
The control plane sends:

```proto
message CommandCancel {
  string command_id = 1;
}
```

(included in the `ControlMsg.oneof` as variant `91`). The node
either replies with `CommandReply { status=CANCELLED }` or, if
the command has already produced a terminal reply, returns the
cached reply. Cancellation is best-effort: a `CreateSandboxCommand`
whose backend create has already started is allowed to finish if
unwinding mid-create would leak more state than completing.

Cancellation latency target: p95 ≤ 1 s. Surfaces as
`xrlenv_cancel_latency_seconds{reason="command"}` in spec 08.

## Trajectory fetch multiplexing

`FetchTrajectoryCommand`:

```proto
message FetchTrajectoryCommand {
  CommandHeader header     = 1;
  string  rollout_id       = 2;
  string  trajectory_uri   = 3;     // from the rollouts row
  Range   range            = 4;     // step range or byte range
  bool    include_binary   = 5;     // BlobRefs (spec 14)
  bool    chunked          = 6;     // streamed reply if true
}
```

Multiplexing rules:

- Multiple `FetchTrajectoryCommand`s can be in flight to the same
  node concurrently up to `max_inflight_traj_chunks` total chunks
  across them.
- Chunks carry the `command_id` of their owning fetch; the
  control plane demuxes by `command_id` for the trajectory viewer.
- A `CommandCancel` for a fetch stops chunk emission promptly;
  the node may still emit one final terminator chunk.

## Image directive delivery

`ImageDirectiveCommand` carries a list of `(image_or_asset_ref,
priority, deadline_s)` tuples. The node hands the directive to its
local `ImageCacheManager` (spec 15). Replies:

- Initial `CommandReply { status=ACCEPTED }` immediately on
  receipt (the directive is enqueued).
- Periodic `SandboxEvent { kind="image.warmup_progress" }` as
  pulls complete or fail; these are not replies, just events.
- Final `CommandReply { status=OK, payload=WarmupReport }` when
  the directive's deadline elapses or the set is fully resident.

Idempotency key (sha256 of the directive's content) means the
control plane can re-issue the same directive without
duplicating pulls.

## Drain and GC

`DrainCommand` triggers spec 04's drain protocol. The reply
streams progress events (`drain_started`, `sandboxes_remaining`,
`drain_complete`) as `SandboxEvent`s and a final `CommandReply`
when the drain timer elapses or the count hits zero.

`GCCommand` is the control plane asking the node to run an
immediate reconcile pass; reply is the count of orphans
destroyed.

## Health probing through the stream

Spec 11's `Healthz(node_id)` and `Readyz(node_id)` (control-plane
gRPC) consult the node-registry's last-known state populated by
heartbeats; they do not issue a fresh `HealthProbe` on every
poll. `HealthProbe` is reserved for "operator forced a recheck"
flows (admin panel button, `xrlenv health <node> --force`) and
for probe-loop refreshes (default 60 s).

## Wire size budget

A heartbeat is ~2 KB (resource snapshot + ring-buffer delta + per-
sandbox stats summary); at 5 s cadence one node generates ~35 MB
per day of inbound traffic. A 100-node cluster: ~3.5 GB/day,
well under the control plane's gRPC capacity. Trajectory fetches
are bursty — the budget assumes operator-driven (a few per
minute), not training-loop driven (training reads from native
sinks per spec 08).

## Fleet acquire fields (multi-container tasks, phase 1)

Fleet reservation (spec 03) is admitted and accounted entirely
control-plane-side (spec 10); the node agent is **fleet-unaware**. It
receives ordinary create / acquire-container commands whose placement and
cgroup limits the scheduler has already decided. The declaration is
**generic** fleet metadata — the core assumes no container roles — and
travels across two hops:

**Consumer / harness → control plane.** The consumer declares a fleet with
three **generic Docker labels** on the acquire, mirroring how
`xrlenv.task_key` / `xrlenv.group_id` already flow (no new SDK/RPC surface,
and the platform never infers a fleet from anything else):
- **`xrlenv.fleet_id`** (opaque string) — set on every container of the
  fleet. A *third* id axis: reservation identity, distinct from `task_key`
  (fairness) and `instance_id` (identity), invariant 9.
- **`xrlenv.fleet_cpu_request`** / **`xrlenv.fleet_mem_request`** — the
  fleet's **peak** footprint, set on the **first** acquire for a `fleet_id`,
  omitted on companion acquires (which draw from the reservation the first
  acquire opened). The scheduler reads these; they never travel further.

The control plane parses these labels next to `task_key` / `group_id` and
runs the reservation (spec 03 / 10). **No fleet labels → the acquire is
admitted independently, exactly as before** (the faithful default).

**Control plane → node** (`AcquireContainerCommand` / `CreateSandboxCommand`).
Adds **`fleet_id` only**, applied as a container **label** so the raw-GC
reconciler (spec 09) and the reservation-release path can map a container
back to its fleet; the node does nothing else with it. **`fleet_footprint`
is not a CP→node field** — it is a scheduler input consumed at admission
and never reaches the node.

New reply status **`FleetOverBudget`** — the scheduler rejects a companion
acquire that would push the fleet past its declared footprint *before* any
node command is issued; returned to the consumer, the fleet's other
containers untouched. Sits alongside `OverCapacity` (the invariant-6
node-side rejection) but is a control-plane decision.

**Reservation release** rides the existing destroy / `list_raw_containers`
reconcile path (see *Drain and GC*): when the node confirms destroy of the
fleet's final labeled container, the control plane frees the reservation
(invariant 2). A `fleet_id` idle past `fleet_reservation_ttl_s` is
reclaimed even if a destroy was missed.

Wire: `fleet_id` (string) is added to `AcquireContainerCommand` /
`CreateSandboxCommand` as the node label; the footprint is **not** a node
field (declared consumer→CP via the labels above); `FleetOverBudget` joins
the reply-status set. All backward-compatible — an older node ignores
`fleet_id` beyond labeling, and an older control plane that never sets it
keeps today's per-container admission.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: stream lifecycle, message envelope, command-reply
  correlation, idempotency cache, flow-control caps, reconnect
  + replay, command cancellation, trajectory fetch multiplexing,
  image directive delivery, drain / GC commands.
- **Phase 1**: mTLS in place of bearer tokens; signed
  `ControlHello.instance_id` so node can refuse a forged control
  plane.
- **Phase 2**: priority lanes for commands (admin > scheduler >
  background); preemption of low-priority fetches when capacity
  binds.
- **Phase 3**: `SnapshotCommand` and session-aware command shapes.
