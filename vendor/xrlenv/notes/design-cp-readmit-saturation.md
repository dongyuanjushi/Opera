# Design: control-plane re-admit on node-saturation create failure (D-AR-2026-07-07-B)

**Status:** IMPLEMENTED (2026-07-07) — the three audit concerns are folded into
the Key decisions / Blast radius / Tests below, and the design shipped as
described. See "Implementation landed" at the end for exactly what changed +
test coverage.

## Problem

`RawContainerCoordinator.acquire` places a task then calls `node.acquire_container`.
When that create fails with a **node-saturation** fault (sysbox-fs
`pre-register … DeadlineExceeded`, docker 5xx, or a `NodeCommandTimeout`) AFTER the
node's own bounded retry exhausts, the coordinator's `except` cleans up and
re-raises — the task dies. The AdmissionQueue only re-queues on a *proactive*
`CapacityExhausted` from `place()`, never a *reactive* create-time saturation 5xx.
Under sustained overload (a full sweep at high concurrency funnelling all sysbox
tasks to one sysbox node) the node-local retry can't recover, so tasks fail.

## Fix: coordinator-level re-admit loop, with explicit failed-node exclusion

Wrap **placement + `node.acquire_container`** in a bounded re-admit loop, *after*
fleet classification. On a saturation failure: release the placement, remember the
node id, and re-admit **excluding the failed nodes** so we don't re-hit the hot
node (AIMD lag alone is not enough — it's reactive + laggy).

### Key decisions (operator-refined)

1. **Loop placement.** Loop only the placement/acquire attempt; do **fleet
   classification once, before the loop**. Re-classifying would trip the existing
   `_fleet_opening` duplicate-opener guard. A fleet **companion** targets a pinned
   node (no placement) — it does NOT re-admit; its saturation failure propagates.
   Non-fleet + fleet-opener use the loop.
2. **Classifier `_is_retriable_acquire_saturation(exc)`** (control-plane):
   - `NodeCommandTimeout` → retriable (node wedged/didn't answer).
   - `XRLEnvError` whose message clearly indicates docker 5xx / `DeadlineExceeded`
     / `sysbox-fs` / timeout → retriable.
   - NOT `BackendCapabilityMissing`, `ImageMissingOnNode`, `ImagePullFailed`,
     `CapacityExhausted`, `MountDenied`, `AuthDenied`; NOT a 4xx (409 name-in-use /
     404 / manifest-unknown) — re-placing can't fix those, so they propagate.
3. **Explicit failed-node exclusion (revised per audit #1).** Track failed node ids
   across the logical acquire; pass `exclude_node_ids` into `place()` /
   `admission.acquire()`.
   - `Scheduler.place(exclude_node_ids=…)` drops those nodes from the candidate
     pool *after* the backend+runtime capability check. If exclusion empties an
     otherwise-non-empty pool → `CapacityExhausted` (queue **waits**, not spins).
     If the pool was empty *before* exclusion (no capable node at all) →
     `BackendCapabilityMissing` (unchanged hard fail).
   - `AdmissionQueue.acquire(exclude_node_ids=…)` passes it to the fast-path
     `place()` **AND stores it on the queued `_Waiter`** so the drain honours it —
     otherwise a "node A failed, node B temporarily full" acquire would enqueue
     without the exclusion and the drain could land back on hot node A.
   - **The coordinator relaxes, not the queue.** Before each attempt the
     coordinator computes `effective_exclude = failed if (capable − failed) else ∅`
     using a new `Scheduler.capable_node_ids(backend, container_runtime)`. So:
     with two or more sysbox nodes, A fails → exclude {A} → land on B (or wait for
     B if full, still excluding A). If B *also* fails → failed = {A,B} = all capable →
     `effective_exclude = ∅` → queue normally so AIMD/health/capacity decide when
     either is safe again (still bounded by `_CP_REQUEUE_MAX` + the total cap).
     Single-node A: attempt 1 fails → failed={A}=all capable → relax → wait for A
     to drain, then place on A. No permanent hard-exclusion of the whole pool.
4. **Total timeout budget + total wall-clock cap (revised per audit #3).** The
   **re-admit phase** — queue wait AND node wire across the re-admits, measured
   from the FIRST create-time saturation failure — is bounded by a **total
   wall-clock cap** `_CP_REQUEUE_TOTAL_CAP_S`. The cap is *armed only on that
   first failure*, so a normal first-attempt admission wait still gets its full
   `queue_timeout_s` (24 h default) — the cap is NOT a bound on the whole logical
   acquire from t=0. Once armed, two axes feed each attempt, and BOTH are clamped
   to the remaining cap:
   - `admission.acquire(timeout_s=…)` (queue wait) gets
     `min(remaining queue_timeout_s, remaining _CP_REQUEUE_TOTAL_CAP_S)`. This
     matters because a queued `_Waiter` carries the exclusion (decision 3) and can
     therefore keep waiting on the non-failed pool — without the cap it could sit
     up to `queue_timeout_s` (24 h default) despite the total cap.
   - `node.acquire_container(acquire_timeout_s=…)` (node wire) gets
     `min(acquire_timeout_s, remaining _CP_REQUEUE_TOTAL_CAP_S)` — a
     `NodeCommandTimeout` attempt can spend up to `acquire_timeout_s` per attempt,
     so without this the worst case would be ≈ `acquire_timeout_s × attempts`.
   `queue_timeout_s` remains a **total** admission-wait budget across re-admits too
   (pass its remainder each attempt). Give up when either budget or the attempt
   bound is hit. (For the target saturation case the node's own ~31 s create-retry
   makes each attempt fast, so the cap rarely binds; it caps the pathological
   all-timeout / long-queue-wait case.)
5. **Bound.** `_CP_REQUEUE_MAX` re-admits (propose 3). Give up (re-raise the last
   saturation error) at the bound, when `queue_timeout_s` is exhausted, or when
   `_CP_REQUEUE_TOTAL_CAP_S` wall-clock is hit.
6. **Cleanup.** Each failed attempt releases its placement (return capacity) before
   re-admitting; the final failure runs the existing fleet/placement cleanup +
   re-raises. The opener's `_fleet_opening` mark (set once at classification) is
   cleared by the existing `except` on final failure.
7. **Ambiguous timeout / late orphan (corrected per audit #2).** A create that
   timed out may have actually spawned a container on the failed node. The node's
   own `_reap_rollout_orphans` reaps by the `xrlenv.rollout_id` label but only on
   that node's *own* next retry — it does NOT cross to the re-admit's new node. A
   first-node orphan left when the CP re-admits elsewhere is cleaned by the
   **node-only raw-GC reconciler**, which lists raw containers by `container_id`
   under `xrlenv.session_kind=raw` and reaps any not matching a live coordinator
   session (there is no CP "by-rollout-id sweep"). If the failed acquire never
   returned a `container_id`, the coordinator has no row to attribute — the orphan
   is still reaped by container id, just without row attribution. Accepted +
   documented.

## audit result — ALL RESOLVED (see decisions above)

Reviewing the design against the current coordinator, admission queue, scheduler,
and raw-GC code paths left the direction sound, with three changes needed before
implementation. All three are now folded into the decisions above:

1. **Queued exclusion needs to be stronger.** Passing `exclude_node_ids` only to
   the admission fast path is not enough. If node A fails and node B is
   temporarily full, `AdmissionQueue.acquire` will enqueue without the exclusion;
   the later drain can then place back onto node A. Preserve the exclusion in the
   `_Waiter` when at least one non-excluded capable node exists, and relax it only
   for the true single-node / all-excluded case where waiting for the same node to
   drain is the intended fallback. The two-sysbox-node overload case needs this
   explicitly: A fails, retry excludes A and lands on B; if B also fails, both
   qualified nodes are now excluded. At that point the correct behavior is NOT to
   spin or permanently hard-exclude the whole sysbox pool. Release B's placement,
   relax the exclusion because every capable node is in the failed set, and queue
   normally so scheduler health/AIMD/capacity signals decide when either node is
   safe to try again, still bounded by `_CP_REQUEUE_MAX` and the total budgets.
   → **RESOLVED (decision 3):** the exclusion is stored on the `_Waiter` (drain
   honours it); the coordinator computes `effective_exclude` per attempt via
   `Scheduler.capable_node_ids(...)` and relaxes to ∅ exactly when `failed`
   covers all capable nodes (both-fail and single-node cases), so no permanent
   pool-wide hard-exclude.
2. **Raw-GC orphan wording is inaccurate.** The node-local retry reaps by the
   `xrlenv.rollout_id` label, but the control-plane raw-GC path lists raw
   containers by `container_id` under `xrlenv.session_kind=raw` and diffs those
   against coordinator sessions. A first-node orphan from an ambiguous create can
   still be reaped as node-only, but not via a CP "by-rollout-id sweep", and not
   with row attribution if the failed acquire never returned a `container_id`.
   Document this as node-only raw-GC cleanup by container id.
   → **RESOLVED (decision 7):** reworded to node-only raw-GC reconciliation by
   `container_id` under `xrlenv.session_kind=raw`; no CP by-rollout-id sweep; no
   row attribution when no `container_id` was returned.
3. **The bound does not cap node wire time.** `queue_timeout_s` only bounds
   admission wait across re-admits. A `NodeCommandTimeout` attempt can still spend
   the full `acquire_timeout_s` wire budget per attempt, so the default worst case
   is roughly `acquire_timeout_s * attempts` plus queue wait. Either document this
   explicitly as accepted, or add a separate total acquire wall-clock cap.
   → **RESOLVED (decision 4):** added `_CP_REQUEUE_TOTAL_CAP_S`, a total wall-clock
   cap on the **re-admit phase** (armed on the first saturation failure, not t=0)
   covering **both** queue wait and node wire. Each
   attempt clamps its admission-wait `timeout_s` to
   `min(remaining queue_timeout_s, remaining cap)` and its node-wire
   `acquire_timeout_s` to `min(acquire_timeout_s, remaining cap)`, so the worst case
   is the cap — not `acquire_timeout_s × attempts`, and not an unbounded queue wait
   on the surviving pool via the `_Waiter` exclusion.

With those adjustments, the design fits the existing invariants: classify fleet
membership once, re-place only non-fleet / opener acquires, release every abandoned
placement before retrying, and keep final failure cleanup on the existing
coordinator exception path.

## Blast radius

- `xrlenv/control/scheduler.py` — `place`/`_place_impl` gain `exclude_node_ids`;
  new read-only `capable_node_ids(backend, container_runtime)` for the coordinator's
  relax decision.
- `xrlenv/control/admission.py` — `acquire` gains `exclude_node_ids`, threaded to
  BOTH the fast-path `place()` and the queued `_Waiter` (audit #1).
- `xrlenv/control/raw_container_service.py` — classifier + the re-admit loop
  (relax when the failed set covers all capable nodes; total wall-clock cap that
  clamps BOTH each attempt's admission-wait `timeout_s` and node-wire
  `acquire_timeout_s` to the remaining budget).
- No node-side change. No wire/proto change.

> **Audit (in `## audit result` above) — resolved.** #1 → decision 3 (waiter carries
> the exclusion; coordinator relaxes only when failed = all-capable, via
> `capable_node_ids`). #2 → decision 7 (corrected to node-only raw-GC by container
> id). #3 → decision 4 (added `_CP_REQUEUE_TOTAL_CAP_S` wall-clock cap; per-attempt
> wire clamped to the remainder).

## Tests (required)

1. **A→B rebalance.** Saturation on node A → re-admit lands on node B, A explicitly
   excluded from the second `place()`.
2. **Both-fail relax (two nodes).** A fails → excludes A → B fails → `failed={A,B}`
   = all capable → `effective_exclude` relaxes to ∅ and the acquire queues normally
   (does NOT hard-exclude the whole pool / spin).
3. **Single-node wait-or-bound.** One sysbox node → all-capable-excluded from
   attempt 1 → relaxes and waits (queue) or stops at `_CP_REQUEUE_MAX`, WITHOUT
   leaking a `_pending` scheduler reservation (placement released every attempt).
4. **Terminal errors propagate.** 4xx / name-conflict / image-not-found /
   `BackendCapabilityMissing` → propagates unretried (classifier returns False).
5. **Budgets are total.** `queue_timeout_s` is total across attempts (not
   per-attempt); the re-admit phase (armed on the first failure) stops at
   `_CP_REQUEUE_TOTAL_CAP_S` wall-clock,
   which clamps BOTH each attempt's admission-wait `timeout_s`
   (`min(remaining queue_timeout_s, remaining cap)`) and its node-wire
   `acquire_timeout_s` (`min(acquire_timeout_s, remaining cap)`) — so a queued
   `_Waiter` on the surviving pool cannot outlast the cap.
6. **Fleet-opener retry.** A re-admitted opener does not double-mark
   `_fleet_opening`; on final failure it is cleared and no footprint/`_pending`
   leaks.
7. **Scheduler exclusion unit.** `place(exclude_node_ids=…)` drops the node;
   all-capable-excluded → `CapacityExhausted` (not `BackendCapabilityMissing`);
   `capable_node_ids(backend, runtime)` returns the right set.
8. **Ambiguous timeout / late orphan** is reaped by the node-only raw-GC
   reconciler by `container_id` under `xrlenv.session_kind=raw` (explicitly
   accepted; no CP by-rollout-id sweep).

## Implementation landed (2026-07-07)

Shipped exactly as designed. Three files, no node/wire/proto change:

- `xrlenv/control/scheduler.py` — `place()`/`_place_impl()` gain
  `exclude_node_ids: frozenset[str] | None`; the exclusion filter runs AFTER the
  backend + runtime capability checks (so empty-before-exclude →
  `BackendCapabilityMissing`, emptied-only-by-exclude → `CapacityExhausted`).
  New read-only `capable_node_ids(backend, container_runtime)` for the relax
  decision.
- `xrlenv/control/admission.py` — `acquire()` gains `exclude_node_ids`, threaded
  to the fast-path `place()` AND stored on the queued `_Waiter` (new field) and
  re-passed on EVERY drain retry.
- `xrlenv/control/raw_container_service.py` — module-scope classifier
  `_is_retriable_acquire_saturation` (+ `_SATURATION_MARKERS`,
  `_TERMINAL_ACQUIRE_ERRORS`) and constants `_CP_REQUEUE_MAX = 3`,
  `_CP_REQUEUE_TOTAL_CAP_S = 180.0`. `acquire()` wraps the place+preflight+create
  attempt in the re-admit loop; fleet role is classified ONCE before the loop.
  The pure-capability probe is computed LAZILY (first failure only), so the
  happy path pays nothing and needs no new scheduler surface. Each attempt clamps
  BOTH admission-wait `timeout_s` and node-wire `acquire_timeout_s` to the
  remaining cap (armed only on the first saturation failure, so a normal
  first-attempt queue wait keeps its full `queue_timeout_s`). The `_SchedulerProtocol`
  gained `capable_node_ids` + `exclude_node_ids` on `place`.

Tests: `tests/unit/control/test_raw_readmit_saturation.py` (16 tests — classifier
matrix, A→B rebalance, both-fail relax, single-node bound-no-leak, terminal
propagate ×3, total-cap give-up + node-wire clamp, fleet-opener re-admit + all-fail
cleanup, and the four scheduler-exclusion units). Full `tests/unit/control` suite:
982 passed. Test 8's node-only raw-GC-by-`container_id` reaping is covered by the
existing `test_raw_gc_reconciler.py::test_node_only_orphan_force_destroyed`.
