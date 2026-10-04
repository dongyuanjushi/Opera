# Stage 3 — health-derived adaptive admission controller

Slice plan for Stage 3 of `notes/admission-capacity-design.md` (pillar
**P1**). Stages 1–2 made the cluster *observable* and made queueing
*honest*. Stage 3 closes the loop: the cluster reads its own health and
**acts** on it — contracting how many containers it admits when the
docker daemons are saturating, expanding again when they recover.

## Goal

After Stage 3: each node carries an *adaptive* concurrent-acquire
limit. When a node's `docker run` latency climbs or it starts erroring
(the Stage-1 signals), its limit contracts; when health holds, it
expands. The scheduler refuses placement on a node already at its
limit, so the overflow queues (Stage 2) instead of melting the daemon.
The cluster discovers its sustainable concurrency by leaning into the
limit gently, not catastrophically — *online feedback replaces upfront
calibration*.

## Scope

**In:** a pure `HealthAimdController` (AIMD math); a control-plane loop
that feeds it the Stage-1 per-node health + current load and updates
per-node limits; a scheduler placement filter that enforces them; a
config flag so it can be A/B'd against the current static estimator;
the admin "Cluster health" adaptive-limit column (the Stage-1 P3.3
deferral).

**Out:** changing the queue mechanics (Stage 2 owns those); the static
`fits()` CPU/mem/disk gate and the issue-#14 disk-pressure gate (both
stay, as their own independent placement filters — AIMD is layered
*alongside* them, not derived from them; see D2); cross-node
rebalancing of already-running work.

## How it plugs in

