# Design: recover from transient node-saturation create failures

**Status:** IMPLEMENTED (2026-07-07), commit `3b06b58` + audit follow-up. The two
opening sections below are the original problem statement (pre-commit code paths,
kept as history); the "Recommendation" section onward is the shipped record. See
spec 04 (§node-agent acquire retry semantics) for the durable operator/developer
reference and `notes/deferred_audit_todos.md` (D-item) for the B follow-on.
**Trigger:** TerminalWorld sysbox sweep at `--max-workers 4` — several acquires died with
`containers.run … 500 … failed to pre-register with sysbox-fs: rpc error: code =
DeadlineExceeded … RST_STREAM … CANCEL`. That's a **transient** gRPC timeout inside sysbox
(sysbox-fs momentarily overloaded by concurrent registrations), not a hard ceiling — a retry
seconds later succeeds.

## The gap (evidence, exact code)

xrlenv already *recognizes* this as node saturation but **kills the victim task** instead of
retrying/deferring it:

- **Node** `RawContainerManager.acquire` (`node/raw_container.py:886-919`): the create runs
  inside `_create_gate()` (a per-node semaphore, `create_concurrency=4`); on
  `DockerException/RequestException` it (1) feeds the AIMD limiter iff
  `_is_node_health_error` (5xx/timeout/transport — `:242`), (2) releases the reserved cores,
  then (3) **re-raises** a translated `XRLEnvError`. The task fails.
- **AdmissionQueue** (`control/admission.py`) re-queues **only on `CapacityExhausted`** from
  `scheduler.place()` (`:220`, `:467`) — a *proactive* "no slots" signal. A *reactive*
  create-time 5xx never enters the queue.
- **AIMD** (`HealthAimdController`, `control/capacity.py:535`) lowers the node's admission
  limit **reactively** via a ~15 s background loop, edge-triggered on *new* errors — it helps
  *future* acquires and lags a burst.

Net: the failure feeds the limiter (future throttle) but the task that hit it dies. There is
already a precedent for retrying a create — the **409 name-conflict** path
`_run_with_name_reclaim` (`:950`) reclaims the name and retries once. There is no equivalent
for a transient saturation 5xx.

Two things are missing: **(1) no retry** of a transient, saturation-recognized failure;
**(2) no sysbox-aware create bound** — the `create_gate` cap (4) is tuned for plain docker,
but sysbox-fs pre-register is far slower, so 4 concurrent sysbox creates overwhelm it.

## Options

### A — Node-side bounded retry-with-backoff  *(recommended, recovery)*
On `_is_node_health_error` create failures, retry the create up to N times with exponential
backoff + jitter, capped by the acquire's remaining deadline. Mirrors the 409 retry. Each
attempt re-enters `_create_gate` (released during backoff so other acquires proceed).
- **+** smallest blast radius, node-local, precedent exists, directly fixes the transient
  case, correct on the current single-sysbox-node pool, no control-plane change.
- **−** retries the *same* node (no rebalance); consumes the acquire's deadline budget during
  backoff.

### B — Control-plane re-admit through the queue  *(heavier, cross-node)*
Coordinator catches a node-health acquire failure and re-submits through `AdmissionQueue`,
which re-places (possibly a *different* node, now AIMD-throttled).
- **+** rebalances across nodes; reuses the queue + AIMD.
- **−** cross-layer (coordinator + admission + error classification); can re-pick the same
  saturated node until AIMD catches up (so it *still* needs a retry cap + backoff); much more
  surface + tests. Overkill while there's one sysbox node.

### C — Sysbox-aware create-concurrency cap  *(recommended, prevention)*
A lower/separate create semaphore for non-`runc` (sysbox) acquires — e.g. 1-2 — because
sysbox pre-register is much slower than a plain runc create. Cuts the failure rate at source.
- **+** prevention; simple; complements A.
- **−** a knob; a failure that still slips through needs A to recover.

## Recommendation: **A + C**  — DECIDED 2026-07-07, IMPLEMENTED 2026-07-07

Node-side bounded retry (A, recovery) + a sysbox-aware create cap (C, prevention). Both are
node-local, minimal blast radius, and match how xrlenv already thinks about create pressure
(the `create_gate` and AIMD). Landed in `xrlenv/node/raw_container.py` (`_create_with_retry`,
`_reap_rollout_orphans`, `_is_retryable_create_error`, sysbox `_create_gate`), wired through
`NodeAgentConfig.raw_sysbox_create_concurrency` → `cli.py` (`XRLENV_RAW_SYSBOX_CREATE_CONCURRENCY`).

**B is the planned follow-on** once there are **multiple dedicated sysbox nodes** — cross-node
re-admit only pays off then, and it reuses A's retry loop. Tracked, not built now.

### Decided parameters (final — these are what shipped)

```
_HEALTH_RETRY_MAX         = 5     # retries (6 attempts total)
_HEALTH_RETRY_BASE_S      = 1.0   # → 1,2,4,8,16 ≈ 31s worst-case
_HEALTH_RETRY_CAP_S       = 30.0  # per-retry ceiling
_HEALTH_RETRY_TOTAL_CAP_S = 45.0  # hard wall-clock ceiling across the whole series
raw_sysbox_create_concurrency = 1 # C — separate, tighter cap for non-runc creates
```

