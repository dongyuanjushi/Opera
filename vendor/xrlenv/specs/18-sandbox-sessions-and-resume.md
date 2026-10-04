# 18 — Sandbox Sessions and Preemption-Safe Resume

## Purpose

Today, a rollout's lifetime equals its sandbox's lifetime. If the
trainer disconnects abnormally — process crash, OOM, spot-VM
eviction, network partition — the sandbox is destroyed and the
in-flight rollout is lost. Hours of work on a long-horizon agent
rollout vaporize for a 5-minute spot eviction.

This spec introduces:

1. **Sandbox sessions** that outlive a single trainer process,
   pinned by an opaque `session_token`.
2. A **command log** persisted off-node so the work record survives
   even total node loss.
3. **Cached-result replay** so a resuming trainer fast-forwards
   through the work it had already done, without re-executing.

This is the prerequisite capability for running long-horizon /
research-agent training on commodity / spot infrastructure.

This spec borrows the pattern from DSec's "Trajectory Logging and
Preemption-Safe Resumption" (DeepSeek), adapted to fit our existing
sandbox-backend / EnvAdapter / coordinator architecture.

## Concepts

- **SandboxSession** — a sandbox plus a persistent command log,
  identified by a `session_token`. The session can be **detached**
  (no live trainer connection) and **reattached** by a trainer
  presenting the token. A session can outlive any number of trainer
  processes.
- **CommandLog** — a globally-ordered, persisted-off-node record of
  every `EnvAdapter.step(action)` against the session, plus the
  result `(obs, reward, done, info)`. The log is **step-level** —
  one entry per step is the replay primitive. Below the step
  boundary the EnvAdapter implements its abstraction by calling
  `exec` / `read_file` / `write_file` / `spawn_service`; those
  calls may be recorded as **optional sub-traces under the parent
  step entry** for debugging and the trajectory viewer, but the
  replay protocol never consults them. See "Command log: schema
  and persistence" below for the on-disk shape and the
  hot-tier / cold-tier split (specs 20, 18).