`Scheduler.place()` already filters candidate nodes through a series of
gates — backend-capable, not disk-pressured (issue #14), not in the
command-timeout cooldown (issue #18). Stage 3 adds one more filter:
**a node at or above its adaptive limit is not eligible.** When every
candidate is over its limit, `place()` raises `CapacityExhausted` → the
request queues in the admission queue → Stage 2's backpressure. That is
the whole control loop.

## Decisions

- **D1 — a pure controller in `capacity.py`.** `HealthAimdController`
  holds per-node `limit` state and exposes `step(health_by_node,
  load_by_node)` (run one AIMD round, update limits) and
  `limit_for(node_id)`. It takes data *in* and is otherwise pure — no
  registry / scheduler dependency — so the AIMD math is unit-tested in
  isolation. The capacity estimator's reserved "online refinement" hook
  (`capacity.py`) is where this lands; the static `StaticCapacityEstimator`
  stays as the hard `fits()` ceiling.
- **D2 — AIMD mechanics + the (non-resource-derived) bounds.** Per
  node: on a *bad* health tick, `limit = max(floor, limit // 2)`
  (multiplicative decrease — contract hard); on a *good* tick,
  `limit = min(max_limit, limit + 1)` (additive increase — recover
  slowly). `floor = 1`.

  The limit is **not** ceiling-ed by `StaticCapacityEstimator.fits()`.
  For raw containers — the workload of concern — per-task resource
  cost is unknowable upfront: a SWE-bench-Pro task may need 2 CPU +
  50 GB or 0.5 CPU + 10 MB, and the consumer declares nothing. The raw
  path runs against a *synthetic* manifest with a placeholder
  `ResourceSpec`, so `fits()` is not a meaningful raw-container
  ceiling — and making it "correct" is exactly the calibration
  problem §2 abandons. The real bound is the **health signal**: AIMD
  grows the limit until `docker run` p95 / errors degrade, then
  contracts. `max_limit` is only a *tunable runaway-guardrail* (so
  additive-increase can't drift up unbounded during a long quiet
  stretch) — not a resource calculation.

  The static `fits()` CPU/mem/disk gate and the issue-#14
  disk-pressure gate keep running as their own independent placement
  filters (meaningful for case-1 template rollouts, coarse for raw);
  AIMD is a separate filter layered on top. The issue-#18 per-node
  create/destroy/pull semaphores remain the hard floor that absorbs
  AIMD's sawtooth overshoot.
- **D3 — cold-start: slow-start.** Seed each node's limit at a
  user-tunable `initial_limit` (default **16**) and let
  additive-increase ramp from there — a fresh node never over-admits
  before it has health data. `initial_limit` is an operator knob; the
  right starting point is cluster-shape-dependent.
- **D4 — what "bad health" means.** A node tick is *bad* if its
  Stage-1 snapshot shows `create_p95_ms` above `p95_bad_threshold_ms`
  — a user-tunable knob, default **60000 ms (60 s)** — **or** any
  `docker_error_count` / `docker_timeout_count` in the window.
  Errors/timeouts are the emergency signal; p95 is the smooth one.
  Both route to the same multiplicative decrease for v1 (a harder cut
  on errors is a possible later refinement).
- **D5 — behind a flag, A/B'd.** A config flag (`adaptive_admission`,
  default **off**). Off → today's static behaviour exactly. On → the
  controller + the scheduler filter are live. This lets a real run
  compare static vs adaptive before adaptive becomes the default.
- **D6 — control cadence.** A background loop ticks every ~15 s (a few
  heartbeats of data per tick). The controller reads each connected
  node's latest health from the in-memory `RemoteNodeTransport`
  (`_last_health`) — freshest, no SQLite round trip — and the current
  per-node load from the scheduler's existing load accounting.
- **D7 — node-load source.** "Current load" = the same per-node
  running-container count the scheduler already computes
  (`_gather_cluster_load` + the raw-session provider). A node is
  over-limit when `load(node) >= limit(node)`.

## Deliverables

### 3a — `HealthAimdController` (pure AIMD)

`xrlenv/control/capacity.py` — `HealthAimdController` + an
`AimdConfig`. The config carries the operator knobs —
`initial_limit` (default 16, D3), `p95_bad_threshold_ms` (default
60000, D4), `max_limit` (the runaway-guardrail, D2) — plus the AIMD
constants (decrease factor 0.5, increase step 1, floor 1, tick
cadence). `step(...)` runs one round; `limit_for(...)` reads back. No
I/O, no async — pure, fully unit-tested (decrease on bad signal,
increase on good, floor / max_limit clamping, slow-start seed, an
unknown node).

### 3b — control loop + scheduler enforcement

- `distributed_runtime.py` — a background task (lifecycle alongside the
  watchdog / GC reconciler) that every ~15 s gathers health + load and
  calls `controller.step(...)`.
- `scheduler.py` — `place()` gains the adaptive-limit filter; `Scheduler`
  takes an optional `aimd_controller`. When the flag is off, no
  controller is wired and the filter is a no-op.
- the `adaptive_admission` flag through `build_distributed_runtime`.

### 3c — operator surface

- the admin "Cluster health" per-node table gains the **adaptive
  limit** (and last-contraction) column — the Stage-1 P3.3 deferral,
  now that the limit exists.
- a per-node `xrlenv_node_admission_limit` metric for graphing.

## Tests

- `HealthAimdController` — decrease halves on a bad signal, increase
  steps on good, `floor` / `max_limit` clamp, slow-start seed at
  `initial_limit`, the `p95_bad_threshold_ms` knob, AIMD sawtooth over
  a sequence of ticks.
- scheduler — a node at its adaptive limit is filtered out of
  placement; all-over-limit → `CapacityExhausted` (→ queue).
- flag off → behaviour byte-identical to the static estimator.
- admin — the adaptive-limit column renders from the live controller.

## Acceptance

- On a real over-subscribed run with `adaptive_admission` on, the
  per-node limits visibly contract as `docker run` p95 climbs, the
  overflow queues instead of timing out, and there are zero
  `containers.run` 600 s daemon-melt failures — compared against a
  flag-off run of the same workload.
- `gen_protos.sh` n/a (no proto change); full unit suite + `ruff` green.

## Suggested commit breakdown

1. 3a — `HealthAimdController` + `AimdConfig` (pure, unit-tested).
2. 3b — control loop + scheduler placement filter + the flag.
3. 3c — admin adaptive-limit column + the per-node metric.