Backoff: `wait_i = min(BASE·2^i, CAP)` jittered **down** ×0.75–1.0 (so no single wait exceeds
the cap and concurrent retriers desynchronise). Base 1s over 5s: base 5s pure-exponential
totals 5+10+20+40+80 = **155s** (too large); 1s totals **31s** AND fires the first retry in
~1s (sysbox-fs usually clears in <1–2s), so the common case recovers faster.

### The seven review findings — how each was settled

1. **Retry policy** — hard-coded constants above (not config knobs); config can come later if
   a node needs to tune it. `max=5 / base=1s / factor=2×`, jittered down.
2. **AIMD accounting** *(corrected from the earlier "on final give-up" note — that was wrong)*
   — record **one** health error per acquire that hit **any** `_is_node_health_error` fault,
   not per attempt and not only on give-up. A create that retries **then succeeds** still
   records one signal (the node *was* saturated → future admits should throttle). A
   down-daemon ConnectionError records (feeds AIMD) even though it is not retried. AIMD is
   edge-triggered; N records ⇒ N halvings ⇒ floor from one burst — hence exactly one.
3. **Retryable set** — a **narrower** `_is_retryable_create_error` (5xx / timeout only) drives
   the *retry* decision; the broader `_is_node_health_error` still drives *AIMD*. A clean
   dead-daemon `ConnectionError` is therefore **not** retried in place (a 31s backoff can't
   revive a down daemon; fail fast so the CP marks the node unhealthy) but **does** throttle
   admission. 4xx (409/404) never retried.
4. **Duplicate-container leak on ambiguous timeout** *(new High from review)* — a timed-out /
   5xx'd create may have actually spawned the container without the client seeing the reply.
   Every raw container always carries a unique `xrlenv.rollout_id` label (set unconditionally
   in `acquire`, even when no `name` is passed), so before each retry `_reap_rollout_orphans`
   force-removes any container wearing this acquire's label. **Fail closed (audit P2):**
   `_reap_rollout_orphans` returns whether the node is *confirmed* clean; if the list call
   fails or a found orphan won't remove, the retry loop does NOT create again (which would
   stack a duplicate on a possibly-live container) — it surfaces the last transient and the
   caller re-submits with a fresh rollout_id. Backstops: the create's 409 name-reclaim and the
   coordinator's raw-GC by-label sweep.
5. **Sysbox cap wired operationally** — `raw_sysbox_create_concurrency` (default 1) on
   `NodeAgentConfig`, threaded into `RawContainerManager` in `agent.py`, tunable via
   `XRLENV_RAW_SYSBOX_CREATE_CONCURRENCY` in `cli.py`. `0` falls back to the general cap.
6. **Deadline (audit P2 — now threaded, incl. the default).** The CP stamps the *effective*
   acquire wire budget onto `pull_deadline_s` for **every** remote acquire — the caller's
   explicit `acquire_timeout_s` when given, else `DEFAULT_ACQUIRE_TIMEOUT_S = 600s` (the same
   value its `_send_and_wait` ceiling enforces). The node reads it back as
   `ensure_image_deadline_s`, which now bounds both the image pull and the create retry. The
   retry total is bounded FOUR ways: attempt count, per-retry cap, the hard wall-clock total
   (`_HEALTH_RETRY_TOTAL_CAP_S = 45s`), AND the caller's remaining wire budget — a deadline
   anchored at acquire entry (before the pull eats into it) and passed into `_create_with_retry`.
   A fail-fast caller fails fast on the node too, never retrying past the point the control
   plane already timed out the command. (Follow-up fix: the *default* acquire previously reached
   the node as `pull_deadline_s == 0` → no node deadline; the CP now always stamps the effective
   value. `DEFAULT_ACQUIRE_TIMEOUT_S` matches the node's default pull timeout, so this is
   behaviour-preserving for the pull and only adds the retry deadline. A purely local in-process
   acquire has no wire deadline and is bounded by the 45s hard cap alone.)
7. **Observability** — `health_snapshot` now sums **both** create gates (general + sysbox), so
   a sysbox-only bottleneck no longer reads as an idle node; each retry logs a warning and the
   give-up surfaces the last transient (not silent latency).

## Blast radius + touchpoints

- **Code:** `xrlenv/node/raw_container.py` (retry loop, orphan reap, sysbox gate, snapshot),
  `xrlenv/node/agent.py` + `xrlenv/node/cli.py` (the sysbox-cap knob). No control-plane/
  admission change for A+C.
- **Specs:** a note in **04** (node agent — acquire retry semantics). 03/10 (admission/
  capacity) unaffected — this is node-local recovery, not a queue change.
- **Backlog:** add a D-item in `notes/deferred_audit_todos.md` for the B follow-on.
- **Tests:** unit — (a) 500-then-success ⇒ retry succeeds; (b) persistent 500 ⇒ gives up
  after N with **one** AIMD error recorded + cores released; (c) 409 / 404 not retried;
  (d) clean ConnectionError feeds AIMD but is not retried; (e) reap-before-retry removes a
  rollout_id orphan; (f) total wall-clock cap honored; (g) sysbox cap serializes non-runc
  creates + `health_snapshot` counts the sysbox gate.
