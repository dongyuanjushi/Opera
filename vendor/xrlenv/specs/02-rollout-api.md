# 02 — Rollout API (gym/step layer)

## Purpose

Sit on top of the sandbox primitives (spec 01) and expose a higher-level,
RL-shaped API: rollouts, steps, deadlines, rewards, trajectories. This is
the *only* place RL semantics live. Everything below it is a generic
sandbox; everything above it (consumer SDK, adapters) consumes this layer.

## Concepts

```python
# Templates are dynamic — operators register them at runtime via
# `xrlenv template register` (spec 03 TemplateCatalog). The SDK
# accepts plain strings; the `Template` namespace in the example
# code below is just a convenience constants module the platform
# ships with the *built-in* templates pre-named, so calling code
# stays readable. New templates do not require an SDK release.
class Template:                          # str-valued constants; see spec 06
    TERMINAL_BASE  = "terminal-base"
    SWE_BENCH      = "swebench-base"
    OSWORLD        = "osworld-base"
    WEB_SEARCH     = "web-search-base"     # phase 1
    DEEP_RESEARCH  = "deep-research-base"  # phase 2
    # Operator-registered templates: pass the name as a literal string,
    # e.g. client.rollout(template="my-task", ...).

@dataclass
class Deadline:
    soft_s: float | None   # signal "you have N seconds left" to the policy
    hard_s: float          # kill the rollout, return truncated trajectory

    # Per-phase overrides; if None the template's defaults apply.
    # WHY: SWE-bench instance images vary 10x in pull time, init scripts
    # vary by repo size; one global timeout forces conservative defaults.
    # All of these run *outside* hard_s except where noted in the
    # "Per-rollout timeline" table below.
    queue_timeout_s:      float | None = None    # admission wait
    image_pull_timeout_s: float | None = None    # ensure images/assets
    create_timeout_s:     float | None = None    # backend.create
    init_timeout_s:       float | None = None    # template init.cmd
    setup_timeout_s:      float | None = None    # EnvAdapter.setup
    step_timeout_s:       float | None = None    # each step (counts against hard_s)
    reward_timeout_s:     float | None = None    # final reward (counts against hard_s)
    teardown_timeout_s:   float | None = None    # EnvAdapter.teardown

@dataclass
class StepResult:
    obs:    Any      # shape defined by the template's EnvAdapter (spec 14)
    reward: float
    done:   bool
    info:   dict     # tool-specific extras: tokens, screenshots, exit_codes...
    truncated: bool = False

@dataclass
class Trajectory:
    rollout_id:  str
    template:    str
    steps:       list[Step]    # action, obs, reward, ts, info per step
    status:      Literal["finished", "truncated", "cancelled", "failed"]
    reason:      str | None    # terminal reason; populated when status != "finished"
    final_reward: float
    metadata:    dict          # template params, deadlines, seeds, etc.
```

### Lifecycle states

A rollout passes through a fixed set of *transient* states before
landing in a terminal one. The transient states are observable in
the StateStore's `rollouts.status` column and on the SDK
`RolloutSession.state` property; consumers normally only see
`running` (between `start_rollout` and the first terminal status)
but the others surface in admin tooling and metrics.

```
                 ┌──────────────┐
   admit  ────►  │   queued     │ ──cancel──►  cancelled
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐
                 │   starting   │ ──init/setup fail──►  failed
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐
                 │   running    │ ──hard_deadline──►  truncated
                 └──┬───────┬───┘ ──cancel────────►  cancelling ──► cancelled
                    │       │
                    │       └─ step error ─►  failed
                    │
                    ▼
                 ┌──────────────┐
                 │  finishing   │ ── teardown ok ─►  finished
                 └──────────────┘
                        │
                        └─ teardown fail ──►  failed (reason=reward_failed | teardown_failed)

   any non-terminal state ──node_lost──►  failed
```

| State | Kind | Allowed predecessors |
|---|---|---|
| `queued` | transient | (entry — admission) |
| `starting` | transient | `queued` |
| `running` | transient | `starting`, `cancelling` (after a no-op cancel that races terminal) |
| `cancelling` | transient | `running`, `queued` |
| `finishing` | transient | `running` (after `done=True` or hard deadline) |
| `destroying` | transient (sandbox-side, parallel to `finished`/`failed`/...) | any terminal |
| `finished` | terminal | `finishing` |
| `truncated` | terminal | `running`, `finishing` |
| `cancelled` | terminal | `cancelling` |
| `failed` | terminal | any non-terminal |

