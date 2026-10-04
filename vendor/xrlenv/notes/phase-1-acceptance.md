# Phase-1 Acceptance Smoke

> **Status**: phase-1-exit gate, not a slice deliverable.
>
> Phase-1 ships when **two** acceptance gates pass — a correctness
> gate (Slime-driven SWE-bench Verified smoke, same shape as
> phase 0) and a scale gate (SQLite-backed control plane
> sustaining **100-200 concurrent rollouts** under target
> latency). Both are operator-driven and **not** part of any CI
> run because they require manually-provisioned cloud VMs (the
> user has VM-only access on GCP + AWS — see `CLAUDE.md` "Cloud
> constraints"). The planning context for both gates lives in
> `notes/phase-1-to-do.md`. **Redis StateStore + the 500-1k+
> concurrent target moved to phase 2** on 2026-05-02 (Q4 revised);
> SQLite WAL handles the 100-200 envelope comfortably (~1-3k
> writes/sec ceiling).
>
> The phase-0 acceptance baseline (8/8 finished across 2 GCP nodes
> on 2026-04-30, recorded in `notes/phase-0-acceptance-results.md`)
> is the precondition: phase-1 starts where phase-0 ended.

## What "phase 1 ships" looks like

Phase-1 makes the platform **trainer-integrated and image-
distribution-aware** at the 100-200 concurrent envelope. Two
distinct signals together close the gate. (Higher-concurrency
load capability — Redis-backed control plane sustaining 500-1k+
rollouts — is phase-2 work, deferred 2026-05-02 because the
phase-1 design target is well within SQLite WAL's envelope.)

### Correctness gate — Slime-driven SWE-bench Verified

The platform carries trainer-driven rollouts end-to-end through a
real benchmark using the canonical Slime SDK shape. SWE-bench
Verified is the canonical phase-1 benchmark because:

1. It exercises Pattern A at scale (~500 per-instance images, each
   1–4 GB) — the same shape as terminal-bench-2 but pushing
   image-distribution and image-cache discipline far harder.
2. Its harness is pytest-driven (`tests/test_*.py` runs against
   the patched repo) — exercises the `in_sandbox_final reward.cmd`
   path with a real benchmark grader, not a synthetic shell test.
3. The trainer-side integration is **real** (Slime adapter is
   wired up); only the model is replaced with the gold-patch
   oracle for the gate. This validates the same plumbing a
   real-model run would use, while keeping the smoke
   reproducible without a model server.

The smoke demonstrates the trainer + platform end-to-end:

- The control plane boots on the operator's laptop with the
  default **SQLite WAL** state store (phase 1 stays SQLite-only;
  Redis lands in phase 2).
- Two `xrlenv-node serve` processes connect outbound from
  freshly-provisioned GCP VMs (one each), authenticated with
  the operator-issued node tokens (still bearer-token —
  mTLS lands in P1.x hardening).
- The trainer wraps `xrlenv/adapters/slime.py::make_rollout_fn`,
  with the policy mocked to return the gold-patch oracle's next
  shell action. So the *Slime adapter* is real (not stubbed);
  only the model is the oracle.
- 8 SWE-bench Verified instances run; the Pattern-A resolver
  overlays the per-instance image + resources from each
  instance's manifest.
- Image-affinity scheduling (D18) routes each instance to a node
  that already has its image; pre-flight image check (D19) trips
  cleanly with `reason="image_missing"` when configured to.
- All 8 rollouts seal as `finished` with non-null `final_reward`.
- The admin panel renders all 8 under `/rollouts`; `/images` view
  shows the per-node image tier histogram.
- `xrlenv events` shows clean lifecycle events; `xrlenv audit
  --kind auth.denied` returns 0 rows.

### Scale gate — SQLite-backed control plane at 100-200 concurrent

The platform sustains the load profile a real phase-1 training
run produces — 100-200 concurrent containers, comfortably within
SQLite WAL's ~1-3k writes/sec ceiling. The validation is split
across two sub-gates because control-plane bookkeeping load and
image-cache discipline at scale are orthogonal validations:

- **Gate 2a — bookkeeping at 200.** 200 concurrent `hello-shell`
  rollouts on the SQLite-backed control plane. Cheap mocks: the
  sandbox boots, runs N no-op steps, exits. Load is on
  state transitions / heartbeats / events / scheduling decisions /
  sandbox lifecycle, NOT on the sandboxes themselves.
- **Gate 2b — image-cache discipline at 32.** 32 concurrent
  SWE-bench Verified instances on the SQLite-backed control plane
  with image-cache eviction (D16) actively reclaiming disk.
  Smaller concurrency than 2a (per-instance images cost real
  disk), bigger per-rollout budget.

Both 2a and 2b run on the same SQLite StateStore; failing either
fails the scale gate. Phase-2 lifts both targets (500-1k+ on
Redis-backed control plane).

## Operator runbook

### Phase-1 prerequisites

Before either gate, the operator's cluster needs:

1. **A control plane VM** (or laptop) with:
   - The `xrlenv` package installed (or `pip install -e .` from
     a phase-1 checkout). State store defaults to SQLite WAL
     at `~/.xrlenv/state.db`; no separate service to provision.
   - `xrlenv up --grpc-host <bind> --grpc-port 50051` running.
2. **At least 2 GCP VMs** (or 1 GCP + 1 AWS) bootstrapped via
   `deploy/bring-up-node.sh`. Each VM:
   - Has the operator's node token (issued via `xrlenv tokens
     issue node` on the control plane) wired into the systemd
     drop-in (handled by `bring-up-node.sh`).
   - Has its operator user added to the `docker` group (handled
     by `bring-up-node.sh`'s `ensure_operator_docker_group`).
3. **For Gate 1 + 2b only**: SWE-bench Verified instance images
   built locally on each VM via the SWE-bench-Verified plug-in's
   `build-task-images.sh` (or pulled from the cluster's registry
   mirror if B8.3 is wired). The image-build script is idempotent
   and skip-if-tagged (carry-over from phase-0's `62b8078`).

### Gate 1 — Correctness gate driver

```bash
# On the laptop / control plane:
xrlenv up --grpc-host 127.0.0.1 --grpc-port 50051 &
xrlenv nodes  # both VMs STATUS=connected with seconds-fresh LAST_SEEN
xrlenv tokens issue consumer  # one-time

# Run the Slime-driven correctness gate:
export XRLENV_CONSUMER_TOKEN=$(cat ~/.xrlenv/secrets/consumer.token)
.venv/bin/python examples/slime/swebench_verified_smoke.py \
    --connect-host 127.0.0.1 --connect-port 50051 \
    --min-nodes 2 \
    --instances 8
```

Expected last line of stdout:

```
8 / 8 SWE-bench Verified rollouts sealed as finished across 2 node(s):
  ['<gcp-vm-A>', '<gcp-vm-B>']
```

### Gate 2a — Bookkeeping scale driver

```bash
# Same control plane as Gate 1.

# Spawn 200 concurrent hello-shell rollouts; sustain for 10 min.
.venv/bin/python examples/scale/bookkeeping_load.py \
    --connect-host 127.0.0.1 --connect-port 50051 \
    --concurrent 200 \
    --duration-s 600
```

Driver reports, every 30 s:

```
[t=  0.0s] in-flight=200 created=200 created_p50=0.18s p99=0.8s
[t= 30.0s] in-flight=200 finished=590 created_p50=0.17s p99=0.7s
...
[t=600.0s] in-flight=200 finished=11820 created_p50=0.18s p99=0.9s
PASS: created p50=0.18s (<1.0s), p99=0.9s (<5.0s),
      transient_pin_max=0s (<300s), sqlite_errors=0
```

### Gate 2b — Image-cache discipline driver

```bash
# Same control plane.

# 32 concurrent SWE-bench Verified instances rotating through
# more instance images than fit in the per-VM cache; eviction
# kicks in actively.
.venv/bin/python examples/scale/swebench_verified_load.py \
    --connect-host 127.0.0.1 --connect-port 50051 \
    --concurrent 32 \
    --total-instances 96 \
    --duration-s 1800
```

Driver expects:

- All 96 instances complete (some via cache hits, some after
  eviction-then-re-fetch).
- Image cache evicts least-recently-used **final** tags first;
  `<bench>-base/<task>` tags survive the eviction passes
  (validates A6/D16's tier ordering).
- No rollout fails with `reason="image_missing"` after the
  eviction pass (pre-flight check D19 either finds the image
  locally or routes to a node that has it via affinity D18).

## Pass criteria

### Gate 1 (correctness)

| Check | How to verify |
|---|---|
| 8 SWE-bench Verified rollouts seal `finished` | smoke prints "8 / 8 rollouts sealed as finished"; `xrlenv rollouts --template swebench-verified --status finished` shows 8 |
| Distribution across both VMs | smoke prints "across 2 node(s)"; per-rollout `node_id` shows split |
| Slime adapter is real (not stubbed) | code path: `xrlenv/adapters/slime.py::make_rollout_fn` is invoked; only the policy is replaced with the gold-patch oracle. Verifiable via stack-trace-on-`step()` or by toggling a `--policy=oracle` flag the driver exposes |
| Per-instance image overlay worked | `xrlenv events --rollout <id> --kind rollout.start` payload shows the per-instance `docker_image` from the SWE-bench Verified manifest |
| Image affinity verified | `xrlenv events --rollout <id> --kind placement.image_present` shows `true` for each rollout (event added in D18) |
| Pre-flight image check works | dedicated integration test: instance image only on one node, schedules to bare node, fails with `reason="image_missing"` (NOT `sandbox_create_failed`) |
| Node-token auth gated the bidi stream | run the CP with `XRLENV_AUDIT_AUTH_SUCCESS=1` (success auditing is OFF by default), then `xrlenv audit --kind auth.token_used --role node` shows ≥2 entries; `xrlenv audit --kind auth.denied` returns 0 rows |
| External plug-in package mechanism works | `xrlenv-swebench-verified` is `pip install`-able from a separate directory; the manifest registers in the catalog without source-tree edits (validates B11) |
| Trajectory viewer renders SWE-bench rollouts | open `/rollouts/<id>` in the admin panel; step list, actions, observations, gold-patch-oracle commands all visible |
| `/metrics` covers the rollout | `curl 127.0.0.1:9090/metrics \| grep xrlenv_rollouts_finished_total` shows 8 increments tagged `template="swebench-verified"` |
| `xrlenv nodes` shows live state | both VMs `STATUS=connected` with seconds-fresh `LAST_SEEN` (carry-forward of phase-0's `c795e1c` heartbeat-mirror fix) |

### Gate 2a (bookkeeping at 200)

| Check | How to verify |
|---|---|
| 200 concurrent rollouts sustained for ≥10 min | driver reports `in-flight=200` consistently across the full window |
| Median rollout-creation latency < 1 s | driver reports `created_p50` |
| 99p rollout-creation latency < 5 s | driver reports `created_p99` |
| No transient-state pinning >5 min | driver's periodic `xrlenv rollouts --status cancelling/finishing --age >300s` returns 0 rows throughout |
| SQLite StateStore correctness preserved | `xrlenv events --kind state.error` returns 0 rows; control-plane log free of `sqlite3.OperationalError` / `database is locked` traces (the latter would indicate WAL contention worth investigating) |

### Gate 2b (image-cache at 32, real per-image disk pressure)

| Check | How to verify |
|---|---|
| All 96 SWE-bench Verified instances complete | driver reports `finished=96`; `xrlenv rollouts --template swebench-verified --status finished` shows 96 over the 30-min window |
| Image-cache eviction tier ordering preserved | per-VM `docker images` snapshot at end: ≤K final `<bench>/<task>` tags (LRU), all `<bench>-base/<task>` tags still present (the base layer is the most expensive to recreate) |
| No `image_missing` after warm-up | first ~32 instances may pull/build (cold start); after that, every instance either hits the cache OR is routed to a node that has it via image-affinity. `xrlenv events --kind rollout.failed --reason image_missing --age <30min` returns 0 rows |
| No `sandbox_create_failed` from registry-pull errors | a regression of phase-0's `5a38e78` digest-fix would surface here; clean run = 0 rows |

## Honesty caveats — what the gates do and don't say

**Gate 1 — platform integrity ✓, model-eval ✗.** The Slime
adapter is real but the policy is the gold-patch oracle. Rewards
of 1.0 reflect what SWE-bench's gold patches accomplish through
XRLEnv's plumbing — a sanity floor and a platform-validity check,
NOT a model-eval result. To get an evaluation-valid score, swap
the oracle for a real model server in the smoke driver. The
platform's grader-isolation guarantees (D12 stage 1 — timing-
isolated verifier injection) hold regardless of policy choice.

**Gate 2a — bookkeeping load ✓, sandbox-resource load ✗.** 200
hello-shell mocks pressure the control plane's state machinery;
they do NOT pressure the data-plane's actual sandbox-creation
budget (network, disk, container start-up). Gate 2b validates a
slice of the data-plane pressure (32 concurrent + image-cache
eviction); higher-concurrency SWE-bench Verified validation is
post-phase-1 work alongside the Redis StateStore lift.

**Gate 2b — image-cache discipline at 32.** 32 concurrent
SWE-bench Verified instances pressures the image cache enough to
exercise the eviction tier without requiring the ~2 TB of image
storage a full-fleet validation would need. Higher concurrency
on real SWE-bench load lives in phase 2 alongside the Redis
StateStore + autoscale + durable trajectory storage work.

**Why 100-200, not 500-1k.** Original phase-1 plan targeted
500-1k concurrent on a Redis-backed control plane. Revised
2026-05-02 (Q4 in `notes/phase-1-to-do.md`): SQLite WAL handles
~1-3k writes/sec, so 100-200 concurrent is comfortably within
its envelope. Deferring Redis avoids shipping a second
state-store implementation just to validate a target the phase-1
deployment doesn't actually hit. Phase 2 lifts both the target
and the StateStore together when scale-out work justifies the
operational overhead.

**Caveats common to both:** acceptance is operator-driven (no CI
because cloud VMs are manually provisioned per CLAUDE.md
constraints); future acceptance runs follow the recipe in this
doc and the phase-0 baseline format in
`notes/phase-0-acceptance-results.md`.

## Current readiness — phase-1 progress tracker

(Populated as P1.x slices land. Today: all rows pending.)

| Phase-1 spec area | Slice | Status |
|---|---|---|
| Phase-0 carry-forward (D13/D15/D17/D21 + test debt) | P1.1 | ⏳ |
| External plug-in package mechanism (B11) | P1.1.5 | ⏳ |
| Image distribution architecture (A1: D18+D19+D20) | P1.2 | ⏳ |
| Image-cache eviction policy (A6 / D16) | P1.2 | ⏳ |
| ~~Redis StateStore (B6.1)~~ — **DEFERRED phase 2** | — | n/a |
| Slime adapter (B1.1) + sink-aware reader (B1.5) | P1.3 | ⏳ |
| verl adapter (B1.3) + sink-aware reader (B1.5) | P1.3 | ⏳ |
| `client.warmup(...)` SDK (B3.7) | P1.3 | ⏳ |
| `xrlenv-swebench-verified` external pip package (B2.1) | P1.4 | ⏳ |
| `EnvAdapter.enumerate_tasks()` contract (B9.3) | P1.4 | ⏳ |
| `userns-remap` default + UID separation (B5.4 + B5.7 / D12.2) | P1.4 | ⏳ |
| **Gate 1 — correctness (`examples/slime/swebench_verified_smoke.py`)** | P1.4 | ⏳ |
| **Gate 2a — bookkeeping load (`examples/scale/bookkeeping_load.py`)** | P1.5 | ⏳ |
| **Gate 2b — image-cache discipline (`examples/scale/swebench_verified_load.py`)** | P1.5 | ⏳ |
| Function-Call execution mode (B4.2) | P1.5 | ⏳ |
| Warm pools + ring buffer (B4.3 + B4.4) | P1.5 | ⏳ |
| ~~Active+standby control plane (B6.2)~~ — **DEFERRED phase 2** (lands with Redis) | — | n/a |
| OTel + rolling-file logs (B7.1 + B7.2) | P1.5 | ⏳ |
| Admin panel basic auth + 24h history + actions (B7.3 + B7.4 + B7.5) | P1.5 | ⏳ |
| Egress-allowlist networking + audit (B5.5 + B5.6) | P1.5 | ⏳ |
| OSWorld + web-search plug-ins (B2.2 + B2.3) | P1.5 | ⏳ |
| `xrlenv analyze` CLI (B9.1) | P1.5 | ⏳ |
| Reward modes `external_final` + `token_level` (B1.6) | P1.5 | ⏳ |
| Trajectory viewer extensions (B7.8–B7.12) | P1.5 | ⏳ |
| `xrlenv bootstrap` subcommand (B8.1) | P1.5 | ⏳ |
| **mTLS for node-control stream (B5.1)** | P1.x | ⏳ deferred to hardening |

## Next steps

Ordered by what unblocks the gates, then by what's a real
platform need surfaced post-phase-0.

### Hard requirements for the correctness gate (Gate 1)

1. **P1.1 — Phase-0 carry-forward.** D21 (`Client.list_nodes`)
   and D15 (GC layer 3) and D17 (HTTP timeout plumb-through) and
   D13 (load-vector placement) all need to ship before slices
   below it can build cleanly. ~2 weeks.
2. **P1.1.5 — B11 external plug-in package mechanism.**
   Foundation. The SWE-bench Verified plug-in (P1.4) ships using
   it. ~1 week.
3. **P1.2 — Image distribution.** A1 umbrella
   (D18+D19+D20) + A6/D16 + admin `/images` view. SQLite stays as
   the only StateStore — Redis (B6.1) deferred to phase 2 since
   the phase-1 100-200 concurrent envelope is comfortably within
   SQLite WAL's ~1-3k writes/sec ceiling. ~3 weeks.
4. **P1.3 — Slime + verl adapters.** B1.1 + B1.5 + B3.7 + B1.3.
   ~3 weeks.
5. **P1.4 — SWE-bench Verified plug-in + Gate 1 driver.** B2.1 +
   B9.3 + B5.4 + B5.7/D12.2 (if applicable) + B1.2. ~3 weeks.

### Hard requirements for the scale gate (Gates 2a + 2b)

6. **Gate 2a driver** — `examples/scale/bookkeeping_load.py`.
   Fans out N concurrent `hello-shell` rollouts using
   `Client.batch_rollout`; collects per-rollout creation latency;
   enforces transient-state cleanliness via periodic state-store
   query. ~3 days.
7. **Gate 2b driver** — `examples/scale/swebench_verified_load.py`.
   Fans out N concurrent SWE-bench Verified instances on a
   rotating instance set deliberately larger than the per-VM
   image cache budget. ~3 days.
8. **Image-cache eviction observability.** Cache events
   (`image_cache.evict`, `image_cache.fetch`, `image_cache.miss`)
   need to surface in `xrlenv events` so Gate 2b can verify the
   tier ordering held. ~3 days; lands with A6/D16 in P1.2.

### Real platform gaps that should land before phase-1 ships

9. ~~**mTLS for node-control stream.**~~ Deferred to P1.x
   hardening (Q7).
10. **Phase-1 refresh.sh equivalent** for the `xrlenv-swebench-
    verified` external package — operators iterating on the
    plug-in shouldn't have to re-bootstrap. Same shape as the
    phase-0 `deploy/refresh.sh`. ~2 days.
11. **Slime adapter integration test** that runs without a real
    Slime install (uses a mock `Sample` shape). Lets CI catch
    Slime-adapter regressions without a ~2 GB Slime install in
    the test env. ~1 week.

### Quality-of-life nits (low priority)

12. **Auto-derive `provider` column in `xrlenv nodes` from
    node_id prefix** (`gcp-` / `aws-` / `local-`). Five-line
    change.
13. **`xrlenv rollouts --aggregate` flag** that reports per-
    template / per-node summary statistics. Useful for both
    gates' driver scripts.

### After phase 1 ships (defer to P1.x or phase 2)

14. **mTLS for node-control stream** (Q7 / B5.1). P1.x
    hardening.
15. **CubeSandbox / microVM backend** (Q3). Phase 2.
16. **Mixed-backend capacity accounting** (A8 / D14). Phase 2,
    lands with CubeSandbox.
17. **k8s/MIG/ASG autoscale + durable trajectory store + multi-
    tenant isolation** (phase 2 and phase 3 per spec 00).
18. **Sandbox sessions with preemption-safe resume** (phase 3).

---

## What's NOT in phase 1 (and why)

Honest scoping so reviewers don't ask:

- **CubeSandbox / microVM backend** — image story is genuinely
  different (microVM rootfs ≠ Docker image); phase 1 keeps Docker
  as first-class citizen, CubeSandbox lands phase 2.
- **mTLS** — phase-0 bearer tokens cover the gate's auth needs;
  mTLS is hardening, lands P1.x.
- **Function-Call execution mode** — sandbox-mode trainer
  integration is the phase-1 win; Function-Call lands P1.5.
- **OSWorld + web-search plug-ins** — SWE-bench Verified is the
  canonical phase-1 benchmark; the others are spec-named phase-1
  but can ship in P1.5 without gating.
- **Real model in the loop** — both gates use oracles or mocks.
  Real-model runs are post-acceptance.
- **500-1k+ concurrent on Redis** — phase-1 targets 100-200
  concurrent on SQLite (within WAL's ~1-3k writes/sec ceiling),
  validated via Gate 2a. Redis StateStore + the higher target
  land in phase 2 alongside autoscale + durable trajectory
  storage. Pre-baked design answers for the eventual Redis impl
  are recorded in `notes/deferred_audit_todos.md` (B6.1).
- **1k concurrent SWE-bench Verified** — Gate 2b at 32 validates
  the eviction tier; full 1k-at-real-load needs ~2 TB image
  cache per VM and lives in phase 2 alongside autoscale.
