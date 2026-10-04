# 05 — Consumer SDK

## Purpose

The Python client library consumers use. Wraps the gRPC API in a
shape that's natural to call from a rollout function or a Ray actor.
Stays consumer-agnostic; framework-specific glue lives in
`xrlenv/adapters/{slime,verl}.py` (specs 11 and 12).

## Surface

```python
from xrlenv import Client, Template, Deadline

client = Client("orchestrator.example.com:50051")

# Single rollout: context-manager style.
async with client.rollout(
    template=Template.SWE_BENCH,
    init={"instance_id": "django__django-12345"},
    deadline=Deadline(soft_s=540, hard_s=600),
    request_id=...,                          # optional; SDK auto-fills UUID4
    task_key="django__django-12345",         # optional; enables group/affinity
) as session:
    while not session.done:
        action = await policy.act(session.observation)
        await session.step(action)
trajectory = session.trajectory     # also: client.fetch_trajectory(id)
```

`request_id` makes start idempotent — a retry with the same id returns
the same rollout instead of double-allocating (control plane caches
for `idempotency_ttl_s`, default 300). `task_key` opts the rollout
into per-task fairness caps and group anti-affinity (see
`batch_rollout_group` below).

For long steps where the consumer wants the rollout kept alive without
sending a step, run a background heartbeat:

```python
async def keepalive(session):
    while not session.done:
        await asyncio.sleep(30)
        await session.heartbeat()
```

Default `idle_ttl_s` is 120 s. Each `step()` counts as a touch
implicitly; explicit heartbeat is only needed during long step
computations.

```python
# Batched: the workhorse for consumers.
result = await client.batch_rollout(
    template=Template.SWE_BENCH,
    inits=[...],            # list[dict], len == N
    policy=policy,          # any Awaitable[Action] = await policy.act(obs)
    hard_deadline=600,
    soft_deadline=540,
    concurrency=64,         # max in-flight at once
)

result.finished      # list[Trajectory]    rollouts that completed cleanly
result.truncated     # list[Trajectory]    hit hard deadline, partial trace
result.failed        # list[FailedRollout] sandbox/node errors with reason
```

### Group-aware primitives (algorithm-agnostic)

The SDK exposes the **primitives** an over-request / filter / cancel
loop is built from, without baking in any one engine's policy:

```python
# Annotate rollouts with task_key and group_id; the platform uses these
# for scheduling anti-affinity and per-task fairness caps. Both opaque
# to XRLEnv.
result = await client.batch_rollout(
    template=Template.SWE_BENCH,
    inits=[{"instance_id": "django__django-12345"}] * 24,
    policy=policy,
    task_keys=["django__django-12345"] * 24,    # opt-in fairness/affinity
    group_ids=["iter42-grp7"] * 24,             # opt-in group identity
    hard_deadline=600,
    concurrency=64,
)

# Cancel everything still in flight in a group.
report = await client.cancel_group("iter42-grp7", reason="have enough")

# Cancel a single rollout.
await client.cancel_rollout(rollout_id, reason="filter_dropped_group")
```

These are **primitives, not a policy**. Each consumer / adapter
composes them differently:

- **Slime** (spec 11) submits `over_sampling_batch_size` groups,
  awaits `FIRST_COMPLETED`, applies its `dynamic_sampling_filter`,
  and calls `cancel_group` when its own bookkeeping says the group
  is no longer needed. See [Slime's `generate_rollout_async`](https://github.com/THUDM/slime/blob/3cdec0cc82dd82f2b1a3e80164430c662d9354ea/slime/rollout/sglang_rollout.py)
  for the loop shape we mirror.
- **verl** (spec 12) builds its `DataProto` filter pass on the same
  primitives.
- **Custom consumers** (curriculum, MCTS branching, rejection
  sampling) compose the primitives directly.

XRLEnv deliberately does **not** ship a hard-coded
"`batch_rollout_group(target_group_size, overrequest_factor,
cancel_on_completion=True)`" helper, because every engine's
over-request / filter / cancel policy is different and forcing one
shape would warp the others. Spec 02 documents the rationale.

`batch_rollout` is the call shape both adapters wrap. It guarantees:

- All sandboxes are released (destroyed or returned to warm pool) before
  it returns. No leaks.
- `hard_deadline` is enforced per rollout; one straggler does not delay
  the others.
- Concurrency cap is honored end-to-end.
- Backpressure: when `concurrency` sessions are in flight, additional
  inits queue control-plane-side rather than overwhelming the network.

## Concurrency model

- `Client` is asyncio-native. Internally it multiplexes onto a single
  bidi-stream gRPC connection.
- Safe to call from inside a Ray actor (used by the verl adapter).
- A blocking-style `client.sync` wrapper exists for consumers that don't
  want to deal with asyncio (`client.sync.batch_rollout(...)`).

## File transfer helpers

For agent-installation patterns (terminal-bench's "drop a setup script
in, then exec it") and for moving large artifacts to/from a sandbox:

```python
# Small payloads — convenience wrappers over write_file / read_file.
await session.write("/tmp/setup.sh", b"#!/bin/bash\n...")
data: bytes = await session.read("/tmp/result.json")

