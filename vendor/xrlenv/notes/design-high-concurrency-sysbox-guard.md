# Design — transparent high-concurrency guard for capacity-capped runtimes

**Status:** implemented 2026-07-08 (harbor plug-in only; no xrlenv-core change).
**Companion to:** `design-per-node-runtime-concurrency-cap.md` (the server-side
cap this guard makes usable) and `design-cp-readmit-saturation.md` (create-time
saturation, a different layer).

## Problem

The per-node sysbox concurrency cap (`nodes.yaml
max_concurrent_by_runtime: {sysbox-runc: 4}`) correctly bounds sysbox-fs load:
overflow acquires hold in the admission queue until a slot frees. That is right
for a **patient** consumer — a trainer using the xrlenv SDK blocks in the queue
(24 h default) and its acquire simply lands when capacity appears.

But the **harbor** benchmark harness is *impatient*: it wraps environment setup
(the acquire **plus** the agent install that follows) in a hard
`asyncio.wait_for` — `_AGENT_SETUP_TIMEOUT_SEC = 360 s`. At high request
concurrency the sysbox queue is deep: with cap = 4 and N queued sysbox tasks, the
last waits ~(N-4)/4 x task-time for a slot — easily > 360 s. When the acquire
out-waits harbor's window, harbor **cancels** it, surfacing a non-retriable
`TimeoutError`, and the trial fails.

The operator's only lever was to hand-lower `MAX_WORKERS` (we ran the single-node
sweep at 8, not 16/32). That is exactly wrong: **the consumer should not need to
know the cap, or that sysbox even exists.** Guarding a high-concurrency request
against a capped runtime is xrlenv's job, not the caller's.

## Decision

Fail-fast at the acquire + let harbor's own trial-retry loop absorb the wait.
Two plug-in-local changes, no xrlenv-core surface touched:

1. **Adapter fails fast below harbor's setup window.**
   `xrlenv_plugins/harbor/environment.py` passes
   `queue_timeout_s = _ACQUIRE_QUEUE_TIMEOUT_S` (default **240 s**, env-tunable via
   `XRLENV_HARBOR_ACQUIRE_QUEUE_TIMEOUT_S`) to `acquire_container`. An at-cap
   acquire now raises **`CapacityExhausted`** at ~240 s — comfortably before
   harbor's 360 s cancel, and with headroom left for the post-acquire
   image-pull / container-start / agent-upload.

2. **The trial queue retries — but ONLY infra-transient errors.**
   `run_oracle_sweep.py` builds
   `RetryConfig(max_retries=<--retries, default 6>, include_exceptions=
   _INFRA_RETRY_EXCEPTIONS, min_wait_sec=2, wait_multiplier=1.0, max_wait_sec=10)`.
   `_INFRA_RETRY_EXCEPTIONS = {CapacityExhausted, ControlPlaneLost, NodeLost,
   NodeCommandTimeout}`. (Also fixes a latent bug: the config previously passed
   `max_attempts=`, not a real field — pydantic dropped it, so retries were always
   0 regardless of `--retries`.)

The caller now requests any `MAX_WORKERS`; xrlenv paces the capped runtime
transparently.

## Why this doesn't pollute the eval or waste work

Both were the reviewer's first questions, and both fall out of *where* the
fail-fast sits — the acquire is harbor's **first** setup step
(`_start_agent_environment → start() → acquire_container`), before any container,
image pull, agent install, or verifier:

- **No wasted setup.** A `CapacityExhausted` retry re-attempts only the acquire —
  the queue wait. Zero container/agent work is redone. Near-constant 2 s backoff
  keeps the trial back in the admission queue quickly (a long backoff is dead time
  where it isn't competing for a freed slot); the 240 s in-queue budget dominates,
  so the trial spends ~99 % of its time actually queued.

- **No eval pollution.** The task body runs **exactly once** — the attempt that
  finally gets a slot. Harbor reports that final attempt's result, so a
  waited-then-passed trial is a clean reward-1.0 pass in the tally,
  indistinguishable from a first-try pass; the retry counter is the only trace.
  The guard rail is the include-set: retries fire on **capacity/infra** only.
  Task-content failures (`AgentTimeoutError`, verifier errors) are *out* of the
  set — and are in harbor's default `exclude_exceptions` too — so a genuinely
  failed task is **never** re-rolled into a fluke pass. That is the real pollution
  risk, and it is closed by construction.

## Sizing

Default `--retries 6` → 7 attempts x ~240 s queue budget ≈ 28 min of slot-wait
coverage — enough for a single cap-4 node carrying the ~23 sysbox tasks in the TW
corpus. A run with no capacity pressure consumes 0 retries. Operators who raise
harbor's setup timeout (`agent_setup_timeout_multiplier`) should raise
`XRLENV_HARBOR_ACQUIRE_QUEUE_TIMEOUT_S` in step, keeping it below the resulting
window so `CapacityExhausted` still beats the cancel.

## Rejected alternatives

- **Generous fixed setup timeout** (bump `agent_setup_timeout_multiplier` so the
  acquire just blocks in-queue). One line, matches xrlenv's patient-queue design,
  but detects a genuinely-down node slowly (waits the full inflated window) and
  buries a real capacity signal. Kept as the env escape hatch, not the default.
- **Client-side sysbox throttle** (process-level semaphore in the adapter). Cannot
  help: the semaphore wait is *inside* harbor's setup timeout, and the queue is
  inherently deep (23 tasks / 4 slots) — the last task still waits ~17 min no
  matter how submission is paced. Throttling doesn't change the physics; only
  more slots or a longer effective wait budget does.

## Tests

`benchmarks/terminalworld/tests/test_run_oracle_sweep.py` pins: `--retries` maps
to `max_retries` (regression guard for the dropped-kwarg bug), infra errors
retry / task-content errors never do (mirrors harbor's gate), the include-set is
disjoint from harbor's task-content exclude-set, and the adapter constant's
default + env override.

## Follow-up — conc-32 hardening (2026-07-08)

Pushing the single-sysbox-node dev cluster to **conc-32** (the caller shouldn't
have to hand-lower it — that was the whole premise) surfaced two more infra bugs
the conc-8 run masked. Both are now fixed; the TW full sweep went **181/187 →
187/187 at conc-32 with 0 infra exceptions**, no wedge, no leak.

1. **CP re-admit budget starved the committed create deadline** (commit b3356fd).
   The re-admit loop (`_CP_REQUEUE_TOTAL_CAP_S=180`) clamped BOTH the admission
   wait AND the node-wire `acquire_timeout` to the remaining budget. Right for the
   WAIT, wrong for a committed `docker create`: after a long sysbox queue-wait the
   create got ~30 s and timed out mid-create (`NodeCommandTimeout`; tw_650591 25 s,
   tw_709166 30.2 s). harbor allows 600 s for env-build, so starving the create was
   self-inflicted. Fix: floor the committed-create deadline at
   `_MIN_CREATE_DEADLINE_S=180` (capped at an explicit caller `acquire_timeout_s`);
   the cap still bounds placement RE-TRIES, never the committed create. Also:
   `NodeCommandTimeout` now survives gRPC rehydration (client `_KIND_TO_EXC` +
   server `_EXC_TO_CODE`→DEADLINE_EXCEEDED) — it was arriving as a bare
   `XRLEnvError` ("gRPC error UNKNOWN"). NOT made retriable (a timed-out create may
   have quietly succeeded → orphan; prevent, don't retry).

2. **Sysbox DESTROYS weren't serialized like creates** (commit 5d4f19c). The big
   one. `raw_sysbox_create_concurrency=1` protects sysbox-fs's *register* step on
   create, but destroys went through the general destroy gate (concurrency 4) → 4
   concurrent sysbox-fs FUSE *unmounts* (`fusermount3`) under churn wedged the
   daemon exactly like unthrottled creates. The wedged `docker rm` hung in D-state
   and the container LEAKED (Up ~57 min, holding a cap slot, dragging the whole
   sysbox layer → starving OTHER tasks past their agent/verifier timeouts —
   tw_526185/586787/582345/583114). Fix: the symmetric analog
   `raw_sysbox_destroy_concurrency=1` + `_sysbox_destroy_semaphore`;
   `RawContainerRecord` persists `container_runtime` so `destroy()` routes sysbox
   teardowns through it. This single fix cleared 4 of the 5 remaining failures.
   Insight: the per-node concurrent cap bounds RUNNING count; the create+destroy
   serialization bounds the FUSE-op RATE — both are needed under high churn.

3. **Residual content-flake → content-retry** (commit 069ae1e). With infra at 0
   exceptions, a rotating ~1-task CONTENT flake remained on the most
   nondeterministic tasks (gdb-backtrace unwind — ASLR-disable EPERM → non-det
   backtrace; DinD-verifier timing). `run_full_sweep.sh` (TW + tb2.1) now
   content-retries reward-0 tasks (`CONTENT_RETRIES=2`; solved if ANY attempt
   rewards 1.0; a persistent failure still fails the run) → deterministic green.
   tw_650591 also cpu-pinned (commit f004ea1). Node leaks from pre-fix runs were
   cleaned reboot-free via FUSE-abort + `docker rm -f` (see
   [[sysbox-fs-wedge-and-slurm-decoupling]] — no reboot).

Validated: TW **187/187 @ conc-32** (single pass, 0 retry rounds needed),
tb2.1 **88/88 @ conc-64** (1 retry round: adaptive-rejection-sampler +
torch-pipeline-parallelism), both 0 infra exceptions.