`destroying` is a sandbox-side state, not a rollout-side state.
Capacity is released only when the sandbox transitions out of
`destroying`; the rollout terminal status can be observable
*before* its sandbox's destroy completes (matches the M3 SLO
split: time-to-mark-terminal vs per-sandbox destroy duration).

### Terminal status and reason

`status` is the four-valued enum above. `reason` is a string label
that disambiguates *why* a non-finished status occurred. Phase 0
populates these labels:

| status | typical `reason` values |
|---|---|
| `finished` | (null) |
| `truncated` | `hard_deadline`, `idle_ttl`, `step_timeout` |
| `cancelled` | `consumer_cancelled`, `group_cancelled` |
| `failed` | `node_lost`, `sandbox_crash`, `backend_error`, `init_failed`, `setup_failed`, `reward_failed` |

Trainer adapters (Slime, verl) map the four statuses to their own
markers — see specs 11 and 12. The metrics layer (spec 08) treats
`failed` as infra failure for `xrlenv_rollouts_finished_total`
counters; `truncated` and `cancelled` are not infra failures.

## Lifecycle

```python
async with client.rollout(
    template=Template.SWE_BENCH,
    init={"instance_id": "django__django-12345"},
    deadline=Deadline(soft_s=540, hard_s=600),
) as session:
    while not session.done:
        obs    = session.observation
        action = await policy.act(obs)
        result = await session.step(action)
    trajectory = session.trajectory
```

- `start_rollout` creates a sandbox via the scheduler, runs the template's
  `init` script, then calls the template's **EnvAdapter** (spec 14)
  `setup(init_params)` inside the sandbox, returns the initial `obs`.
- `step` ships the action to the in-sandbox stub, which forwards it to
  the loaded `EnvAdapter.step(action)`, computes `reward` (see "Reward
  sources" below), returns `StepResult`.
- `finish` (called automatically on context exit) calls
  `EnvAdapter.teardown()`, then destroys the sandbox and seals the
  trajectory.

The shapes of `Action` and `Observation` are defined entirely by the
template's EnvAdapter — XRLEnv core treats them as opaque payloads.
For OSWorld, an action might be `{type: "click", x: 100, y: 200}` and
an observation might be `{screenshot: <png_bytes>, a11y_tree: {...}}`;
for terminal-bench, an action is a shell-command string. See
[spec 14](14-env-adapters.md) for the full protocol and built-in
adapters.

## Canonical startup sequence

A rollout's startup is a fixed ordered pipeline. The consumer sees
one `start_rollout` call; underneath, the coordinator and node
agent run these phases in order, each with its own timeout, its
own failure status, and its own cleanup obligation:

| # | Phase | Owner | Timeout source | Failure → status / reason |
|---|---|---|---|---|
| 1 | Resolve template + instance | coordinator | (instant) | `failed` / `template_unknown` \| `instance_unresolved` |
| 2 | Ensure images / assets present on placed node | image cache mgr (spec 15) | `Deadline.image_pull_timeout_s` | `failed` / `image_pull_timeout` \| `asset_fetch_failed` |
| 3 | Reserve capacity and create sandbox | scheduler + node agent + backend (spec 01 `create`) | `Deadline.create_timeout_s` (default 60) | `failed` / `over_capacity` \| `sandbox_create_failed` |
| 4 | Start declared `services:` (topo-sorted, health-gated) | in-sandbox stub | `service.startup_timeout_s` per service | `failed` / `service_failed:<name>` |
| 5 | Run template `init.cmd` | in-sandbox stub | `Deadline.init_timeout_s` (default 120) | `failed` / `init_failed` \| `init_timeout` |
| 6 | Import EnvAdapter module/class | in-sandbox stub | (covered by next phase) | `failed` / `adapter_import_failed` |
| 7 | Call `EnvAdapter.setup(merged_params)` | in-sandbox stub | `Deadline.setup_timeout_s` (default 60) | `failed` / `setup_failed` \| `setup_timeout` |
| 8 | Return first observation | coordinator | (instant) | (none) |

`merged_params` passed to `setup` is the merge of (a) the
template's `env_adapter.init_params` (static), (b) the rollout's
per-call `init={...}` (dynamic), and (c) the resolver's per-
instance return when the template uses an `instances:` resolver
(spec 06). Conflicts resolve right-to-left: per-call > resolver >
manifest. The merged dict is what cached replay (spec 18) uses
as the canonical setup signature; templates that change the merge
order are a breaking change.

Cleanup obligations on failure: any phase that completed must be
unwound before the rollout is sealed `failed`. Concretely, if
phase 5 (init) fails, the node agent destroys the sandbox created
in phase 3; if phase 7 (setup) fails, the sandbox is destroyed
*and* the EnvAdapter's `teardown()` is **not** called (setup never
returned). Image pulls are not rolled back — they remain in the
cache for the next attempt. Capacity is released only after the
node confirms destroy.

