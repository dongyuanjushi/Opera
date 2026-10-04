# Admission & capacity architecture — design brief

**Status:** design — forks resolved in the 2026-05-20 walk-through and recorded
inline as **Decisions**. Ready to break into implementation slices (§7). One
item — P3's streaming-vs-poll wire choice — has an accepted v1 fallback noted.

**Branch:** `design/admission-capacity-architecture`.
**Lineage:** follow-on from issue #18 (PR #19). Issue #18 made the cluster
*survive* over-subscription; this work makes it *handle* over-subscription
correctly.

---

## 1. Problem

The 2026-05-20 SWE-bench-Pro full run (`--num-workers=64`, 2-node cluster,
731 tasks) exposed the core gap. Of 731 tasks:

- 25 "no report" — all infra: the node-side docker daemon, swamped by
  concurrent multi-GB image work, blew its 600s HTTP-client ceiling on
  `containers.run`. The cluster **admitted more containers than the nodes
  could carry** (admission summary: "peak concurrent containers sustained:
  ~65 … you requested more than the cluster can carry") and then failed
  *catastrophically* — timeouts, grader crashes — instead of pushing back.

Issue #18 / PR #19 added per-node pull/create/destroy concurrency caps, so a
burst now degrades into a node-side wait rather than a daemon meltdown. That
is a *stopgap*: it bounds the blast radius. It does not answer the real
question.

## 2. The core question

**What should the cluster do when the user has no idea of the workload's
capacity needs or per-task resource cost?**

This is the normal case for agentic RL / eval workloads: a SWE-bench-Pro task
is a multi-GB image with an unknown CPU/disk/mem profile until it runs. The
user expresses a *desired* parallelism (`--num-workers=64`); they cannot know
what the cluster can actually sustain.

Two candidate behaviors were discussed:

1. **Reject at submission** if the request would over-subscribe.
2. **Queue with backpressure** — admit only what keeps the cluster healthy,
   queue the rest, never time out a job for *waiting*, and tell the user
   clearly how many are running / queued / why.

### Decision: option 2, with option 1 in a narrow form

- Hard **reject at submit** is the wrong primary mechanism: per-task cost is
  unknowable upfront, and rejecting a 731-task / 64-wide request just pushes
  the calibration burden back onto the user. Reject is correct **only** for a
  request that is *impossible*, not merely *heavy* — e.g. a single task whose
  resource ask exceeds any node. No amount of queueing fixes that.
- A **soft warning at submit** ("requested 64-wide; cluster currently
  sustains ~30; the rest will queue") is useful and cheap.
- Everything else is **option 2**.

**Guiding insight:** *online feedback replaces upfront calibration.* We do not
need to know per-task cost in advance. The cluster admits work, watches its
own health, and contracts admission when health degrades — discovering its
sustainable limit by leaning into it *gently* (backpressure) instead of
*catastrophically* (timeouts).

## 3. What xrlenv has today (and what's wrong with it)

| Component | File | Today | Gap |
|---|---|---|---|
| Admission queue | `control/admission.py` | Queues rollouts when the scheduler can't place; sqlite-backed; background drain worker. | Limit is **estimate-derived**, not health-derived → over-admits. |
| Scheduler capacity gate | `control/scheduler.py` | Placement + disk-pressure gate (issue #14) + raw-session load accounting. | Capacity = a number, not a feedback loop. |
| Capacity estimator | `control/capacity.py` (spec 10) | Static + "online-refined" estimate. | Refines toward a *throughput* estimate, not a *health* signal. |
| `queue_timeout_s` | spec 02, raw acquire path | Bounds how long a request waits in the queue (default 300s rollout / 3600s raw). | A job can **fail for waiting** — conflates queue-wait with run-time. |
| Per-node caps | `node/image_cache.py`, `node/raw_container.py` | `pull_concurrency`, `create_concurrency`, `destroy_concurrency` semaphores. | A second, *independent* queue layer; waiting on it counts against `acquire_timeout_s`; invisible to cluster admission accounting. |
| Over-request feedback | `compat/docker_client.py` atexit summary | Reports queue stats. | **Post-mortem** — printed at end of run, not live. |
| Admin "Cluster health" page | `admin/templates/health.html`, `admin/server.py` | Triages stuck *sandboxes* + failure-rate per *template*; an "Under-utilized nodes" table; a "Heartbeat-late nodes" placeholder. | Case-1-centric (both triage sections read empty for raw workloads); frames *under*-use while the pain is *over*-subscription; the heartbeat section is a non-functional placeholder. → reworked in P3.3. |

The bones of option 2 exist. Several things are structurally wrong, and the
operator's health surface doesn't show what actually matters.

## 4. Design principles

- **Mechanism, not policy.** Core ships the health-aware admission queue,
  the backpressure controller, and a legible running/queued/why state. The
  *policy* — fail-fast vs wait-forever, how aggressive the contraction —
  stays a consumer knob.
- **Online feedback replaces upfront calibration** (§2).
- **Backpressure, not rejection.** A heavy request is throttled, not refused.
- **Queue-wait ≠ run-time.** Two separate clocks. Waiting in a queue consumes
  no cluster resources and is not a failure; it must not burn a run deadline.
- **A silent queue is indistinguishable from a hang.** Legible live state is
  not a nicety — it is what makes "don't time out queued work" safe.

## 5. Direction — four pillars (forks resolved 2026-05-20)

Synthesising idea for P1: **AIMD finds the operating point; the per-node
static caps shipped in #18 catch its overshoot** — the controller can stay
simple because there is a hard floor beneath it.

### P1 — Health-derived adaptive admission limit

Replace the estimate-driven admit decision with a feedback controller: each
node watches its own health and contracts/expands its concurrent-acquire
ceiling.

- **Decision — health signals.** *Primary:* `docker run` (container create)
  call latency, p95 over a rolling window, per node. It climbs *smoothly*
  under daemon load and is directly attributable to node saturation — unlike
  exec latency (polluted by how slow a task's own suite is) or pull latency
  (dominated by image size + network). *Emergency triggers:* docker
  timeout-rate + 500-rate — lagging signals (the failure already happened),
  so they force a fast hard contraction rather than driving the smooth loop.
  *Separate hard gate, not folded into the loop:* disk-pressure — issue #14
  already owns it; a different, slow-moving axis.
- **Decision — contraction policy.** AIMD: multiplicative decrease (×0.5) the
  instant the signal goes bad, additive increase (+1 per stable interval) on
  sustained-good; conservative recovery. An over-subscribed docker daemon
  *is* a congestion problem. AIMD's sawtooth overshoot is absorbed by the
  #18 per-node create/destroy/pull caps, which remain as the hard floor.
- **Decision — granularity.** Per-node adaptive limits; cluster capacity =
  their sum. Docker daemons are per-node and nodes diverge (li-4 vs li-5);
  one cluster number can't express that, and the scheduler already places
  per-node.
- **Decision — where it lives.** Extend `control/capacity.py` — its
  online-refinement hook is rewired from throughput-estimation to
  health-AIMD. Scheduler + `AdmissionQueue` consult capacity unchanged; the
  number behind it becomes adaptive. The existing throughput estimate stays
  as the cold-start seed + a sanity ceiling. Spec 10 gets updated.

### P2 — Separate the queue-wait clock from the run clock

- **Core (not a fork):** run-time deadlines (`session_deadline_s`, exec hard
  timeout) start **only at admission** — queue-wait never consumes them.
- **Decision — default queue-wait.** Effectively unbounded: a large backstop
  cap (~24h) so a forgotten run can't leak forever, *not* a literal infinity.
- **Decision — fail-fast.** An explicit opt-in: a user who wants "a slot in
  5 min or fail" sets a small `queue_timeout_s`. Today's finite 300s default
  is backwards — flip it to the large backstop.

### P3 — Live, legible backpressure feedback

Continuously, while the run is in flight: *N running, M queued, 0 failed;
mean queue wait; cluster at capacity; your requested C exceeds sustainable
~S.*

- **Decision — wire protocol.** `AcquireContainer` becomes a
  **server-streaming RPC**: it emits `QueuedUpdate{position, est_wait,
  reason}` frames while queued, then a terminal `AcquireResult`. Push, not
  poll — a silent block is what makes people add queue timeouts in the first
  place. *Accepted v1 fallback* if the streaming change is too invasive for
  the first slice: keep `AcquireContainer` unary, add a `QueueStatus` poll
  RPC the consumer hits while blocked. Streaming is the target. (Proto
  change; drop-in + `Client` consume it differently — specs 02 / 05 / 21.)
- **Decision — surfaces, prioritised.** (1) drop-in / SDK live output — the
  user's own terminal must show running/queued/wait live; this is the #19
  atexit summary made live, and where the over-request realisation has to
  land. (2) admin panel — see P3.3. (3) a `xrlenv queue` CLI — optional.
- **Decision — P3.3, rework the admin "Cluster health" page.** Today's page
  is unfit: case-1-centric (it triages stuck *sandboxes* and failure-rate
  per *template* — concepts raw-container workloads don't have, so both
  sections read empty), it carries a non-functional "Heartbeat-late nodes"
  *placeholder*, and its "Under-utilized nodes" framing points at *under*-use
  while the real pain is *over*-subscription. Rework it into a **per-node
  signal view that renders exactly the health inputs P1 computes** — one
  computation, two consumers (the AIMD controller and the panel). Per node:
  running / adaptive-limit, `docker run` p95, docker timeout+500 rate, disk
  free, heartbeat age, current AIMD limit + last contraction. Cluster:
  admission queue depth, mean/max queue wait, total running vs. capacity.
  Make the remaining triage sections raw-container-aware; implement the
  heartbeat-age column for real (post-#18 the data exists) or delete it.

### P4 — Unify the node-side sub-queues

The per-node pull/create/destroy semaphores are a second queue layer below
the admission queue. **Decision — option (a):** once P1's per-node limit is
doing its job the right number of acquires land per node, so the semaphores
are rarely the binding constraint and degrade to a pure safety floor. Their
**contention depth becomes a P1 input signal** (a backing-up create
semaphore = P1 should contract). We do *not* build separate accounting to
exclude node-side waits from the run deadline (rejected option b): a
non-trivial node-side wait is a P1 failure, to be fixed in P1.

## 6. Rejected / deferred

- **Hard reject-at-submit for heavy (not impossible) requests** — §2.
- **Per-task resource calibration upfront** — replaced by online feedback.

## 7. Staged implementation sketch

1. **Observability first (P3).** Compute + expose the P1 health signals, and
   do the P3.3 admin "Cluster health" rework on top of them. Independently
   useful, and it makes everything after it measurable — you cannot tune P1
   without first being able to see its inputs.
2. **P2** — split the clocks; flip the `queue_timeout_s` default; the
   "queued" wire status (streaming, or the poll fallback).
3. **P1** — the health-AIMD controller in `capacity.py`, behind a flag,
   A/B'd against the current estimator on a real run.
4. **P4** — fold the node-side sub-queue contention into P1's signal set.

## 8. References

- Issue #18 / PR #19 — the survival stopgap this builds on.
- `specs/03-control-plane.md`, `specs/10-capacity-estimator.md`,
  `specs/02-rollout-api.md` (`queue_timeout_s`), `specs/21` (node wire).
- 2026-05-20 run log: `coding-bench/tmp/e2e-swepro-eval-20260520T225918Z.log`
  — 25/731 no-report, all docker-daemon overload.
- `notes/related-work.md` — check how upstream batch systems (Slurm, k8s,
  Ray) frame admission vs. queueing before locking P1's controller shape.
