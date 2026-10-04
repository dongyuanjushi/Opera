# Design principles and invariants

This page is for contributors changing XRLEnv internals. It records the
load-bearing design rules that user-facing docs should not need to explain in
full.

## Three-plane split

XRLEnv separates the Consumer plane, control plane, and data plane so trainer
logic, scheduling, and sandbox execution remain independently replaceable.

**Consumer plane.** Owns policy, reward processing, and trajectory consumption.
It calls `Client.rollout`, `Client.batch_rollout`, and `Client.replay`.

**Control plane.** Owns scheduling, admission, deadlines, registry state,
template catalog, security checks, and trajectory sealing. It knows both
Consumer requests and sandbox metadata, but only in the narrow shape “schedule
this template, return a rollout session.”

**Data plane.** Runs one `xrlenv-node` daemon per host. It owns local sandbox
creation, destruction, image/cache state, and the in-sandbox stub connection.
Nodes initiate outbound bidi streams to the control plane.

## Core invariants

### Sandbox identity is separate from rollout identity

`sandbox_id` and `rollout_id` are separate columns with separate lifecycles.
Phase 0 destroys the sandbox at rollout finish, but code must not assume
`sandbox_id == rollout_id`; sandbox sessions break that equation.

### Capacity is released only on node-confirmed destroy

The scheduler treats a sandbox slot as occupied until the owning node confirms
destroy. “Destroy enqueued” is not “slot free.”

### Trajectories are immutable after seal

Once a sink emits `seal(...)`, the record set for that rollout is frozen. Late
reward updates write a new record set rather than mutating the sealed
trajectory.

### Template manifests are pinned at run start

The control plane pins each template by `(name, version, digest)` at run start.
Later edits to the same `(name, version)` are visible only to future runs.

### Resolved instance config is fixed for the life of a rollout

The instance resolver runs once at admission. The resulting image refs, asset
refs, mounts, and per-instance resource spec remain fixed through
cancel/retry/resume.

### The node is final authority on local resources

The scheduler works from heartbeat state, but the node may reject placement
with `OverCapacity` if live local state disagrees.

### Node transport is outbound only

All control-plane-to-node communication rides the bidi gRPC stream initiated by
the node. The node agent does not open an inbound listener.

### Metadata stays in state; blobs stay outside

Trajectory bodies, snapshot artifacts, command-log chunks, and image layers do
not belong in SQLite or Redis. The state store carries metadata and locators.

### `task_key` is fairness; `instance_id` is identity

`task_key` is opaque to the platform and used for anti-affinity and
`max_runs_per_task`. `instance_id` is the cache key used by image, asset, and
replay machinery.

### Reward mode is validated at template registration

A manifest/adapter reward-mode mismatch should fail at registration, not in the
middle of a rollout.

## Mechanism, not policy

XRLEnv core ships primitives such as `task_key`, `group_id`,
`cancel_rollout`, `cancel_group`, anti-affinity, and `max_runs_per_task`.
Engine-specific over-request, filter, and cancel loops belong in Consumer-side
trainer adapters, not in `xrlenv/control/`.

If a change needs trainer-framework-specific logic, keep it out of the core
control plane and put it behind an adapter boundary.