### Per-rollout timeline (single canonical view of every deadline)

```
       │ queue        │ ensure │ create │ services │ init │ setup │  step×N  │ reward  │ teardown │ destroy
       │ (admission)  │ images │  +cap  │          │      │       │          │ (final) │          │
       ▼
       ├──────────────┴────────┴────────┴──────────┴──────┴───────┤          ├─────────┤          │
       └─ start_rollout() returns first obs here ───────────────────┘          │         │          │
       ├──────────────────────── soft_s (signaled in obs) ─────────────────────────────│          │
       ├──────────────────────────────── hard_s (truncates, kills sandbox) ─────────────────────│
                       ^queue_timeout_s         ^image_pull_timeout_s
                                                ^create_timeout_s     ^init_timeout_s
                                                                      ^setup_timeout_s
                                                       ^step_timeout_s (per step)
                                                                                ^reward_timeout_s
                                                                                          ^teardown_timeout_s
```

| Phase | Counts against `hard_s`? | Has its own timeout? |
|---|---|---|
| queue (admission) | no | `queue_timeout_s` (default 300) |
| ensure images | no | `image_pull_timeout_s` |
| create + capacity reservation | yes (rare; usually fast) | `create_timeout_s` |
| services + init + setup | yes | each phase's own timeout |
| each step | yes | `step_timeout_s` per step |
| reward (final modes) | yes | `reward_timeout_s` |
| teardown | yes (best effort) | `teardown_timeout_s` |
| destroy | no (after seal) | bounded by node-agent destroy SLO |

Queue and image-pull happen *before* the rollout's hard-deadline
clock starts; this matches Slime/verl expectations that "I asked
for a rollout, the env was 4 GB and the registry was slow" should
not consume the rollout's budget. Once the sandbox is created and
the first observation has been returned, `hard_s` runs from that
point.

## Deadline semantics

- **Soft deadline**: when reached, the next `obs` carries
  `info["time_left_s"]` so the policy can wrap up. Optional per-template
  flag.
- **Hard deadline**: the orchestrator kills the sandbox unilaterally.
  Any in-flight `step` raises `RolloutTruncated`. The trajectory is
  sealed with `status="truncated"` and returned. Two SLOs apply
  separately:
  - *Time to mark terminal*: ≤ 1 s after hard deadline expiry. The
    rollout's `Trajectory` is observable as `truncated` and the
    sandbox is enqueued for destroy by then.
  - *Per-sandbox destroy duration*: p50 ≤ 2 s, p95 ≤ 5 s. Capacity
    is not released to the scheduler until the node confirms
    destroy.
- The node agent destroys sandboxes through a bounded-concurrency
  executor (spec 04 default 8). When a single event enqueues many
  destroys (group cancel, drain, mass-truncation at iteration
  boundary), the per-sandbox duration SLO holds but the tail of the
  backlog is bounded by `ceil(N / max_concurrent_destroys) ×
  per_sandbox_p95`, not by a flat 5 s. Operators tuning batch size
  for tight iteration windows should size `max_concurrent_destroys`
  accordingly.
- **Why this matters**: Slime's RolloutManager dynamic-batches whatever
  finishes in time and drops stragglers; verl does similar dynamic
  batching. Our hard-deadline behavior must free resources fast enough
  that the next iteration doesn't starve.

## Reward contract

Reward is training data. Different templates need different reward
shapes (per-step shaped reward for shell envs, sparse final reward
for SWE-bench, judge-as-final for OSWorld), and conflating them
silently corrupts RL runs. The template manifest declares an
explicit `RewardContract`:

```yaml
reward:
  mode:    in_sandbox_final     # one of the modes below
  cmd:     ["pytest", "--exitcode-as-reward"]   # mode-specific
  timeout_s: 60
  on_error: fail_rollout        # fail_rollout | zero_reward | partial
```

`mode` values:

| `mode` | Computed by | Computed when | Per-step `reward` field |
|---|---|---|---|
| `env_step` | `EnvAdapter.step()` inside the sandbox | every step | populated each step |
| `in_sandbox_final` | template `reward.cmd` run inside the sandbox | once, at `done` (or hard deadline) | always 0.0 except final step |
| `consumer_final` | consumer-side callable supplied via SDK | once, in consumer process after `finish` | always 0.0; final reward written into trajectory at seal |
| `external_final` | orchestrator HTTPs the configured endpoint with the sealed trajectory | once, at seal | always 0.0; final reward returned by service |
| `token_level` | `EnvAdapter.step()` returns token-aligned reward array in `info` | every step | optional; `info["token_rewards"]` is the truth |

Exactly one mode applies per template. `Trajectory.final_reward` is
the **scalar contract** the trainer adapters consume — it is the
sum of `step.reward` for `env_step` / `token_level` modes, and the
returned scalar for the three `*_final` modes.

### Failure handling

Each mode has a timeout (`reward.timeout_s`, falls through to
`Deadline.reward_timeout_s`). On timeout or non-zero exit:

- `on_error: fail_rollout` (default) — trajectory sealed with
  `status="failed"`, `reason="reward_failed"`. Capacity / metrics
  treat as infra failure.
- `on_error: zero_reward` — trajectory sealed with the original
  status (`finished` / `truncated` / `cancelled`),
  `final_reward=0.0`, `metadata.reward_error="..."`.
- `on_error: partial` — only valid for `env_step` / `token_level`;
  use the partial sum collected so far. Sealed with the original
  status.

### Where the call lives

| Mode | Process |
|---|---|
| `env_step` / `token_level` | in-sandbox stub → `EnvAdapter.step` |
| `in_sandbox_final` | in-sandbox stub runs `reward.cmd` after `EnvAdapter.step` reports `done=True`; result returned to coordinator |
| `consumer_final` | SDK invokes the consumer-supplied `reward_fn(trajectory)` after `finish` returns; the SDK back-fills `Trajectory.final_reward` and the sealed jsonl |
| `external_final` | coordinator POSTs the sealed trajectory (sans large blobs) to `reward.url`; reply is a `{reward: float, info: {...}}` JSON |

Consumers do not need to know which mode is in use *unless* the
template is `consumer_final`, in which case the consumer must pass
`reward_fn=` to `client.rollout(...)` / `client.batch_rollout(...)`.
The SDK validates this at call time and raises
`RewardFnRequired(template, mode)` if missing.

### Phase ladder for reward modes

- **Phase 0**: `env_step`, `in_sandbox_final`, `consumer_final`.
- **Phase 1**: `external_final`, `token_level`.
- Built-in EnvAdapters declare which mode they natively support
  (spec 14); a manifest may not pair an `env_step`-only adapter
  with `mode: in_sandbox_final`.

## Replay & determinism

- We do **not** promise deterministic rollouts. Real environments
  (browsers, package mirrors, time-of-day) defeat that.
- We promise full action/obs replay **subject to the trajectory
  sink's durability matrix** (spec 08). Replay reads the
  `TrajectoryLocator` (spec 20) on the rollout row and dispatches
  to the right `TrajectoryReader` plugin (spec 17). Per-sink
  guarantees:
  - `platform-jsonl` — durable on node disk (spec 20 path
    `~/.xrlenv/runs/<date>/<rollout_id>/trajectory.jsonl`); the
    canonical replay floor and the default for CI.
  - `slime-sample` / `verl-dataproto` — best-effort; reachable
    only when the consumer's buffer is still alive. After consumer
    exit, replay returns `ReplayUnavailable` unless the operator
    configured a `multi:[platform-jsonl, native]` sink.
  - `none` — explicit opt-out; replay returns `ReplayUnavailable`.
- `client.replay(rollout_id)` returns a normalized `Trajectory`
  when the locator's sink supports it; otherwise raises
  `ReplayUnavailable`. Phase 0.

## Idempotency, heartbeat, and idle reaping

Three lifecycle hygiene mechanisms that close gaps a naive `start →
step → finish` loop leaves open.

### Idempotency at every layer

`request_id` is the consumer-facing knob, but every internal
operation that can be retried under a transient failure has a
deterministic idempotency key. Spec 21 §"Idempotency" lists the
key derivation per command. The bus is:

| Operation | Caller-supplied key | Internal key |
|---|---|---|
| Rollout start | `request_id` (default UUID4) | n/a |
| Sandbox create on a node | (derived from rollout) | `rollout_id` |
| Sandbox destroy | n/a | `sandbox_id + ":destroy"` |
| Image / asset pull | n/a | sha256 of directive content |
| Trajectory fetch (admin / viewer) | (none; viewer fans out) | `rollout_id + ":" + range_hash` |
| Function-call invoke | `InvokeRequest.request_id` (optional) | same; SDK auto-fills UUID |
| Session attach | `session_token` (deterministic via `derive_from`) | n/a |

Retries that arrive at the same node with the same key return the
cached reply (per spec 21's per-node LRU). Retries that race a
control-plane crash and land on a fresh control-plane process
hit the StateStore's `idempotency` table (spec 20) for the
cluster-wide answer. Both layers cooperate so the consumer can
retry transient infra errors without ever creating a duplicate
sandbox.

### Client-supplied `request_id` for idempotent start

`client.rollout(...)` and `client.batch_rollout(...)` accept a
`request_id: str` (the SDK auto-generates a UUID4 if you don't pass
one). The control plane caches `(request_id → rollout_id)` for
`idempotency_ttl_s` (default 300). A retry with the same
`request_id` returns the existing rollout, never a new sandbox.

WHY: at thousands of concurrent rollout starts per training
iteration, retry storms (TCP RST, gRPC connection reset, transient
429s) are normal. Without idempotency we leak sandboxes and corrupt
batch counts. Mirrors the
[`POST /allocate(request_id=...)`](https://github.com/Gen-Verse/OpenClaw-RL/blob/main/terminal-rl/remote/pool_server.py)
pattern in pool_server.

### Consumer-side heartbeat

Once a rollout starts, the consumer must `touch` its session at least
every `idle_ttl_s` (default 120) or the coordinator reaps it.
`session.step(...)` counts as a touch implicitly; for long-running
steps the consumer should call `await session.heartbeat()` from a
background task.

WHY: a gRPC stream drop catches process death and network partition
and triggers cancellation (spec 03 "Hung rollout"). It does *not*
catch **application-level hangs** — consumer process alive, stream
open, Python deadlocked. Without idle TTL the sandbox sits until the
hard TTL (1 h), wasting the slot. Idle TTL is more aggressive and
catches abandoned rollouts within the heartbeat window.

### Idle TTL vs hard TTL

Two thresholds, two purposes:

| | Idle TTL (`idle_ttl_s`) | Hard TTL (`ttl_s`) |
|---|---|---|
| Default | 120 s | 3600 s |
| Triggered by | client failed to heartbeat / step | wall-clock since rollout start |
| Catches | abandoned rollouts (client-side hang) | runaway rollouts (server-side hang) |

Both are independent reapers; whichever fires first wins. Both can be
overridden per-rollout in the `Deadline`.

## Group / batch rollouts: primitives, not policy

Algorithms like GRPO (Group Relative Policy Optimization), Slime's
`generate_rollout_async`, and verl's filter pipelines all share the
same shape — **sample N rollouts per prompt, possibly over-request,
possibly filter and cancel** — but they differ substantially in *how*
they decide when to over-request, when to filter, and when to cancel.

A few examples to make the variation concrete:

- **Slime** ([sglang_rollout.py](https://github.com/THUDM/slime/blob/3cdec0cc82dd82f2b1a3e80164430c662d9354ea/slime/rollout/sglang_rollout.py))
  drives its own loop: submits `over_sampling_batch_size` groups,
  awaits `FIRST_COMPLETED`, applies a user-supplied
  `dynamic_sampling_filter` (e.g. drop groups with all-zero variance),
  continues until `rollout_batch_size` is satisfied, then `abort()`s
  the rest. There's also `partial_rollout` plumbing that recycles
  aborted samples into the next iteration's buffer.
- **verl** uses `DataProto` masks plus its own filter pass.
- **A custom consumer** might implement curriculum, rejection sampling,
  MCTS branching, or an entirely different overdraft strategy.

XRLEnv must not bake any specific over-request / filter / cancel
*policy* into its core. Different engines have different patterns,
and locking one in would force every other engine into an unnatural
shape. Instead, XRLEnv provides **primitives** the consumer composes:

### Primitive 1: `task_key` and `group_id` as opt-in annotation

Rollout requests can carry:

- **`task_key`** — identifies the prompt / problem (e.g.
  `"django__django-12345"`, a math-problem hash, a request UUID for
  a unique prompt).
- **`group_id`** — binds rollouts that share a *training iteration's
  group identity* together so the platform can treat them as a unit
  for scheduling and cancellation.

Both are opaque strings to XRLEnv. The platform never inspects them;
it uses them only as keys for the affinity and fairness logic below.
Consumers without group semantics simply leave them unset.

### Primitive 2: cancellation

Two control-plane RPCs:

- `cancel_rollout(rollout_id, reason: str)` — terminate one rollout,
  hard-deadline it, return its partial trajectory marked
  `status=cancelled`.
- `cancel_group(group_id, reason: str) → CancelGroupReport` —
  terminate every still-running rollout in the group; idempotent.

The consumer calls these when *the consumer* has decided. XRLEnv does
not auto-cancel based on a group count threshold or a filter result;
that's policy. The platform's job is to execute the cancel quickly
and free the sandbox within the standard 5 s teardown budget.

### Primitive 3: group anti-affinity at scheduling

When a rollout request carries `group_id`, the scheduler prefers
nodes that don't yet host a sibling. WHY: a single slow or saturated
node should not disproportionately delay the whole group. This is a
*hint*, overridden by capacity and per-task cap when needed; never a
hard constraint.

### Primitive 4: per-node per-task fairness cap

Each node has `max_runs_per_task` (default 4, configurable). Honors:

- One task can't monopolize one node, even when over-request sends
  many sibling rollouts at once.
- Group anti-affinity has somewhere to land (the cap pushes a 5th
  sibling to a different node automatically).

Independent of per-template resource capacity (spec 10): per-template
cap is resource-driven ("how many of any swebench-base fit on this
node"); per-task cap is algorithm-driven ("how many of *this specific
prompt* on this node"). Both must hold.

### What XRLEnv deliberately does NOT provide

- No `target_group_size` parameter on the core API. The consumer
  knows the target; the platform doesn't need to.
- No `overrequest_factor`. Engines have wildly different policies
  (Slime uses `over_sampling_batch_size` and a filter; verl uses
  masks; curriculum consumers may not over-request at all).
- No `cancel_on_completion=True` default. Cancellation is the
  consumer's call, made explicit by `cancel_group`.
- No filter/keep logic. `dynamic_sampling_filter` lives in the
  consumer / adapter, not in XRLEnv.

### Where the engine-specific loops live

Each trainer adapter implements its engine's pattern on top of these
primitives:

- **Slime adapter (spec 11)** mirrors `generate_rollout_async`: keeps
  submitting `over_sampling_batch_size` until the consumer's
  `dynamic_sampling_filter` has accepted `rollout_batch_size` groups,
  then calls `cancel_group(...)` for any group whose remaining
  in-flight members are no longer needed. Partial-rollout salvage
  uses XRLEnv's trajectory replay (spec 02 replay) on the
  aborted-but-partial trajectories.
- **verl adapter (spec 12)** mirrors verl's `DataProto` filter pass:
  same primitives, different glue.
- **Custom consumers** compose `batch_rollout(...)` (spec 05) with
  `cancel_rollout` / `cancel_group` themselves; no specific helper is
  required.

## Snapshot & branch (phase 2)

- `session.snapshot() → SnapshotID` captures sandbox state mid-rollout.
- `client.rollout(template, restore_from=SnapshotID)` starts a new
  rollout from that state.
- Useful for MCTS-style search, off-policy correction, ablating from
  a known prefix.
- Backend support: real on Cube/Firecracker, no-op on Docker (raises
  `BackendCapabilityMissing`). Templates can declare
  `snapshot_required: true` (spec 06 manifest field) to refuse
  Docker placement.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: gym/step lifecycle, deadline (soft + hard +
  per-phase overrides), reward modes `env_step` + `in_sandbox_final` + `consumer_final`,
  replay, **idempotent start (`request_id`)**, **consumer-side
  heartbeat + idle TTL**, **group / batch rollout primitives
  (`task_key`, `group_id`, `cancel_rollout` / `cancel_group`, group
  anti-affinity, per-node per-task fairness cap `max_runs_per_task`)
  — engine-specific over-request / filter loops live in the trainer
  adapters**.
- **Phase 1**: external reward service, partial-trajectory truncation
  signals exposed to policy, soft deadline standardized in obs.
- **Phase 2**: snapshot/branch first-class, multi-agent rollouts (one
  rollout, multiple coordinated sandboxes).