# Large payloads (>16 MB) — streaming to/from a local path.
await session.upload(local_path="./agent_release.tar.gz",
                     sandbox_path="/opt/agent.tar.gz")
await session.download(sandbox_path="/var/log/run.log",
                       local_path="./run.log")

# Directories — auto-tar/untar.
await session.upload(local_path="./my_pkg/",
                     sandbox_path="/opt/my_pkg")  # archived on the wire
```

These ride on the backend's `write_file_stream` / `read_file_stream`
primitives (spec 01). Bounded memory regardless of payload size.

For the common "shared agent binary across many sandboxes" case,
prefer a host bind-mount declared in the template (`ResourceSpec.mounts`)
to repeated per-sandbox upload — see spec 06.

## Stateless invocation (`client.invoke`, phase 1)

For templates declared with `execution_mode: function-call`
(spec 01, spec 06), short stateless calls bypass the rollout
machinery entirely:

```python
result = await client.invoke(
    template=Template.MATH_EVAL,
    cmd=["python", "-c", "print(2 ** 64)"],
    stdin=None,
    timeout_s=5.0,
    request_id=...,                # optional; SDK auto-fills UUID4
    agent_originated=False,        # see "Trust level" below
)
result.stdout, result.stderr, result.exit_code
```

Routes to a free executor in the per-template pool on a node;
returns in ~1 ms dispatch latency rather than the ~200 ms a full
sandbox spin would take. No `RolloutSession`, no trajectory, no
deadline / heartbeat machinery — these calls are pure
request/response. Used inside consumer code or inside an EnvAdapter
that needs to run a quick subprocess on the user's behalf.

`client.invoke` does *not* tag with `task_key` / `group_id` (these
are per-rollout concepts); throughput-balancing across nodes is
done by simple round-robin among nodes whose pool isn't saturated.

### Trust level

Function-call mode trades isolation for speed. The template's
`trust_level` (spec 01, spec 19) determines whether
agent-generated commands are accepted:

- `trust_level: trusted-only` (default) — the platform refuses to
  dispatch invocations marked `agent_originated=True` to this
  pool. Only operator/EnvAdapter-originated calls run. The SDK
  raises `AgentInvocationDenied` when an agent-originated call
  hits a trusted-only pool.
- `trust_level: untrusted-allowed` — the manifest authors have
  acknowledged the weaker isolation; agent-originated calls are
  permitted.

Consumers and EnvAdapters that pass agent-emitted commands through
`client.invoke` set `agent_originated=True` so the platform can
enforce the policy.

## Sessioned rollouts (`client.start_session` / `client.attach`, phase 3)

For long-horizon rollouts where preemption resilience matters
(spot-VM training, multi-hour research-agent rollouts), the
sessioned API in spec 18 supersedes the basic `client.rollout`:

```python
session_token = await client.start_session(
    template=Template.DEEP_RESEARCH, init={...},
    snapshot_interval_s=60,
    derive_from={                        # deterministic; survives consumer preemption
        "experiment_id": "exp-2026-04-25-grpo-7b",
        "rollout_id":    42,
        "sample_index":  17,
    },
)

async with client.attach(session_token) as session:
    while not session.done:
        await session.step(await policy.act(session.observation))

# After preemption + new consumer process: the consumer recomputes
# the same session_token from its own checkpointed state and reattaches.
async with client.attach(session_token) as session:
    # Cached-result replay catches up automatically; new work
    # appends to the off-node log.
    ...
