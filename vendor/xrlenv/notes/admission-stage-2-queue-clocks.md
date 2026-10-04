# Stage 2 — separate queue-wait from run-time + queued feedback

Slice plan for Stage 2 of `notes/admission-capacity-design.md` (pillars
**P2** + **P3.1**). Stage 1 made the cluster *observable*; Stage 2 makes
*queueing honest* — a job is never failed for *waiting*, and a waiting
job is visible instead of a silent block.

## Goal

After Stage 2: a request that can't be placed immediately waits in the
admission queue **without** consuming any run-time deadline, by default
for as long as it takes; the consumer sees live "queued — position N of
M, est. wait …" feedback while it waits; "fail fast if not admitted
within N" becomes an explicit opt-in rather than the default.

## Scope

**In:** P2 — confirm/enforce that run-time deadlines start at admission,
not submit; flip the `queue_timeout_s` default to effectively-unbounded;
make a small `queue_timeout_s` the explicit fail-fast opt-in. P3.1 (poll
variant) — a `QueueStatus` RPC + a consumer-side poller that emits live
queue feedback.

**Out:** the health-derived admission *controller* (Stage 3 — Stage 2
does not change *what* is admitted, only how waiting is treated and
shown); making `AcquireContainer` itself server-streaming (see D1).

## Decisions within Stage 2

- **D1 — poll, not a streaming `AcquireContainer`.** The design brief
  named server-streaming as the target with a poll fallback "accepted
  for v1". Stage 2 takes the **poll** path: keep `AcquireContainer`
  unary, add a small unary `QueueStatus` RPC the consumer polls *while*
  its acquire blocks. The drop-in already drives acquire on an event
  loop (`_run_sync`), so the poller is a concurrent **asyncio task** on
  that loop — no thread, no change to the `AcquireContainer` shape, far
  less blast radius than restructuring the acquire RPC. Streaming stays
  a possible future optimisation; it is not needed for honest feedback.
- **D2 — default queue-wait + the knob.** `queue_timeout_s` stays a
  consumer-facing parameter — it is already exposed on
  `Client.acquire_container(...)` and the drop-in (the
  `xrlenv.queue_timeout_s` kwarg / reserved label). Stage 2 (a) flips
  its default to **exactly 24 h (86400 s)** — a backstop so a forgotten
  run can't leak a waiter forever, not literal infinity — and (b) makes
  a *small* caller value the explicit fail-fast opt-in. Today there are
  two *intentional* per-path finite defaults — case-1 rollouts 300 s
  (`coordinator.py`, the `else 300.0`) and raw containers 3600 s
  (`RawContainerCoordinator.acquire`, deliberately larger) — plus a
  vestigial `AdmissionQueue.acquire(timeout_s=300.0)` that never takes
  effect (both callers always pass `timeout_s`). P2 supersedes all
  three with the single 86400 s backstop; this is a *consolidation
  under one policy*, not a bug fix, and tidies the dead signature
  default in passing. The knob, its 24 h default, and the
  small-value-means-fail-fast semantics are documented **explicitly**
  in `docs/developer_guide/timeouts.md`.
- **D3 — the pollable handle.** A queued consumer must be able to name
  *its* request. The consumer supplies a `request_id` (UUID) in the
  `AcquireContainerRequest`; `QueueStatus(request_id)` returns that
  request's `{position, queue_depth, est_wait_s, state}`. (Reuse the
  request's existing idempotency-key header if one is already carried —
  confirm during implementation.)
- **D4 — run-clock start.** Audit/confirm that `session_deadline_s` and
  the exec hard timeout already start at session creation (post-admission,
  post-pull) — they appear to (`RawContainerSession.deadline_at` is set
  when the session is created). If so, P2's run-clock half is a
  *verification + a regression test*, not a refactor; the concrete code
  change is D2 (the default) + D3/poll.

## Deliverables

### 2a — clock separation + `queue_timeout_s` default (P2)

- `RawContainerCoordinator.acquire` / `AdmissionQueue` — one consistent
  86400 s (24 h) `queue_timeout_s` default; a small caller value still
  works as fail-fast; drop the vestigial signature default.
- Confirm (and pin with a test) that `session_deadline_s` + exec hard
  timeout start at admission, not at request submit — queue-wait must
  not erode them.
- The control-plane "admit-queued" WARN already fires when a request
  waited; keep it.
- **Documentation deliverable:** `docs/developer_guide/timeouts.md` —
  update the `queue_timeout_s` entry to state the 24 h default, that it
  is a consumer-set knob on `acquire_container` / the drop-in, and that
  a small value opts into fail-fast. This is an explicit Stage-2
  acceptance item, not an afterthought.

Files: `control/raw_container_service.py`, `control/admission.py`,
defaults in `client/transport.py` / `client/client.py`,
`docs/developer_guide/timeouts.md`.

### 2b — `QueueStatus` RPC + control-side introspection (P3.1 wire)

- `AdmissionQueue` — track per-request enqueue order so it can answer
  `position` (rank in the FIFO) and `queue_depth`; a coarse `est_wait_s`
  from recent drain rate.
- proto: a `QueueStatus(QueueStatusRequest{request_id})` →
  `QueueStatusResponse{position, queue_depth, est_wait_s, state}` unary
  RPC on `rollout_control.proto`; `AcquireContainerRequest` carries the
  consumer-supplied `request_id`. Regenerate `_pb2`.
- control: the servicer answers `QueueStatus` from the `AdmissionQueue`.

Files: `api/proto/rollout_control.proto` + regen, `control/admission.py`,
`control/rollout_endpoint.py` (or wherever the RPC servicer lives),
`control/grpc_endpoint.py`.

### 2c — consumer-side live queue feedback (P3.1 surface)

- `Client` / `transport.py` — generate a `request_id` per acquire; while
  the acquire RPC is in flight, run a concurrent poller task hitting
  `QueueStatus` every few seconds.
- The drop-in (`compat/docker_client.py`) surfaces each poll as a live
  stderr line — "queued: position 34/64, est. wait ~90s" — so the
  over-request realisation lands in the operator's terminal in real
  time. The existing atexit admission summary stays as the wrap-up.

Files: `client/client.py`, `client/transport.py`,
`compat/docker_client.py`.

## Tests

- `admission.py` — `position` / `queue_depth` correctness as requests
  enqueue/drain; a small `queue_timeout_s` still fails fast; the large
  default does not fail a long waiter.
- run-clock: a request that waited in the queue gets its *full*
  `session_deadline_s` from session creation (queue-wait not subtracted).
- `QueueStatus` RPC round-trips position/depth for a queued request.
- drop-in: a queued acquire emits at least one live "queued" line and
  still succeeds once admitted.

## Acceptance

- A consumer requesting more concurrency than the cluster carries sees
  live per-request queue position in its terminal, and every job
  eventually runs — none failed for *waiting* — on the default settings.
- A consumer that sets a small `queue_timeout_s` still fails fast.
- `gen_protos.sh` clean; full unit suite + `ruff` green.

## Suggested commit breakdown

1. 2a — clock separation + `queue_timeout_s` default flip.
2. 2b — `QueueStatus` RPC + `AdmissionQueue` position/depth introspection.
3. 2c — consumer-side poller + drop-in live queue feedback.