- **Resume** — when a trainer reattaches with a `session_token`,
  the coordinator restores the sandbox (from a snapshot if the live
  one is gone), opens the command log, and serves cached results
  for every subsequent call that matches the next-expected log
  entry. New calls (past the log's tail) execute normally and are
  appended.
- **Divergence** — if a resuming trainer issues an action that
  *doesn't* match the next-expected log entry, the session marks
  divergence: any cached entries past that point are invalidated
  and the session continues from the divergence point with fresh
  execution against the restored sandbox.

## Lifecycle

```
trainer starts                         trainer disconnects (preemption)            trainer resumes
   │                                       │                                            │
   ▼                                       ▼                                            ▼
  start_session(template) ──► session_token              ◄── attach(session_token) ──┐
       │                                       (sandbox detached;                    │
       ▼                                        snapshot taken on a schedule;        │
   ┌───── normal rollout ─────┐                 command log persisted)               │
   │  step #1 → log #1        │                                                      │
   │  step #2 → log #2        │                                                      │
   │  step #3 → log #3        │                                                      │
   │     ...                  │                                              ◄───────┘
   │  step #50 → log #50      │                              ┌─── resumed rollout ──────┐
   └──────────────────────────┘                              │ step #1 → MATCH log #1 → │
                                                              │   return cached result   │
                                                              │ step #2 → MATCH log #2 → │
                                                              │   return cached         │
                                                              │   (...catches up to 50)  │
                                                              │ step #51 → execute fresh │
                                                              │           append log #51 │
                                                              └──────────────────────────┘
```

Two paths the coordinator's restore takes:

- **Sandbox still alive** (trainer disconnected, node didn't):
  reattach to the existing sandbox; the in-sandbox stub still has
  state. Fast-forward the trainer's repeated calls against the log.
- **Sandbox lost** (node evicted, restarted, network gone): create a
  fresh sandbox from the most recent snapshot whose seq is at or
  near the log tail (snapshot cadence keeps that gap small — see
  "Snapshot cadence as the actual lever" below). The few entries
  between the snapshot seq and the tail are served back to the
  trainer as cached `(obs, reward, done)` without invoking the
  EnvAdapter, advancing the trainer's replay cursor; the EnvAdapter
  is *not* re-invoked and the sandbox state is not advanced through
  those entries.

  Correctness condition: the snapshot must be close enough to the
  log tail that fresh actions issued past the tail still execute
  against a correct sandbox state. The session contract guarantees
  this by requiring a backend with cheap chainable snapshots and
  per-step (or near-per-step) snapshot cadence. Templates relax
  cadence at their own risk; see "Sandbox state correctness gap"
  below for the formal statement.

  On reach of the log's tail, the trainer's new calls flow through
  normally and append to the log.

## API

```python
# Trainer side

session_token = await client.start_session(
    template=Template.SWE_BENCH,
    init={"instance_id": "..."},
    deadline=Deadline(...),
    # Two independent knobs (sooner of the two fires); defaults
    # satisfy max_resume_gap = 1 (per-step) on Cube. Override only
    # for benign hermetic templates that have opted into relaxed
    # cadence.
    snapshot_every_k_steps=1,                   # default 1 (per-step)
    snapshot_interval_s=30,                     # default 30s wall clock
    log_persistence="off-node",                 # off-node | local-only
    durability_mode="node-loss-resumable",       # see "Durability modes" below
)

# A session_token is a UUID. Persist it trainer-side.

async with client.attach(session_token) as session:
    while not session.done:
        action = await policy.act(session.observation)
        await session.step(action)
trajectory = session.trajectory

# Later, after preemption + resume on a fresh trainer:
async with client.attach(session_token) as session:
    # session.observation is the post-resume state.
    # session.replayed_steps lists steps already in the log.
    # The first session.step(action) calls match-and-cache against
    # the log; only past the log tail does new work happen.
    ...

# Explicitly end a session (frees sandbox, retains log):
await client.end_session(session_token, retain_log=True)
```

```proto
service Orchestrator {
  rpc StartSession(StartSessionRequest) returns (StartSessionReply);
  rpc Attach(stream AttachClientMsg)    returns (stream AttachServerMsg);
  rpc EndSession(EndSessionRequest)     returns (Empty);
  rpc ListSessions(SessionFilter)       returns (SessionList);
}
```

`Attach` is the bidi stream that supersedes `Rollout` for sessioned
work. The first message carries the `session_token`; the coordinator
loads the session state and starts streaming.

## Command log: schema and persistence

The log is **step-level**: one top-level entry per
`EnvAdapter.step(action)` call, which is the only boundary the
agent observes. Below that boundary the EnvAdapter implements its
abstraction by calling sandbox primitives (`exec`, `read_file`,
`write_file`, `spawn_service`); those calls are **observability
detail nested under the parent step**, never used as a replay
primitive.

Why one boundary and not two:

- The agent only ever sees `step → (obs, reward, done)`. Replay's
  job is to reproduce *what the agent observed* — that is exactly
  the step boundary.
- Cached step-replay returns `(obs, reward, done)` directly without
  re-invoking the EnvAdapter. The EnvAdapter's internal ops don't
  re-run, so their idempotency / determinism doesn't matter on
  replay.
- The op sub-trace is recorded by the stub's request middleware
  (one of the phase-0/1/2 design hooks above) for the trajectory
  viewer (spec 17) and for debugging EnvAdapter bugs. It costs
  almost nothing when steps are small; it never gates correctness.

Schema:

```json
{
  "seq":         42,
  "ts":          1714000000.123,
  "rollout_id":  "...",
  "action":      { /* whatever EnvAdapter.step accepts */ },
  "result":      { "obs": ..., "reward": 0.0, "done": false, "info": {...} },
  "duration_s":  0.42,
  "ops":         [                         // optional sub-trace; debug only
    { "kind": "exec",       "cmd": "ls /tmp",       "exit": 0,    "duration_s": 0.003 },
    { "kind": "read_file",  "path": "/result.json", "size": 412,  "duration_s": 0.001 },
    { "kind": "write_file", "path": "/tmp/x",       "size": 28,   "duration_s": 0.001 }
  ]
}
```

`action` and `result` are the replay primitives. `ops` is purely
observability — the trajectory viewer renders it on a "raw
commands" tab; the replay protocol ignores it entirely.

(There is intentionally **no `idempotent` field** at the step
level. Earlier drafts carried one for an op-level replay model
that this design abandoned in favor of step-level cached replay
plus snapshot cadence. Leaving the field in the schema would
invite an implementation to recreate the op-level model.)

Persistence (canonical tiers in spec 20):

- **`local-only`** (legacy / short-rollout opt-out): log lives in
  the per-rollout directory on the node
  (`~/.xrlenv/runs/<id>/log.jsonl` — the run-dir layout in spec
  20). Lost if the node is lost. Cheap; appropriate only when the
  workload is not preemption-sensitive and the operator has
  consciously accepted node-loss risk.
- **`off-node`** (default): each entry is appended synchronously
  to the **hot tier** — the `command_log_hot` row set in
  StateStore (spec 20; redis-backed by phase 3, the substrate
  sessions ship on). The hot log bytes themselves survive node
  loss. *Resume after node loss* additionally requires a durable
  snapshot chain — see the `durability_mode` table below; the log
  alone is not enough. Older entries roll into the **cold tier**
  in object storage on the snapshot-anchored schedule documented
  under "Two-tier storage" below.

Snapshots are taken in step with the log: every
`snapshot_every_k_steps` steps or every `snapshot_interval_s`
seconds (whichever fires first; see "Snapshot cadence as the
actual lever" for the canonical knob names), the coordinator
sends a `SnapshotCommand` (spec 21 phase-3 message) to the owning
node, which calls `backend.snapshot(sb)` and replies with the
resulting `SnapshotID` plus the log's current tail seq number.
The mapping `(seq → SnapshotID)` is persisted in the StateStore's
**`session_snapshots`** table (spec 20 — one row per snapshot
taken); the `sessions` row caches the `latest_durable_snapshot_*`
fields for O(1) lookup of the most recent durable snapshot for
resume. On resume from a lost node, recovery reads
`session_snapshots` to find the latest snapshot ≤ the requested
log position, restores from it, then catches up the trainer's
replay cursor against the hot-tier log entries (no re-execution;
see "Sandbox state correctness gap" above).

## Cached-result replay: matching and divergence

On reattach, the coordinator opens the log and starts a "replay
cursor" at seq 0 (or at the snapshot point, when restoring). For
each new call from the trainer:

1. Read the next log entry under the cursor.
2. Compare the new call's `kind` + `request` to the entry.
3. **Match** → return the entry's `result` to the trainer
   immediately. Do not execute against the sandbox. Advance cursor.
4. **Mismatch** (divergence) → invalidate every entry past the
   cursor; execute the call for real against the sandbox; append a
   fresh log entry at the cursor's position; cursor advances.

When the cursor reaches the log tail, the session is fully caught
up — every subsequent call executes for real and appends to the log.

### Why this design doesn't need per-call idempotency markers

In an op-level-replay design, idempotency markers gate whether
each internal op (`exec`, `write_file`, `curl -X POST ...`) is
re-executed against the restored sandbox or served from cache.
That brings real complexity: per-op marking, divergence detection
at the op level, "what counts as idempotent" judgement calls.

Step-level cached replay sidesteps the entire question for the
catch-up phase: the EnvAdapter is **not re-invoked** during cached
replay; no internal ops re-execute; idempotency is moot.

The only remaining correctness concern is **new actions past the
log tail running against a sandbox state older than the log tail**
(if we restored from an older snapshot during cold restart). The
clean answer is to keep the gap ~0 via **frequent cheap
snapshots**, not via op-level re-execution.

### Snapshot cadence as the actual lever

Sessions take snapshots aggressively while attached. Two
independent knobs gate cadence; the sooner of the two fires:

- **`snapshot_every_k_steps`** — snapshot after every K
  EnvAdapter steps. Default `K = 1` (per-step snapshot, the
  cadence required to satisfy `max_resume_gap = 1`).
- **`snapshot_interval_s`** — snapshot at most every N seconds
  of wall time, even if no step has fired (covers long-thinking
  rollouts where one step takes minutes). Default 30 s.

With chainable overlaybd (Cube; spec 01) per-step snapshots are
millisecond-scale and cheap. The two knobs and `max_resume_gap`
must satisfy: `max_resume_gap >= snapshot_every_k_steps`. The
control plane's TemplateCatalog (spec 03) rejects a template
that violates this at register; the SDK does not need to validate
manifests on the trainer side.
- The seq number of the snapshot is recorded against the log so
  cold restart can find the latest snapshot ≤ the requested log
  position.
- On cold restart: restore from the latest snapshot, which is at or
  near the log tail. Cached replay catches up the few remaining
  entries (returning cached `(obs, reward, done)` without invoking
  the EnvAdapter). When the cursor reaches the tail, the sandbox
  state is fresh enough that new actions run against the right
  state.

### Sandbox state correctness gap (the formal contract)

Cached step-replay advances the trainer's replay *cursor* but does
not advance the sandbox *state*: when the cursor matches a log
entry, we return its `(obs, reward, done)` directly; the
EnvAdapter is not re-invoked and no in-sandbox side effects occur.
After cold restore from snapshot at seq `S`, the sandbox state is
"the world as of step `S`," even after we have served cached
results up to seq `T > S`.

This is correct **only when the trainer's first fresh action past
the log tail can be executed against a sandbox state at seq `S`
without observable harm**. The guarantee the session contract
makes:

- The latest durable snapshot's seq `S` is within `max_resume_gap`
  of the log tail seq `T`. Phase-3 default: `max_resume_gap = 1`
  (per-step snapshot). Templates may raise this knowingly when the
  gap is provably benign for their EnvAdapter (e.g. a hermetic
  pure-evaluator where steps `[S, T)` did not mutate disk).
- The session marks itself `degraded` and surfaces a metric when
  `T - S > max_resume_gap`. Trainers can choose to abandon a
  degraded resume rather than continue.

Backends that cannot deliver the cadence required to keep `T - S
≤ max_resume_gap` are not eligible for sessions; this is enforced
by the capability flags `supports_chainable_snapshot` and
`live_state_captured` (spec 01). The next section explains why
that excludes Docker.

### Sessions are Cube-only in this design

`docker commit`-based snapshots (spec 01's Docker backend) are
seconds-scale and don't capture live process state. Snapshotting
every step against Docker is impractical; falling back to op-level
re-execution to bridge a multi-step gap drags the whole
idempotency-marker machinery back in.

Cleaner design choice: **sessions require a backend with
`supports_chainable_snapshot=True` and `live_state_captured=True`**
— in practice, Cube. Docker templates use the existing
rollout-tied sandbox lifecycle (rollout finish = sandbox destroy).
Trainers that care about preemption resilience opt into a Cube
template.

A Docker session-like fallback (snapshot at session end only;
catastrophic-loss = unrecoverable) is a phase-3+ option if there's
real demand; not worth the complexity until then.

## Replay suitability — what we promise, what we don't

The log records **what the agent observed**. Cached replay returns
those observations faithfully — that part is deterministic. But
replay says nothing about **how the world will respond to a fresh
action issued past the log tail**. The world has been moving the
whole time the trainer was disconnected. Sessions can resume; the
*environment outside the sandbox* generally cannot.

Concrete examples of what *cannot* be reliably resumed:

- **Dynamic web pages**: a CSS selector or pixel coordinate that
  located a button at seq 42 may not locate it at seq 43 — the
  page reflowed, the layout split-tested, the language switched,
  the button is gone.
- **Time / date sensitive actions**: `date`, `now`, "today's
  weather," any query whose result depends on wall-clock time will
  produce different outputs on resume.
- **External APIs with mutable state**: rate limits reset, account
  balances change, search-result ordering shifts, A/B test arms
  flip, CAPTCHAs appear or expire.
- **Stochastic upstream services**: LLM-as-tool with non-zero
  temperature, randomized recommendations, anything seeded by
  upstream entropy we don't control.

None of these break **cached replay during catch-up** — we serve
the originally-recorded observation no matter how the world has
moved. They break **the agent's expectations going forward**: the
trainer's seq 43 action was chosen against an *observation from
the past*, but it executes against the world's *current* state.

This is not a bug in the session design. It is a property of *any*
agentic workflow against a non-stationary environment, including
ones that never preempt — pause an OSWorld rollout for 30 minutes
between steps and the same drift appears. Sessions just make
pauses cheaper and therefore drift more visible.

### Template classes by replay suitability

A spectrum, not a binary:

| Class | Examples | Resume behavior |
|---|---|---|
| **Hermetic** — environment fully captured by sandbox state | Pure code execution against a pinned snapshot (terminal-bench-2 with a frozen `task.toml`, SWE-bench against a pinned commit, classical RL gyms, math evaluators) | Resume is faithful. Snapshot + cached replay reproduces both observation history *and* the world's response to new actions. The session design works exactly as advertised. |
| **Stationary** — environment changes slowly relative to a session's wall time | Code-execution tasks against the live filesystem the agent built (SWE-bench mid-rollout, a Python kernel session), local databases, documented APIs that don't churn | Resume usually works. Catch-up is exact; new-action drift is small enough that most rollouts complete successfully on resume. Some failures expected. |
| **Non-stationary / live** — environment changes on its own faster than the session's resume window | OSWorld with live internet, web-browsing agents, agents calling real third-party APIs, time/date-sensitive tasks | Resume preserves the observation history but does **not** preserve the agent's ability to continue the rollout meaningfully. Sessions are still useful for forensics (what happened?) but not for *resuming work in progress*. |

The template author knows which class their template is in; we
recommend they document it (a one-line note in `template.yaml`'s
README).

### Honest API behavior for the non-stationary case

For templates in the **live** class, the SDK still allows
`start_session` / `attach`, but two things are documented as the
trainer's responsibility:

- **Decide whether resume is worth attempting.** A trainer that
  knows it's running an OSWorld web-browsing rollout may choose to
  abandon a 4-hour-old session and start fresh rather than resume
  into a drifted world.
- **Detect divergence post-tail.** When new actions begin failing
  in implausible ways (selector not found, click hits empty
  space, error rates spike), the trainer should treat the session
  as failed and abandon it. The platform doesn't auto-detect this
  — the agent's observations are themselves the signal.

For templates in the **hermetic** or **stationary** class, the
session design works as intended and is the right tool.

### What the log is always good for, regardless of class

Even for non-stationary templates, the off-node command log is
valuable for purposes that don't require resuming new work:

- **Provenance**: forensic answer to "what did this rollout
  observe and decide?", surviving total node loss.
- **Trajectory viewer** (spec 17): rendered as a normal trajectory
  for debugging, post-mortems, and offline analysis.
- **Off-policy training data**: the recorded `(action, obs,
  reward)` tuples are valid training data even if the agent can't
  resume the workflow.

So: sessions are a **resume mechanism that happens to work in
practice for hermetic and stationary templates**, plus a
**universally-valuable persistence + provenance mechanism** that
applies to every template. The two values are separable; templates
unsuitable for resume can still benefit from the persistence side.

## Sandbox lifetime when detached

Two axes drive the choice: **task state size** (how expensive is
restore?) and **detach duration** (how long are we paying RAM for
the warm sandbox?). Three configurable modes cover the space, plus
a reactive eviction safety valve that any of them respects.

### The three modes

| Mode | Behavior | When to use |
|---|---|---|
| `keep-warm` | Live sandbox retained until explicit `end_session`. Instant reattach. | Long-running tasks where reattach latency dominates and node RAM is plentiful. Operator commits to the cost. |
| `snapshot-on-detach` | Snapshot and destroy immediately on detach. RAM reclaimed; reattach pays restore cost. | Short tasks where state is small (restore is fast) or capacity-tight clusters where every GB matters. Forensic-mostly use cases (the log is the value, not the resume). |
| `keep-warm-with-decay` *(default)* | Keep warm for `keep_warm_s` (default 600 s = 10 min) after detach, then snapshot and destroy. | The typical case: instant resume for short blips (network glitch, fast trainer restart); RAM reclamation when a detach turns into a long pause. |

`keep-warm-with-decay` is the default because most preemptions are
short-lived (network reset, container restart, fast trainer
re-init); long detaches gracefully shed without operator
intervention.

### Periodic snapshots, regardless of mode

While a sandbox is warm, the platform still takes periodic
snapshots (every K steps or every N seconds; the snapshot-cadence
design from above). This is the durability backstop: if the warm
sandbox's node dies, the session can still resume from the latest
snapshot. **"Keep warm" never means "no snapshots."** The session
is durable against node loss in all three modes; it differs only
in *whether the live sandbox is also retained*.

### Reactive eviction safety valve

When a node hits capacity pressure (memory or CPU), **detached-
but-warm sandboxes are evicted first**. They have a recent
snapshot; resumption from snapshot is correct, just slower. Order:

1. Detached sandboxes past their `keep_warm_s` window → snapshot,
   destroy.
2. Detached sandboxes within their window → snapshot, destroy
   *anyway* if pressure persists.
3. Attached sandboxes (live trainer connection) are never
   reactively evicted; the scheduler refuses new placements first.

The capacity estimator (spec 10) accounts for detached-but-warm
sandboxes as real resource consumers, so reactive eviction kicks in
automatically when packing requires it. Operator override:
`xrlenv sessions evict <session_id>` forces the destroy + snapshot
on a specific session.

### Per-template defaults; per-call override

Templates declare their typical session profile in
`template.yaml`:

```yaml
session:
  default_lifetime_mode: keep-warm-with-decay   # | keep-warm | snapshot-on-detach
  keep_warm_s: 600                               # 10 min default
  snapshot_interval_s: 30
```

Hermetic short-running templates (terminal-bench-2 instance,
5-minute SWE-bench) typically default to `snapshot-on-detach` —
state is small; restore is fast; warm RAM isn't worth it.
Long-running research-agent templates default to
`keep-warm-with-decay` with a longer `keep_warm_s` (e.g. 30 min).

The trainer overrides per-call:

```python
session_token = await client.start_session(
    template=Template.DEEP_RESEARCH, init={...},
    lifetime_mode="keep-warm",     # I really care about reattach latency
    keep_warm_s=3600,
)
```

## Log size bounding and compaction

Long sessions (multi-day research agents, 24h+ rollouts) generate
sizeable logs — tens to hundreds of MB easily, GB-scale at the
extreme. Holding all of that in hot storage indefinitely is
wasteful; compaction is required.

The compaction unit is **the snapshot**, because step-level cached
replay only ever needs entries from the latest snapshot to the log
tail (the resume cursor never starts before the latest snapshot in
the normal protocol). Older entries are forensic / audit data, not
load-bearing for resume.

### Two-tier storage

```
Time →

  seq:    0    100    200    300    400    500    600    700    ...
  snaps:        S1            S2            S3            S4

  Hot tier (`control-plane-local`, spec 20 — fast):
                                                   [── from S4 to tail ──]
  Cold tier (`object-store`, spec 20 — cheap):
        [── chunk S0..S1 ──][── chunk S1..S2 ──][── chunk S2..S3 ──][── chunk S3..S4 ──]
```

- **Hot tier** — entries from the most recent durable snapshot to
  the log tail. Lives in the StateStore. Used for cached replay
  during catch-up. Bounded.
- **Cold tier** — older entries, batched into snapshot-aligned
  chunks (each chunk covers `[snap_i, snap_{i+1})`), gzipped,
  written to object storage. Used for trajectory-viewer historical
  rendering, off-policy data extraction, audit. Effectively
  unbounded.

### Compaction triggers

1. **Snapshot-anchored** (normal path): once snapshot `S_{i+1}` is
   durably persisted, the entries in `[S_i, S_{i+1})` are batched,
   gzipped, written cold, dropped from hot. Compaction lags one
   snapshot interval behind so the hot tier always has a complete
   chain from the latest snapshot.
2. **Size-pressure** (failsafe): if hot exceeds
   `hot_log_max_bytes` (default 100 MB) or `hot_log_max_entries`
   (default 10000) without a recent enough snapshot, the
   coordinator forces an extra snapshot and triggers compaction
   immediately. Bounds worst-case hot-tier size.

### Why this doesn't break divergence detection

The resume cursor always lands at or after the latest snapshot,
never before. Divergence is detected forward from the cursor —
trainer issues seq N, we compare to log entry seq N — and both
live in the hot tier (snapshot → tail). Cold-tier entries are
never consulted by the divergence path.

The one exception is an explicit "render the full history"
trajectory-viewer request, which fetches from cold on demand —
slower but supported, and orthogonal to resume correctness.

### Cold tier is optional

```yaml
session:
  hot_log_max_entries: 10000
  # Cold tier is configured cluster-wide via spec 20's
  # `object_store.session_cold` (root) plus the pinned `logs/` and
  # `snapshots/` subtrees. A null / unset cluster config means no
  # archival — see the durability-mode constraints below.
```

When no object storage is configured, log compaction degrades to
**deletion**: post-snapshot entries are dropped, period. Resume
correctness is preserved **only when the latest snapshot required
by the selected `durability_mode` is still available locally** —
i.e. for `trainer-preemption-only` resume against a still-alive
node, where the node-local snapshot is the source of truth.
For `node-loss-resumable`, deletion-without-cold-tier silently
violates the durability contract; the control plane refuses to
start such a session on a cluster that has neither
`cluster-shared` nor a configured object-store cold tier (see
"Durability modes" below). The trajectory viewer's historical
view also degrades to "from the latest snapshot only" — forensic
audit history is lost in either case.

With object storage:

```yaml
session:
  hot_log_max_entries: 10000
  # Cold tier is the cluster-wide `object_store.session_cold`
  # config in spec 20 — there is no per-session cold_tier override.
  # Templates that want a custom logs / snapshots split can use
  # spec 20's `object_store.session_logs` / `session_snapshots`.
```

Snapshots themselves are also archived alongside their command-log
chunks, so a long-finished session's full state + log is
recoverable from cold storage even after the cluster has GC'd
local copies.

### Linkage to snapshot cadence

Snapshot interval directly determines hot-tier size: the hot tier
holds at most one snapshot interval's worth of entries (plus a
small slop margin before size-pressure compaction kicks in). The
default `snapshot_interval_s: 30` keeps hot small (tens of entries).
A template that snapshots much less aggressively (every 5 min)
will see proportionally larger hot-tier usage. **The two knobs are
linked; document them together when tuning a template.**

### Failure modes

- **Cold-tier write fails** (object-store outage, network issue):
  retain entries in hot tier and retry. The hot tier may temporarily
  exceed its soft cap; this is acceptable. Persistent retry failure
  raises an operator alert; the session keeps working but its hot
  tier isn't being trimmed. **Never silently drop entries on cold
  write failure.**
- **End-of-session flush**: when a session ends explicitly
  (`end_session`), any pending compaction must complete before the
  hot tier is cleared. Otherwise the tail entries between the
  latest archived snapshot and the session end would be lost.
  `end_session(retain_log=True)` waits on this flush; the operator-
  facing message reflects the wait if it's slow.

### Per-session storage budget under this scheme

For a 24-hour research session at ~1 step/sec, ~5 KB/step:

| Tier | Size |
|---|---|
| Hot (latest snapshot → tail) | ~10–100 MB depending on snapshot interval |
| Cold (full session, post-archival) | ~400 MB compressed, in object storage |
| 1000 concurrent long sessions | ~10–100 GB hot (redis-friendly), ~400 GB cold (object-store-friendly) |

Without cold tier: hot stays the same; cold history isn't kept.
Either way, **the running cluster's hot-storage footprint is
bounded regardless of session age**.

## Token derivation and durability

For preemption-safe resume to work end-to-end, the trainer must
durably hold the `session_token` across its own preemption. If the
trainer can't find its tokens after restart, all the platform's
session-resume machinery is moot — the sessions exist but no one
can reach them.

Insight: **both Slime and verl already checkpoint** the state
needed to recover tokens (model weights, optimizer state, training
step / `rollout_id`, data-source cursor). The platform doesn't
need a separate token-durability mechanism; it needs tokens that
are **deterministically derivable** from state the trainer is
already persisting.

### Tokens by derivation

The SDK's `start_session(...)` accepts an optional `derive_from`
dict. When provided, the token is `sha256(canonical_json(derive_from))`
(truncated and hex-encoded). Same fields → same token, every time,
across processes, across cluster restarts. When absent, the SDK
generates a random UUID — the right answer for trainers that don't
care about preemption.

```python
# Trainer with preemption resilience:
session_token = await client.start_session(
    template=Template.DEEP_RESEARCH,
    init={...},
    derive_from={
        "experiment_id": "exp-2026-04-25-grpo-7b",  # pinned at run start
        "rollout_id":    42,                         # from trainer checkpoint
        "sample_index":  17,                         # from data source
    },
)

# Trainer without preemption concerns:
session_token = await client.start_session(template=..., init=...)
# → random UUID; sessions don't survive trainer preemption.
```

### Slime: automatic via the adapter

The Slime adapter (spec 11) derives tokens automatically from
fields the Slime trainer already maintains:

```python
session_token = await client.start_session(
    template=template,
    init=init_for_sample(sample),
    derive_from={
        "experiment_id": args.experiment_id,
        "rollout_id":    args.rollout_id,
        "sample_index":  sample.index,
        "group_id":      group_id,
    },
)
```

On Slime restart-from-checkpoint: Slime loads checkpoint →
`rollout_id` is restored → `data_source.load(rollout_id)` rebuilds
the Sample list with the same `.index` values → adapter recomputes
the same `derive_from` dict → same hash → same token →
`client.attach(...)` reaches the in-flight session.

**Zero extra code in Slime.** The trainer doesn't know sessions
exist; the adapter handles all of it.

### verl: automatic via the adapter

The verl adapter (spec 12) derives from `(experiment_id,
training_step, batch_index)` — all already in verl's checkpointed
state and per-row `DataProto.index`. Same recovery story.

### Custom trainers

Two patterns:

- **Already checkpointing**: pass `derive_from` with whatever
  stable identifiers the trainer is checkpointing. The SDK handles
  the hashing. Most custom trainers fall here.
- **Not checkpointing**: don't pass `derive_from`; accept random
  UUIDs and that sessions die on preemption. Equivalent to today's
  behavior.

### What the trainer must explicitly pin

Only one thing: **`experiment_id`** at run start. Everything else
falls out of existing checkpoints. Recommended sources, ranked:

1. **wandb run ID** — best. Already durable, already user-visible,
   already stable across resumes.
2. **User-supplied config-file string** — fine.
3. **Random UUID generated at run start, persisted to the
   trainer's checkpoint directory** — works; adds explicit user
   work.

A trainer that fails to pin `experiment_id` (or pins a non-
deterministic value like `time.time()`) silently breaks resume.
The SDK validates that `derive_from` values are of stable types
(strings, ints, bools) and rejects floats / datetimes / lists with
unspecified ordering.

### Hash stability

`canonical_json(derive_from)` follows a strict canonicalization:
keys sorted lexicographically; only string / int / bool values
permitted; UTF-8 encoded; no whitespace. The SDK pins this rule
in code; **changing it is a breaking change** that would invalidate
every existing token, requiring a major version bump.

### Edge cases

- **Token collision**: two trainers using the same `experiment_id`
  simultaneously hit the same session. Mitigated by recommending
  wandb run IDs (unique by construction). Documented.
- **`derive_from` fields evolve between runs**: token changes;
  prior sessions appear orphaned. Expected — the user did this on
  purpose (e.g. renamed `experiment_id`).
- **Session GC'd between preemption and restart**: deterministic
  recompute still succeeds, but `attach` returns `SessionExpired`.
  Trainer restarts that rollout from scratch.

## Durability modes

"Preemption-safe resume" is not one promise — it is three. The
resume guarantee depends on which off-node tiers a cluster has
configured. Templates declare which mode they require; the
control plane refuses to start a session whose required mode is
not satisfiable on the current cluster.

| Mode | Hot log location | Snapshot chain location | Survives trainer preemption? | Survives node loss? | Survives cluster loss? |
|---|---|---|---|---|---|
| `trainer-preemption-only` | StateStore (control-plane-local) | node-local only (`/var/lib/xrlenv/snapshots/`) | yes | **no** | no |
| `node-loss-resumable` (default for spot-VM training) | StateStore (control-plane-local) | replicated to `cluster-shared` (NFS/EFS) **or** `object-store` cold tier | yes | yes | partial — depends on `cluster-shared` substrate |
| `forensic-only` | StateStore + cold-tier archive | node-local; lost on node loss | n/a (resume not supported) | n/a | n/a |

Declared per template:

```yaml
session:
  durability_mode: node-loss-resumable     # | trainer-preemption-only | forensic-only
  snapshot_replication_target: object-store     # required for node-loss-resumable; tier name from spec 20
```

Validation:

- `node-loss-resumable` requires the cluster has either a
  `cluster-shared` mount available to every node OR a configured
  `object_store.session_cold` URI (spec 20). Control plane
  refuses to start such a session on a cluster that has neither —
  it would be silently lying about the resume guarantee.
- `trainer-preemption-only` has no off-node snapshot requirement;
  the SDK warns at `start_session` if the operator passes
  `lifetime_mode: snapshot-on-detach` (which would defeat the
  purpose by destroying the only durable copy).
- `forensic-only` exists *only* for retained history. The control
  plane therefore **requires** `object_store.session_cold` (or an
  explicit per-session log archival URI) to be configured at
  `start_session` time and refuses with `ColdTierRequired` if
  neither exists — a `forensic-only` session without a cold tier
  is purely deletion-after-compaction, which would defeat the
  mode's only purpose. The `Attach` API additionally raises
  `ResumeNotSupported` on any reattach attempt regardless of
  node-alive state — `forensic-only` is read-only history, not a
  resume mode. Operators who want "session log on the local node,
  no archive, no resume" should not use `forensic-only`; they
  should not start a session at all and use the standard
  rollout-tied trajectory path instead.

This means earlier text saying "off-node persistence survives
node loss" is precise only for `node-loss-resumable`. Cold tier
is required for `forensic-only` and for any `node-loss-resumable`
configuration that does not have `cluster-shared` snapshot
replication available. The only mode for which cold tier is truly
optional is `trainer-preemption-only` against a still-alive node.

## Operator surface

```
$ xrlenv sessions
SESSION                  TEMPLATE     STATE        AGE   LAST_TOUCH  LOG_LEN  SNAPSHOT
ses-7f3a-...             swebench     attached     2h    3s          412      2m ago
ses-9b21-...             swebench     detached     45m   12m         189      1m ago
ses-c041-...             osworld      detached     8h    4h          1924     3h ago

$ xrlenv sessions show ses-7f3a-...
$ xrlenv sessions end ses-9b21-... --retain-log    # release sandbox, keep log
$ xrlenv sessions resume-as ses-c041-...           # interactively reattach (debug)
```

The admin panel (spec 13) gets a `/sessions` view alongside this
spec — i.e. when sessions ship (phase 3) — listing attached /
detached sessions, log lengths, snapshot ages, and a reattach button
(operator-side, for debugging — not for production rollout reattach
which always goes through the trainer SDK).

## Storage / cost

A session that runs for hours generates tens of thousands of log
entries — possibly MB to tens of MB of log per session. Spec 20
defines the canonical storage tiers; sessions consume them as
follows:

- **Hot** (`control-plane-local` tier, spec 20): the
  `command_log_hot` table in StateStore; bounded by snapshot
  cadence + `hot_log_max_*` failsafe.
- **Snapshots** (`node-local` tier, spec 20 path
  `/var/lib/xrlenv/snapshots/<session_id>/`): per-node Cube
  overlaybd chains. For `node-loss-resumable` sessions the chain
  must be replicated to either `cluster-shared` (NFS/EFS) **or**
  the `object-store` cold tier — the control plane validates one
  of the two is configured at `start_session` time.
- **Cold** (`object-store` tier, spec 20): post-snapshot log
  chunks under `<session_cold>/logs/<session_id>/...`; replicated
  snapshot chains (when `node-loss-resumable` selects the
  object-store target) under
  `<session_cold>/snapshots/<session_id>/...`. The two subtrees
  share `object_store.session_cold` as a root by default and may
  be split via `object_store.session_logs` /
  `object_store.session_snapshots` overrides (spec 20). Cold tier
  is optional for `trainer-preemption-only`; it is **required**
  for `node-loss-resumable` whenever `cluster-shared` is also
  unavailable (the control plane refuses to start such a session
  without one of the two).

Sessions retained past their owning rollout count toward the
`session_state` disk pool (spec 10). The Image Cache Manager
(spec 15) treats session snapshots as pinned-while-attached and
LRU-evictable when detached past `session_idle_eviction_s`
(default 24 h).


## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


This entire spec is **phase 3** — preemption-safe sessions are not
on the critical path for phase 0 (initial platform end-to-end),
phase 1 (Cube + Slime/verl + analysis + lazy load + function-call),
or phase 2 (autoscale + branch-snapshot + multi-tenant). It is real
work and substantial RPC surface; deferring keeps the earlier
phases focused.

What phases 0 / 1 / 2 must preserve to make phase 3 land cleanly
without redesign — the **design hooks**:

1. **Stub request middleware seam** (spec 01). The in-sandbox stub
   should route every request through a single dispatcher so a
   logging middleware can later be inserted without touching every
   handler. Concretely: each handler is registered with the
   dispatcher; the dispatcher is the only path that calls them.
2. **Sandbox lifetime ≠ rollout lifetime in the data model** (spec
   03, spec 04). Even though phase 0 destroys the sandbox at
   rollout finish, the StateStore should not bake `sandbox_id ==
   rollout_id` into any schema or code path. `sandbox_id` is its
   own column on the `rollouts` row; the coordinator's destroy
   path should consult an "owner refcount" (currently 0 or 1) so
   a future detached-session refcount of 1-while-detached works
   without rewrite.
3. **Snapshot is a first-class capability, not a debug feature**
   (spec 01). The Cube backend's chainable overlaybd snapshots
   (already phase 1) are the substrate phase 3 builds on. The
   Docker backend's `docker commit` based best-effort snapshot is
   acceptable for phase 1; phase 3 will warn but not block.
4. **Trajectory sinks vs command log are separate concerns** (spec
   08). The pluggable trajectory sink already exists; the command
   log is a *different* artifact at a *different* layer. Don't
   conflate them in code or schema.
5. **GC consults "is there an owner?" not "is the rollout
   finished?"** (spec 04). Today these collapse to the same
   answer; phase 3 separates them (a detached session is sandbox
   with no live rollout but with a session as owner).

These hooks cost almost nothing to honor in phase 0 / 1 / 2 — they
are mostly naming and refcount discipline — and prevent a
substantial later rewrite.

When phase 3 lands, it adds:
- `start_session` / `attach` / `end_session` API and the bidi
  `Attach` stream.
- Off-node command log persistence (hot tier in StateStore; cold
  tier in object storage when configured).
- Step-level cached-result replay protocol — anchored on the
  snapshot-cadence correctness contract (`max_resume_gap`); no
  per-call idempotency markers.
- Admin-panel sessions view (extends spec 13).
- Operator CLI (`xrlenv sessions {list, show, end}`).

## Cross-references

- **Spec 01**: snapshot/restore (chainable for Cube), backend
  capabilities. Sessions require **all** of:
  `supports_chainable_snapshot=True` (cheap incremental snapshots
  per `snapshot_every_k_steps`), `live_state_captured=True`
  (snapshot includes running process state), and a
  `durability_mode` whose snapshot-chain location actually
  survives the failure being claimed (per the durability table
  above — `node-loss-resumable` requires `cluster-shared` or
  `object-store` replication; `trainer-preemption-only` is
  satisfied by node-local). Docker is excluded by capability
  flags; sessions are Cube-only.
- **Spec 02**: `Deadline` semantics still apply within a session;
  `task_key` / `group_id` propagate; cancellation cleanly ends a
  session.
- **Spec 03**: the StateStore gains `sessions` and `command_log`
  tables; the gRPC API gains the four session RPCs.
- **Spec 04**: node agent honors detached-session sandboxes (don't
  GC sandboxes that have an attached session token even if no
  trainer is connected).
- **Spec 08**: trajectory recording is unchanged at the rollout
  level; the *command log* is a separate, lower-level log living
  alongside.
- **Spec 13**: `/sessions` view added.
- **Spec 17**: trajectory viewer can render the command log too,
  via a new "raw commands" tab on rollout detail.
- **Spec 19**: session token storage and revocation follow the
  identity / token rules; resume requires the same operator
  scope as the original `start_session`.
- **Spec 20**: canonical storage tiers (hot / cold / snapshot
  paths); `sessions` and `command_log_hot` schema rows.
- **Spec 21**: `SnapshotCommand` message is the wire form of the
  cadence-driven snapshot the session takes; `Attach` bidi
  uses the same reverse stream.

## Open design questions (to discuss before implementation)

1. ~~**Granularity of the log**~~ — **resolved**. Log at the
   `EnvAdapter.step(action)` boundary; op sub-traces nested for
   debug only, never used as a replay primitive.
2. ~~**Default idempotency**~~ — **resolved**. Step-level cached
   replay doesn't re-invoke the EnvAdapter, so internal-op
   idempotency is moot. The replacement lever is **snapshot
   cadence** (frequent cheap snapshots on Cube), not op-level
   markers. Sessions are Cube-only as a consequence.
3. ~~**Sandbox lifetime when detached**~~ — **resolved**. Three
   configurable modes (`keep-warm`, `snapshot-on-detach`,
   `keep-warm-with-decay`) along two axes (task state size, detach
   duration). Default is `keep-warm-with-decay` with 10 min window;
   templates set their own defaults; trainers override per-call.
   Reactive eviction shed detached-but-warm first under capacity
   pressure. Periodic snapshots fire regardless of mode for
   durability against node loss. See section above.
4. ~~**Log size cap**~~ — **resolved**. Two-tier storage with
   snapshot-anchored compaction; hot tier (StateStore) holds
   entries from latest snapshot to tail (bounded by snapshot
   cadence + size-pressure failsafe); cold tier (object storage,
   under `object_store.session_cold`) holds older entries in
   snapshot-aligned chunks under `<session_cold>/logs/...`. Cold
   tier optionality is gated by `durability_mode` (see the
   "Durability modes" section above):
   - `trainer-preemption-only` — cold tier is optional. When
     absent, compaction degrades to deletion; resume from the
     still-alive node-local snapshot remains correct; forensic
     history is lost.
   - `node-loss-resumable` — cold tier (or `cluster-shared`
     snapshot replication) is **required**. The control plane
     refuses to start the session on a cluster that has neither.
   - `forensic-only` — cold tier is **required** at session
     start (see "Durability modes" above); the control plane
     refuses with `ColdTierRequired` when neither
     `object_store.session_cold` nor an explicit per-session log
     archival URI is configured. The mode's only value is
     archived history; without the cold tier it would be
     deletion-after-compaction.
5. ~~**Trainer-side session bookkeeping**~~ — **resolved**.
   `session_token` is *deterministically derived* via
   `start_session(..., derive_from={...})`, hashed from fields the
   trainer already checkpoints (`experiment_id`, `rollout_id` /
   `training_step`, `sample_index` / `batch_index`). Slime and verl
   adapters auto-derive; the upstream trainers never see sessions.
   Custom trainers either pass their own `derive_from` or accept
   random tokens. Only `experiment_id` must be explicitly pinned by
   the user at run start; wandb run IDs work perfectly. See section
   above.