```

When `derive_from` is omitted, the SDK generates a random UUID; the
session won't survive consumer preemption. When provided, the token
is `sha256(canonical_json(derive_from))` — same fields → same
token → same session, no separate token-storage required.

See spec 18 for the full session lifecycle, the command-log shape,
the snapshot-anchored two-tier compaction, and the consumer-side
deterministic-token-derivation pattern. **Phase 3.**

## Image warmup (phase 1)

For workloads with a long tail of per-instance images (SWE-bench is
the obvious example), the consumer can announce upcoming work so the
cluster pre-fetches images before they're needed:

```python
await client.warmup(
    templates=["swebench-base"],
    instance_ids=upcoming_iter_instances,    # what next K iters will use
    horizon_iters=3,
    deadline_s=120,                          # ready in 2 min, please
)
```

The control plane fans this out as `ImageDirective`s to nodes biased
by where the scheduler expects to place the work, so each node only
pre-fetches what it's likely to run. Returns a future that resolves
with `WarmupReport(ready, missing, deadline_hit)` so the consumer can
decide whether to proceed or wait.

This is paired with the per-node Image Cache Manager (spec 15) which
handles eviction when total upcoming work exceeds node disk.

## Replay

```python
traj = await client.replay(rollout_id)
for step in traj.steps:
    print(step.action, step.obs, step.reward)
```

Replay reads the `TrajectoryLocator` from the rollout's row,
dispatches to the matching `TrajectoryReader` plugin, and returns
a normalized `Trajectory`. No sandbox involved — pure offline.
Use for debugging policies, eval scripts, ablations.

Subject to per-sink durability (spec 08 matrix): `platform-jsonl`
is the durable floor; native sinks (`slime-sample`,
`verl-dataproto`) succeed only while the consumer process / buffer
is reachable. `client.replay` raises `ReplayUnavailable` when the
locator's sink cannot be dereferenced (sink=`none`, native buffer
gone, or required reader plugin missing).

## Error model

### SDK exception hierarchy

```python
class XRLEnvError(Exception):
    """Base class for every error the SDK raises."""
    retryable: ClassVar[bool] = False
    category:  ClassVar[Literal["user", "infra", "workload"]] = "infra"

# user errors — the caller did something the platform refuses
class TemplateUnknown(XRLEnvError):           category = "user"
class RewardFnRequired(XRLEnvError):          category = "user"   # mode=consumer_final without reward_fn
class BackendCapabilityMissing(XRLEnvError):  category = "user"   # template needs cube on no-kvm node
class MountDenied(XRLEnvError):               category = "user"   # spec 19 mount allowlist
class AuthDenied(XRLEnvError):                category = "user"

# infra errors — platform-side issues; usually retryable
class CapacityExhausted(XRLEnvError):         retryable = True    # all nodes full or queue full
class ControlPlaneLost(XRLEnvError):          retryable = True    # phase-0 crash semantic
class NodeLost(XRLEnvError):                  retryable = True
class ImagePullFailed(XRLEnvError):           retryable = True
class AssetFetchFailed(XRLEnvError):          retryable = True

# workload errors — sandbox/template-level problems; not retryable by default
class RolloutTruncated(XRLEnvError):
    """hard deadline / step timeout; partial trajectory returned."""
    category = "workload"
    partial: Trajectory
class RolloutCancelled(XRLEnvError):
    """consumer-initiated cancel or group-cancel; partial trajectory returned."""
    category = "workload"
    partial: Trajectory
class RolloutFailed(XRLEnvError):
    """sandbox crash, init/setup/teardown failure, reward failure."""
    category = "workload"
    reason:  str           # see spec 02 reason table
    partial: Trajectory | None
class ReplayUnavailable(XRLEnvError):
    category = "workload"

# session-specific (phase 3)
class SessionExpired(XRLEnvError):            retryable = False
class SessionDegraded(XRLEnvError):           retryable = False
```

Consumers should treat `category=="infra"` errors as backoff-and-
retry candidates. `category=="user"` should fail loud (likely a
config problem). `category=="workload"` is a finished-with-bad-
outcome case — the consumer counts it, drops it, and moves on.

### Mapping to gRPC, metrics, adapters

| SDK exception | gRPC status | Metric label | Slime `status` (spec 11) | verl marker (spec 12) |
|---|---|---|---|---|
| `TemplateUnknown` | `INVALID_ARGUMENT` | `user_error` | n/a (caller-side) | n/a |
| `RewardFnRequired` | `INVALID_ARGUMENT` | `user_error` | n/a | n/a |
| `BackendCapabilityMissing` | `FAILED_PRECONDITION` | `user_error` | n/a | n/a |
| `MountDenied` | `PERMISSION_DENIED` | `user_error` | n/a | n/a |
| `AuthDenied` | `UNAUTHENTICATED` | `auth_denied` | n/a | n/a |
| `CapacityExhausted` | `RESOURCE_EXHAUSTED` | `capacity` | `failed` | `is_failed=True` |
| `ControlPlaneLost` | `UNAVAILABLE` | `infra` | `failed` | `is_failed=True` |
| `NodeLost` | `UNAVAILABLE` | `infra` | `failed` | `is_failed=True` |
| `ImagePullFailed` | `UNAVAILABLE` | `infra` | `failed` | `is_failed=True` |
| `AssetFetchFailed` | `UNAVAILABLE` | `infra` | `failed` | `is_failed=True` |
| `RolloutTruncated` | `DEADLINE_EXCEEDED` + partial in trailing metadata | `truncated` | `truncated` | `is_truncated=True` |
| `RolloutCancelled` | `CANCELLED` + partial in trailing metadata | `cancelled` | `aborted` | `cancelled_by_group_completion` |
| `RolloutFailed` | `ABORTED` + reason + partial in trailing metadata | `failed:<reason>` | `failed` | `is_failed=True` |
| `ReplayUnavailable` | `NOT_FOUND` | `replay_missing` | n/a | n/a |
| `SessionExpired` | `NOT_FOUND` | `session_missing` | n/a | n/a |
| `SessionDegraded` | (no error; metric-only unless `strict=True`) | `session_degraded` | n/a | n/a |

The metric label feeds
`xrlenv_rollouts_finished_total{template,status}` and
`xrlenv_admission_total{result}`; the log-line `error_kind` field
mirrors it.

Adapters convert the four "trajectory carrier" exceptions
(`Truncated`/`Cancelled`/`Failed` plus the success path) into
the framework's expected status shape — see spec 11/12.

#### gRPC wire shape — structured errors with metadata

The phase-0 wire (`xrlenv.rollout_control.v1.RolloutControl`) carries
SDK exceptions over gRPC's standard error path: server aborts with
the table's status code and attaches three trailing metadata keys:

- `xrlenv-error-kind` — class name (e.g. `RolloutTruncated`,
  `TemplateUnknown`). The Python SDK uses this to rehydrate the exact
  exception subclass; non-Python consumers can branch on it directly
  if the status-code mapping isn't fine-grained enough.
- `xrlenv-error-reason` — set when the exception carries a
  reason field (`RolloutFailed.reason` today; future
  reason-bearing exceptions use the same key).
- `xrlenv-error-partial-bin` — the partial `Trajectory` proto
  payload, base64-binary-safe per gRPC's `-bin` metadata convention.
  Set on `RolloutTruncated` / `RolloutCancelled` / `RolloutFailed`
  when the rollout produced any steps before sealing.

This shape supersedes earlier draft text suggesting carrier exceptions
should ride as "no error; payload in body". The structured-error-over-
gRPC model is what `xrlenv/control/rollout_endpoint.py` implements
and what `Client.grpc` consumes; non-Python consumers (Slime / verl
adapters in phase 1) read the same trailing metadata to reconstruct
their framework's expected status shape.

### Retry policy

Consumers call `client.rollout(...)` / `client.batch_rollout(...)`
with optional `retry=RetryPolicy(max_attempts=3, backoff_s=2.0)`.
The SDK retries only on `retryable=True` exceptions and only
during the start phase (queueing → ensure → create → setup);
once a step has run, retries become the consumer's responsibility
(retrying mid-rollout would silently re-execute side effects).

## Adapters

The framework adapters ship in **phase 1**, not phase 0. Phase 0
delivers only the consumer-agnostic SDK above; phase 1 layers the
adapters on top.

- `xrlenv.adapters.slime` — primary target (spec 11). Phase 1.
- `xrlenv.adapters.verl` — secondary target (spec 12). Phase 1.

For phase 0, the only consumer is
`examples/custom_asyncio_consumer.py` — the bare async loop suitable
for OpenRLHF / AReaL / one-off research scripts. The phase-0 SDK is
deliberately shaped (hard-deadline, partial-trajectory return, batch
helper) so the phase-1 adapters are a thin layer rather than a
redesign.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: surface above (`Client`, `rollout`, `batch_rollout`,
  `replay`, error model). No framework adapters yet; the bare-loop
  `examples/custom_asyncio_consumer.py` is the phase-0 consumer.
- **Phase 1**: Slime + verl adapters layer on top; streaming-trajectory
  variant (`async for step in session.steps()`) for consumers that want
  to consume in flight; basic client-side rate-limit awareness.
- **Phase 2**: `restore_from=SnapshotID` plumbed through; multi-agent
  rollout helpers; client-side trajectory cache for replay.
