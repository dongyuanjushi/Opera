# Phase-1 To-do — converged plan

> **Status:** post-iteration plan; **P1.1 + P1.1.5 + P1.2 + P1.4 +
> P1.6 + P1.7 closed**; **2026-05-06 slim-pivot reframing** in
> place; **2026-05-10 trainer-integration reframing**: B1 (Slime +
> verl adapters) moves to phase 2 alongside Redis + CubeSandbox.
> Phase 1 exits on **operationally-honest cluster substrate for
> agentic benchmark evaluation** — no in-process trainer plane.
> The five-item revised exit checklist lives in
> "Revised phase-1 exit criteria" below; phase-2-bound items are
> captured separately in `notes/phase-2-todo.md`.

## Slice progress

| Slice | Status | Highlights |
|---|---|---|
| **P1.1** | **CLOSED** | A2/D21 (`d4bc66e`), A3/D15 (`82c914e` + audit fix `38b0bb3`), A5/D17 stage 1 (`2bb8edc` + audit fix `38b0bb3`), A7/D13 (`c5226ff`), A9 test-coverage debt (`d162256`+`bd5c42b`). Section A only retains A1, A4, A6 + a deferred-by-design (D5/D8) tail. |
| **P1.1.5** | **CLOSED** | B11.1–B11.5 (`fc8528e`); doc restructure (`e863e36`); audit M1/M2 narrowing → tracked as **D22** for end-of-P1 (`c0fe2cb`). **D22 + B11.6 closed end of P1**: D22 runtime fix (DiscoveredManifest + extra_plugin_roots + PYTHONPATH layering) shipped; B11.6 validation rides on two new worked examples (echo_bench + byo_dataset_harbor) under `examples/pip_new_datasets_or_benchmark/`, both end-to-end-passing on Docker Desktop. |
| **P1.2** | **CLOSED** | **P1.2.a closed** (A1 image distribution umbrella mechanics: `image_pin_mode`, `QueryImage` RPC, image-affinity scheduling, pre-flight check, per-placement audit event, `ship-images.sh`). **P1.2.b closed** (A6/D16 tier-ordered LRU eviction in `a3cdf83`; A5/D17 stage 2 per-call HTTP timeout plumb-through in `d56eb78`; consolidated operator runbook + eviction tunables at `docs/deployment/images.md`). **P1.2.c closed** (B7.6 admin `/images` view, originally shipped at `/blobs` and renamed in 2026-05: `ReportImagesCommand` proto + node-side dispatcher, on-demand fan-out via `cfg.node_lookup`, cluster-wide tier histogram (summed-across-nodes + distinct-by-tag) + dense per-node breakdown). **Redis (B6.1) deferred to phase 2** — phase 1 stays on SQLite WAL at 100-200 concurrent. |
| ~~P1.3~~ | **DEFERRED to phase 2 (2026-05-10)** | Slime + verl adapters; sink-aware readers; `client.warmup(...)`. Re-prioritised twice — first to post-P1.7 under the slim pivot, then to **phase 2** entirely. The phase-1 evaluation-substrate work has carried xrlenv to a usable cluster for agentic benchmark evaluation without an in-process trainer plane. Captured in `notes/phase-2-todo.md` § Trainer integration. |
| **P1.4** | **CLOSED (superseded)** | swebench-verified plug-in shipped in-tree under `xrlenv_plugins/benchmarks/swebench_verified/`. Resolver + EnvAdapter + in-sandbox grader + `build-task-images.sh` + 8-instance oracle smoke. 906/906 tests including 47 new. Operator + developer docs at `docs/integration/benchmarks/swebench_verified/`. **Superseded 2026-05 by P1.7's drop-in approach** — under the slim pivot, swebench keeps its own harness and routes via `xrlenv.from_env()` cluster mode rather than through an xrlenv-shaped EnvAdapter. The in-tree plug-in is slated for deletion (see P1.7 cleanup). The work was a useful interim step but the chosen architecture path supersedes it. |
| **P1.7** | **CLOSED** | **Slim pivot — cluster substrate for evaluation harnesses.** P1.7.A.1 + P1.7.A.2 closed (raw-container session API). P1.7.B (`xrlenv.from_env()` cluster mode docker-py drop-in) closed against a real cluster. P1.7.B.2 (image-affinity scheduling + warm-pool ImageCacheManager + `xrlenv images plan`) closed. P1.7.B.3 (raw rollout tracking) closed. P1.7.C.2 (build-plan dispatch + cancel + calibrate + tarball + build-on-acquire + `--skip-if-present` warm-cluster fast path) closed. P1.7.D (delete in-tree benchmark plug-ins under `xrlenv_plugins/benchmarks/`) closed. |
| **P1.6** | **CLOSED (subsumed)** | Control-plane-driven image builds — folded into P1.7.C.2's build-plan dispatch work. `build-plan.yaml` + `xrlenv build apply` end-to-end ships: per-image-ref dispatch for registry / git / tarball entries; pin-budget guard; cluster-side cancel; calibrate; build-on-acquire after eviction; `--skip-if-present` fast path. Operator + smoke validation covered in `tests/smoke/build_plan/`. |
| P1.5 | **partially deferred → P1.x** | Scale gate driver (100-200 concurrent on SQLite) revised — see "Revised phase-1 exit criteria" below. Remaining surface re-bucketed: **kept for P1.x** (OTel, admin auth, `xrlenv bootstrap`, mTLS, token rotation, userns-remap); **deferred to phase 2** (Function-Call mode, OSWorld, web-search, `xrlenv analyze`, warm pools, trajectory-viewer polish — all listed in `notes/phase-2-todo.md`). |
| **P1.x** | **active — see "Revised phase-1 exit criteria" below** | Observability + auth + bootstrap polish + security hardening + scale-gate (raw-container path). Five-item exit checklist; ~10-15 days of focused work absent surprises. |

Detailed slice contents below.

## Phase-1 thesis (one paragraph — REVISED 2026-05-10)

Phase 0 proved the platform can carry **one** benchmark
(terminal-bench-2) end-to-end across multi-VM with the trainer
plane stubbed by an oracle smoke. **Phase 1 makes xrlenv an
operationally-honest cluster substrate for agentic benchmark
evaluation**: real upstream harnesses (swebench, terminal-bench-2,
harbor) drive case-2/3 rollouts via the docker-py drop-in
(`xrlenv.from_env()`); image distribution is affinity-aware so
plug-ins with hundreds of per-instance images scale; benchmark
plug-ins ship as external pip packages without touching the
source tree; `xrlenv build apply` materializes plans across the
cluster with full operator surface (cancel, calibrate,
build-on-acquire, warm-cluster fast path). Phase 1 stays on
SQLite WAL at the **100-200 concurrent container** envelope.
**Trainer integration (Slime / verl adapters), Redis StateStore,
and CubeSandbox are all deferred to phase 2** — captured in
`notes/phase-2-todo.md`. Phase-1 exit is a five-item checklist
(observability + auth + bootstrap + security + scale-gate-revised)
covered in "Revised phase-1 exit criteria" below.

## Revised phase-1 exit criteria (2026-05-10)

With trainer integration deferred to phase 2, phase 1 exits on a
**five-item checklist** that brings the cluster substrate to a
shape suitable for sustained agentic-benchmark evaluation
(case 2/3) without an in-process trainer plane:

| # | Slice | Items | Cost |
|---|---|---|---|
| **1** | Observability + admin auth | **B7.1** OTel spans for control-plane + node ops; **B7.3** Admin panel HTTP basic auth | ~2-3 days |
| **2** | Bootstrap polish | **B8.1** `xrlenv bootstrap` subcommand replacing shell scripts (cross-platform GCP / AWS branches in Python; testable) | ~2 days |
| **3** | Security hardening | **B5.1** mTLS for node-control stream (Q7); **B5.2** Per-consumer token rotation/revocation; **B5.4** `userns-remap` Docker default | ~3-4 days |
| **4** | Scale gate (revised) | Original gate-2 was "100-200 concurrent containers driven by a trainer." Trainer-deferral drops the trainer half; gate becomes "drive 200 concurrent raw containers via the docker-py drop-in (or `Client.acquire_container`) against a real cluster for ≥30 minutes, no leaks, p95 acquire-latency < 5s." Needs a smoke driver. | ~2-3 days |
| **5** | Phase-1 acceptance write-up | Revised `notes/phase-1-acceptance.md` reflecting the four slices above (replacing the trainer-driven correctness gate) | ~1 day |

**Total: ~10-15 days of focused work.**

### Already-shipped surface that satisfies the new "evaluation substrate" thesis

These don't need to ship; just calling out that they're the load-
bearing pieces of the revised thesis:

- **P1.7.B**: `xrlenv.from_env()` cluster-mode docker-py drop-in.
- **P1.7.B.2**: image-affinity scheduling, ImageCacheManager
  warming, `xrlenv images plan` operator CLI.
- **P1.7.B.3**: raw rollout tracking.
- **P1.7.C.2**: `xrlenv build apply` full lifecycle including:
  registry + git + tarball dispatch, pin-budget, calibrate,
  cancel (with sticky semantics + race fix), build-on-acquire
  after eviction, `--skip-if-present` warm-cluster fast path.
- **B11**: external plug-in package mechanism (two worked
  examples in `examples/pip_new_datasets_or_benchmark/`).
- **Smokes**: `tests/smoke/build_plan/`, `tests/smoke/api_surface/`,
  `tests/smoke/benchmark_integration/`, `tests/smoke/cluster_bringup/`
  cover the full operator surface end-to-end against a real
  cluster.

### Smaller polishes worth picking up opportunistically

Not gating; ship if they come up:

- **B7.4** Admin 24-hr metric history (operator workflow polish).
- **B7.7** Admin `/forwards` + `/audit` views.
- **B5.3** Signed manifests.
- **B6.4** Signed audit archive.
- **B10.1, B10.4** Sandbox sessions design hooks (lock in proto
  shapes for phase-3 sandbox sessions without shipping execution).

## Decisions taken (signed off)

| # | Decision | Effect |
|---|---|---|
| Q1 | **Gold-patch oracle for the correctness gate** | SWE-bench Verified plug-in ships with an oracle adapter that applies the gold patch + runs tests; same shape as tb2's `_make_oracle_policy`. **2026-05 update under slim pivot**: oracle intent stays (gold patches for swebench, harbor's `oracle` agent for tb2), but the plumbing shifts from "in-tree EnvAdapter" to P1.7's drop-in path — `tests/smoke/test_swebench_drop_in.py` already runs swebench's harness with gold patches; P1.7.B retargets it at the cluster topology. |
| Q2 | **Strict P1.1 → P1.2 → ... ordering** | No parallel slices; cleanup finishes before structural work. |
| Q3 | **CubeSandbox / microVM deferred to phase 2** (not phase 1.5) | Phase 1 ships Docker-only as first-class citizen. CubeSandbox-touching items (B4.1, B8.2, A8/D14 mixed-backend) all defer. |
| Q4 | **Redis StateStore deferred to phase 2** (revised 2026-05-02) | Phase-1 target is 100-200 concurrent containers — comfortably within SQLite WAL's ~1-3k writes/sec ceiling. SQLite stays as the only StateStore through phase 1; Redis lands in phase 2 alongside the 500-1k+ target. User's pre-baked design answers for the eventual Redis impl are recorded under B6.1 in `notes/deferred_audit_todos.md` so phase-2 doesn't relitigate. |
| Q5 | **Function-Call mode → phase 1.5** | Sandbox-mode trainer integration is the phase-1 win. |
| Q6 | **SWE-bench *Verified* (not Lite) is the canonical phase-1 benchmark** | Plug-in dir: `xrlenv-swebench-verified`, shipped as an external pip package (validates B11). OSWorld + web-search → phase 1.5. |
| Q7 | **mTLS → phase 1.x hardening** | Bearer tokens stay through both gates. |
| **NEW: B11** | **External plug-in package mechanism** | New deliverable. Plug-ins discoverable via filesystem path, env var, OR python entry-points. SWE-bench Verified is the first user. |
| **2026-05-10: B1 trainer integration deferred to phase 2** | Slime + verl adapters move to phase 2 alongside Redis + CubeSandbox | Phase 1 has reached "operationally-honest cluster substrate for agentic benchmark evaluation" — the slim pivot proved out via real benchmark drop-ins (swebench, terminal-bench-2, harbor) hitting a live GCP cluster. Trainer integration adds a separate axis of complexity (RL training loops, sink-aware readers, warmup) that doesn't share critical dependencies with what phase 1 has shipped. Folding it into phase 2 lets phase 1 exit on hardening (observability, auth, mTLS, bootstrap, scale-gate) instead of dragging on. Detailed acceptance + open items in `notes/phase-2-todo.md`. |

---

## Acceptance — two gates at phase-1 exit

Mirrors `notes/phase-0-acceptance.md`'s structure. Final form
lives in `notes/phase-1-acceptance.md` once this plan ships.

### Gate 1 — Correctness gate

**The signal:** the platform carries trainer-driven rollouts
end-to-end through a real benchmark using the canonical Slime
SDK shape.

- **8 oracle-driven SWE-bench Verified rollouts across 2 VMs,
  all sealed `finished` with parseable `final_reward`.**
- The driver wraps `xrlenv/adapters/slime.py::make_rollout_fn`,
  but mocks Slime's policy: each "model-predicted step" is
  replaced with the gold-patch oracle's next action. So the
  *Slime adapter* is real (not stubbed); only the model is
  replaced with an oracle for the gate, just like phase 0
  replaced the agent with harbor's `solve.sh`.
- Distribution across both VMs verified (4-4 split, same as
  phase-0 gate).
- `auth.denied = 0`; node-token auth gates the bidi stream.
- Pre-flight image check works (D19) — instance scheduled to a
  bare node fails fast with `reason="image_missing"` instead of
  through a docker pull-denied error.

**Honesty caveat (same shape as phase 0):** the smoke uses a
gold-patch oracle as the policy; rewards reflect what the
upstream solution accomplishes through XRLEnv's plumbing — a
platform-integrity signal, NOT a model-eval result. Real Slime-
with-real-model runs are post-acceptance work.

### Gate 2 — Scale gate

**The signal:** the platform sustains the load profile a real
training run produces at the 100-200 concurrent envelope.

- **100-200 concurrent rollouts** on the SQLite-backed control
  plane (Redis deferred to phase 2 — see Q4 decision).
- Rollouts can be cheap mocks (`hello-shell` template, N no-op
  steps, exit) — the load is on the control plane's
  bookkeeping (state transitions, heartbeats, events,
  scheduling decisions, sandbox lifecycle), NOT the sandboxes
  themselves.
- **Acceptance latency targets** under sustained 200 concurrent:
  - Median rollout-creation latency < 1 s.
  - 99p rollout-creation latency < 5 s.
  - No `cancelling` / `finishing` rows pinned >5 min (validates
    the per-RPC timeouts from `2634399` hold under load).
- Run on Docker-only nodes with image-affinity scheduling (D18)
  active.

**Why two gates:** correctness (does the answer come out right)
and scale (does the platform carry the load) are different
validations. The phase-0 gate proved correctness; phase 1 needs
both.

---

## Section A — Phase-0 carry-forward

Same as v1; cross-referenced by D-number. Cost weight `[XS]` ≤ 1
day, `[S]` ≤ 1 week, `[M]` ≤ 2 weeks, `[L]` > 2 weeks.

| ID | Item | D# | Cost | Slot |
|---|---|---|---|---|
| ~~A1~~ | ~~Image distribution + digest pinning umbrella.~~ Closed across P1.2.a + P1.2.b: `image_pin_mode` (b82e7ec), `QueryImage` RPC (8b083dc), image-affinity scheduling + pre-flight check (c163ed2), per-placement `placement.image_check` audit event + `deploy/ship-images.sh` (0ec6b40), per-mode operator runbooks at `docs/deployment/images.md` (P1.2.b [3/3]). | D18+D19+D20 | L | **CLOSED** in P1.2 |
| A2 | SDK `Client.list_nodes()` + `wait_for_nodes()` | D21 | S | P1.1 |
| ~~A3~~ | ~~GC layer 3 — control-plane reconcile via bidi~~ | D15 | S | **CLOSED** in P1.1 (commit 82c914e) |
| A4 | UID separation for grader (D12 stage 2) | D12.2 | M | P1.4 (only if SWE-bench Verified tasks declare non-None user fields; otherwise defer) |
| ~~A5~~ | ~~Per-call HTTP timeout plumb-through — stage 1 closed in P1.1 (2bb8edc); stage 2 closed in P1.2.b (`d56eb78`).~~ | D17 stage 2 | S | **CLOSED** (stage 1 in P1.1, stage 2 in P1.2.b) |
| ~~A6~~ | ~~Image-cache eviction policy (LRU final → stub-runtime → base).~~ Tier-ordered eviction primitive closed in `a3cdf83`; soak-test acceptance still open and slotted into P1.5 scale-gate work. | D16 | M | **PRIMITIVE CLOSED** in P1.2.b |
| ~~A7~~ | ~~Scheduler placement: load-vector cost (CPU+mem+disk slack)~~ | D13 | S | **CLOSED** in P1.1 (commit c5226ff) |
| ~~A8~~ | ~~Mixed-backend capacity accounting~~ | ~~D14~~ | ~~S~~ | **DEFERRED** to phase 2 (CubeSandbox dependency) |
| A9 | Test-coverage debt — D1, D2, D3, D4, D6, D7, D9 closed in `d162256`+`bd5c42b`; D5 + D8 deferred-by-design (entry-point smoke not worth the cost until those grow CLI surface) | D5, D8 only | XS | deferred-by-design |

---

## Section B — Phase-1 surface

Items removed from v1 because of Q3/Q5/Q6/Q7 deferrals are
listed in Section F. Items below are the active phase-1 set.

### B1. ~~Trainer integration~~ — **DEFERRED to phase 2 (2026-05-10)**

All B1 items move to `notes/phase-2-todo.md`. Original table kept
below for traceability (struck-through to reflect the deferral).

| # | Item | Source | Cost | Slot |
|---|---|---|---|---|
| ~~B1.1~~ | ~~`xrlenv/adapters/slime.py`~~ | spec 11, 05 | M | **phase 2** |
| ~~B1.2~~ | ~~`examples/slime/swebench_verified_smoke.py`~~ | spec 11 | S | **phase 2** |
| ~~B1.3~~ | ~~`xrlenv/adapters/verl.py`~~ | spec 12, 05 | M | **phase 2** |
| ~~B1.4~~ | ~~`examples/verl/swebench_verified_smoke.py`~~ | spec 12 | S | **phase 2** |
| ~~B1.5~~ | ~~Sink-aware readers~~ | spec 17 | S | **phase 2** |
| ~~B1.6~~ | ~~Reward modes `external_final` and `token_level`~~ | spec 02 | M | **phase 2** |

### B2. Benchmark integrations

| # | Item | Source | Cost | Slot |
|---|---|---|---|---|
| ~~B2.1~~ | ~~`xrlenv-swebench-verified` external pip package + plug-in adapter + gold-patch oracle~~ | spec 11, 06 | L | **SUPERSEDED 2026-05** by P1.7's drop-in approach. Under the slim pivot, swebench keeps its own harness and routes via `xrlenv.from_env()` cluster mode — no xrlenv-shaped adapter needed. The in-tree plug-in shipped in P1.4 is slated for deletion in P1.7. |
| ~~B2.2~~ | ~~`xrlenv_plugins/benchmarks/osworld/` (Pattern B)~~ | spec 06 | L | **DEFERRED phase 2** (no consumer in phase-1 substrate scope; swebench + terminal-bench-2 cover the slim-pivot validation set) |
| ~~B2.3~~ | ~~`xrlenv_plugins/benchmarks/web_search/`~~ | spec 06, 14 | M | **DEFERRED phase 2** (web_search needs egress-allowlist networking B5.5/B5.6 too) |

### B3. Image distribution + sandbox lifecycle

| # | Item | Source | Cost | Slot | Maps to |
|---|---|---|---|---|---|
| B3.1 | Image-affinity scheduling + `QueryImage` RPC | spec 03, 15, 21 | M | P1.2 | A1 |
| B3.2 | Pre-flight image check before placement | spec 03, 15 | S | P1.2 | A1 / D19 |
| B3.3 | `image_pin_mode` on instances + manifests — three modes: `registry_digest`, `per_node_local`, `shared_storage` (CubeSandbox-inspired; see `notes/related-work.md`) | spec 06, 15, 19 | S | P1.2 | A1 / D20 |
| B3.4 | Image distribution recipes: `deploy/ship-images.sh` (build-once-ship-many) + `deploy/mount-shared-rootfs.sh` (NFS/S3-backed read-only mount) | spec 09 | S | P1.2 | A1 / D20 |
| ~~B3.5~~ | ~~Lazy-load image support — eStargz (Docker only)~~ | spec 06, 15 | M | **DEFERRED phase 2** | (performance polish only matters at trainer-driven concurrency; overlaybd part still tied to CubeSandbox) |
| ~~B3.6~~ | ~~Automatic image-cache eviction policy~~ | spec 15 | M | **CLOSED** in P1.2.b | A6 / D16 |
| ~~B3.7~~ | ~~`client.warmup(templates, instance_ids, ...)` SDK + `WarmupReport` callback~~ | spec 05, 15 | S | **DEFERRED phase 2** | Slime adapter is the consumer; deferred with B1 |

### B4. Sandbox pool — phase-1 scope

| # | Item | Source | Cost | Slot | Notes |
|---|---|---|---|---|---|
| ~~B4.1~~ | ~~CubeSandbox backend~~ | ~~spec 01, 04, 10~~ | ~~L~~ | **DEFERRED phase 2** | Q3: image story is genuinely different; phase 1 stays Docker-only. |
| ~~B4.2~~ | ~~Function-Call execution mode + `client.invoke()`~~ | spec 01, 05 | M | **DEFERRED phase 2** | Q5 originally; with trainer integration deferred too, drops alongside. |
| ~~B4.3~~ | ~~Warm pools per template~~ | spec 04, 06 | M | **DEFERRED phase 2** | Latency floor lift only matters under trainer-driven concurrency. |
| ~~B4.4~~ | ~~24-hour disk-based ring buffer for resource samples~~ | spec 04, 08 | S | **DEFERRED phase 2** | Lands with B7.4 (observability polish). |

### B5. Security

| # | Item | Source | Cost | Slot | Notes |
|---|---|---|---|---|---|
| ~~B5.1~~ | ~~mTLS for node-control stream~~ | ~~spec 07, 19, 21~~ | ~~M~~ | **P1.x hardening** | Q7 |
| ~~B5.2~~ | **CLOSED (2026-05-10, P1.x slice 2)**: `xrlenv tokens rotate <role> [--grace 24h]`, `xrlenv tokens revoke <token-id>`, `xrlenv tokens list`. Immediate-cutover default; per-role `<role>.token.previous.json` sidecar for graced rotations; global `revoked.json` for invalidations. 12-char `token_id` (sha256-first-12) + ≥6-char prefix matching for revoke ergonomics. Hot-reloaded via the existing mtime-watch path. | spec 19 | S | **CLOSED in P1.x slice 2** | |
| B5.3 | Signed manifests (image ref + asset SHA-256) | spec 19, 06 | S | P1.5 | Trusted-publisher list. |
| ~~B5.4~~ | **REVISED + CLOSED (2026-05-10, commit `620f282`)**: `userns-remap` as **per-acquire opt-in** on the raw-container path (`Client.acquire_container(userns_mode="remap")`); default `"host"` preserves benchmark compat. Originally scoped as "default for Docker sandboxes" — reframed because lots of benchmark images need in-container root. | spec 19, 01 | S | **CLOSED in P1.x slice 1** | Defense-in-depth for D12 stage 2. |
| ~~B5.5~~ | ~~Egress-allowlist networking (DNS + iptables per sandbox)~~ | spec 07 | M | **DEFERRED phase 2** | Required for B2.3 (`web_search`) — both deferred together. |
| ~~B5.6~~ | ~~Per-sandbox network audit (`<run-dir>/network.log`)~~ | spec 07, 08 | S | **DEFERRED phase 2** | Lands with B5.5. |
| B5.7 | UID separation for grader (D12 stage 2) | spec 01, 19 | M | P1.x (defense-in-depth) | A4 — keep as phase-1.x polish; cheap to ship now that swebench drop-in flow is stable. |

### B6. State + storage

| # | Item | Source | Cost | Slot | Notes |
|---|---|---|---|---|---|
| ~~B6.1~~ | ~~Redis StateStore~~ | ~~spec 03, 20~~ | ~~L~~ | **DEFERRED phase 2** | Q4 revised 2026-05-02: phase 1 stays SQLite at 100-200 concurrent (well within WAL's ~1-3k writes/sec). Pre-baked design answers in `notes/deferred_audit_todos.md`. |
| ~~B6.2~~ | ~~Active + warm-standby control plane via shared Redis~~ | ~~spec 03~~ | ~~M~~ | **DEFERRED phase 2** | Lands with B6.1. |
| ~~B6.3~~ | ~~Pool-accounting persistence~~ | spec 04, 20 | S | **DEFERRED phase 2** | Lands with B4.3 (warm pools). |
| B6.4 | Signed audit archive | spec 19, 20 | S | P1.x (security polish) | Cheap, complements B5.* hardening. |
| ~~B6.5~~ | ~~Optional object-store integration for trajectory archival~~ | spec 20 | M | **DEFERRED phase 2** | Trainer-driven trajectory volume justifies it; deferred with trainer integration. |

### B7. Observability

| # | Item | Source | Cost | Slot |
|---|---|---|---|---|
| ~~B7.1~~ | **CLOSED (2026-05-11, P1.x slice 4)**: OpenTelemetry tracing surface. New `xrlenv/observability/tracing.py` with lazy `get_tracer()`; 8 instrumentation points (`coordinator.dispatch_rollout`, `coordinator.build_apply`, `scheduler.place`, `node.create_sandbox`, `node.env_step`, `node.ensure_present`, `node.source_build`, `transport.rpc`). Three modes: off (no env var, noop), `OTEL_TRACES_EXPORTER=console`, `OTEL_EXPORTER_OTLP_ENDPOINT=...`. New `[observability]` extra in pyproject; default install stays SDK-free via a graceful noop fallback. | spec 08 | M | **CLOSED in P1.x slice 4** |
| B7.2 | Rolling-file logs per node (vs phase-0 in-memory ring) | spec 08 | S | P1.x (lands with B7.1) |
| ~~B7.3~~ | **CLOSED (2026-05-11, P1.x slice 3)**: Admin panel HTTP basic auth + two-tier viewer/operator roles. `xrlenv tokens issue viewer` emits `read_*` token; `tokens issue operator` emits `write_*`. Bind guard relaxed to permit `--admin-allow-public` when TokenStore is wired; `/healthz` + `/static/*` stay open. Bearer + Basic transports both accepted (CLI + browser). | spec 13 | S | **CLOSED in P1.x slice 3** |
| B7.4 | Admin panel: 24-hour metric history from in-process tsdb | spec 13, 08 | M | P1.x polish (post-exit-checklist) |
| B7.5 | Admin panel: operator actions (pin/unpin templates, trigger warmup, port-forward controls) | spec 13 | M | P1.x polish (gated on B7.3) |
| ~~B7.6~~ | ~~Admin panel: `/images` view~~ | spec 13, 15 | S | **CLOSED** in P1.2.c |
| B7.7 | Admin panel: `/forwards`, `/audit` views | spec 13, 19 | S | P1.x polish |
| ~~B7.8~~ | ~~Trajectory viewer: token-level overlay tab~~ | spec 17 | M | **DEFERRED phase 2** (lands with B1.6) |
| B7.9 | Trajectory viewer: image rendering inline | spec 17 | S | P1.x polish |
| B7.10 | Trajectory viewer: lazy-binary cache mode | spec 17 | S | P1.x polish |
| B7.11 | Trajectory viewer: compare two trajectories | spec 17 | S | P1.x polish |
| B7.12 | Trajectory viewer: bulk download (tarball) | spec 17 | XS | P1.x polish |

### B8. Deployment

| # | Item | Source | Cost | Slot |
|---|---|---|---|---|
| ~~B8.1~~ | **CLOSED (2026-05-11, P1.x slice 5; operator-smoke pending)**: new `xrlenv bootstrap --target {gcp,aws,linux-generic}` Python subcommand at `xrlenv/cli/bootstrap.py` (stdlib-only so it runs on fresh VMs pre-pydantic). Faithful port of bash bootstrap-common.sh: OS probe via /etc/os-release (override via --target-os), cloud-metadata auto-detect (GCP metadata + AWS IMDSv2), Docker install branch (apt/dnf), full ensure_user → ensure_directories → install_python_venv → install_systemd_unit → node-token drop-in → operator docker-group sequence. Idempotent `skip_if` predicates per step. `--dry-run` prints the plan without touching the host. `deploy/bootstrap-{gcp,aws}.sh` are now 3-line wrappers that exec the Python module as a flat script. `refresh.sh` + `bootstrap-common.sh` left intact (refresh.sh still uses them). 23 new unit tests. | spec 09 | M | **SHIPPED in P1.x slice 5** — operator-driven smoke on real VM tomorrow. |
| ~~B8.2~~ | ~~CubeSandbox install in bootstrap~~ | ~~spec 09, 01~~ | ~~S~~ | **DEFERRED phase 2** |
| B8.3 | Optional cluster-wide registry mirror | spec 09, 15 | M | P1.x polish |
| ~~B8.4~~ | ~~Redis bootstrap recipe (single-node + standby)~~ | ~~spec 09, 20~~ | ~~S~~ | **DEFERRED phase 2** (lands with B6.1) |

### B9. Benchmark analysis

| # | Item | Source | Cost | Slot |
|---|---|---|---|---|
| ~~B9.1~~ | ~~`xrlenv analyze` CLI — four-pass analysis~~ | spec 16 | L | **DEFERRED phase 2** | tool for understanding benchmark catalogs at trainer-driven scale; defers with B1. |
| ~~B9.2~~ | ~~`xrlenv analyze --remote analyzer-vm`~~ | spec 16 | S | **DEFERRED phase 2** | Lands with B9.1. |
| ~~B9.3~~ | ~~`EnvAdapter.enumerate_tasks()` contract~~ | spec 14, 16 | S | **N/A (superseded by P1.7 slim pivot)** | EnvAdapter-shaped plug-ins were the pre-slim-pivot model; under the drop-in path, benchmark task enumeration happens inside each harness. Not load-bearing. |

### B10. Sandbox sessions design hooks (no execution — phase 3 work)

| # | Item | Source | Cost | Slot |
|---|---|---|---|---|
| B10.1 | Stub middleware seam for trajectory sub-tracing | spec 18 | XS | P1.5 |
| B10.2 | Sandbox-vs-rollout lifetime decoupling preserved in data model | spec 18 | XS | already honored phase 0 |
| B10.3 | GC by owner-refcount semantics | spec 18 | S | P1.1 (lands with A3) |
| B10.4 | **NEW**: design proto RPC shapes for `Snapshot(sandbox_id) → snapshot_id`, `Restore(snapshot_id) → sandbox_id`, `CommitSandbox(sandbox_id) → template_id`. CubeSandbox-inspired (see `notes/related-work.md`); execution stays phase 3 but the wire shape lands phase 1 so phase-3 doesn't break the proto contract | spec 18, 21 | XS | P1.5 |

### B11. **NEW** External plug-in package mechanism

The pip-package extensibility story. Three discovery paths:

| # | Item | Source | Cost | Slot |
|---|---|---|---|---|
| ~~B11.1~~ | ~~Filesystem-path discovery: `XRLENV_TEMPLATE_DIRS` env var (manifest-only registration; sandbox-import-path extension tracked as D22, end of P1)~~ | new | XS | **CLOSED** in P1.1.5 (commit fc8528e). Audit M2 (2026-05-02): the row originally also claimed a `--template-dirs` CLI flag — that was never implemented and is removed; D22 captures the runtime fix that lifts the env-var contract to fully end-to-end. |
| ~~B11.2~~ | ~~Python entry-points discovery: `importlib.metadata.entry_points(group="xrlenv.benchmarks")` walked by `find_entry_point_manifest_files()`~~ | new | S | **CLOSED** in P1.1.5 (commit fc8528e) |
| ~~B11.3~~ | ~~Convert `xrlenv_plugins/` and `xrlenv_plugins/benchmarks/` to PEP-420 namespace packages (drop `__init__.py`)~~ | new | XS | **CLOSED** in P1.1.5 (commit fc8528e) |
| ~~B11.4~~ | ~~Skeleton at `templates/external_benchmark_package/` — `pyproject.toml` + `manifest.yaml` + `adapter.py` + `plugin.py` + `scripts/build-task-images.sh` + `tests/test_smoke.py`~~ | new | S | **CLOSED** in P1.1.5 (commit fc8528e) |
| ~~B11.5~~ | ~~Docs page: `docs/integration/index-as-a-package.md`~~ | new | S | **CLOSED** in P1.1.5 (commit fc8528e) |
| ~~B11.6~~ | ~~**Validation by use:** ship a real external pip package end-to-end~~ | new | M | **CLOSED** at end of P1: shipped two worked examples at `examples/pip_new_datasets_or_benchmark/` (`echo_bench/` for own-benchmark scenario; `byo_dataset_harbor/` for own-dataset-on-existing-format scenario, vendoring 2 Harbor-Dataset tasks). Each smoke seals `final_reward=1.0` end-to-end, lived validation of D22 + B11.1-5. SWE-bench Verified migrated separately if the user wants — both shapes are validated. |

**Why this matters:** today third parties can't add benchmarks
without forking the repo. Standard Python plug-in idiom (entry-
points + namespace packages) lets `pip install xrlenv-mybench`
register a new benchmark with zero source-tree edits. The
in-tree `xrlenv_plugins/benchmarks/terminal_bench_2/` stays as
the canonical reference + the platform's own CI signal.

**Spec reach:** 06 (manifest discovery), 14 (EnvAdapter
loading), 13 (admin sees external plug-ins via the same
catalog).

---

## Section C — Slice ordering (strict serial, per Q2)

### Slice P1.1 — Phase-0 carry-forward (~2 weeks)

Cleans up rough edges from the phase-0 multi-VM smoke recovery
before the structural work starts.

- A2 / D21 — `Client.list_nodes()` + `wait_for_nodes()` SDK RPC. **CLOSED in `d4bc66e`.**
- A5 / D17 — per-call HTTP timeout plumb-through. **STAGE 1 CLOSED** (per-sandbox cap derived from manifest's max phase timeout + 60 s buffer; coordinator injects `_xrlenv_http_timeout_s` into `init_params`, node-side strips + stages on the per-sandbox record). **Stage 2 deferred to P1.2** alongside D16 — full per-call kwargs through Protocol + proto + StubClient lands when the image-cache eviction work touches the same call sites.
- A3 / D15 — GC layer 3 (control-plane reconcile). **CLOSED** in P1.1 (commit 82c914e). New spec-21 `ListSandboxesCommand` + `xrlenv/control/gc_reconciler.py` periodically diff the node's sandbox-ID set against `state.list_sandboxes()`; node-only orphans are destroyed, state-only orphans seal the owning rollout `failed/sandbox_lost` via the new `coordinator.handle_sandbox_lost`. Wired into `build_distributed_runtime` with a 60 s default (operator/test override via `gc_reconcile_interval_s`); LocalRuntime intentionally not wired (in-process — drift impossible).
- A7 / D13 — scheduler placement load-vector cost. **CLOSED** in P1.1 (commit c5226ff). New `CapacityEstimator.slack_after_placement` returns the minimum-axis remaining-slack fraction (CPU + mem + sandbox-writable disk); `Scheduler.place()` scores candidates as `round(slack * 1000)` instead of the pre-D13 slots-of-this-template-remaining metric, so heterogeneous Pattern-A clusters load-balance across all axes the candidate actually requests rather than just same-template count.
- B10.3 — GC by owner-refcount (lands with A3).
- A9 / D1–D9 — test-coverage debt: **CLOSED** for D1, D2, D3, D4, D6,
  D7, D9 (commits `d162256` + `bd5c42b`). **D5 + D8** remain
  deferred-by-design (sandbox-stub `__main__` argv parser doesn't grow
  CLI surface; not worth the subprocess test cost until that
  changes).

### Slice P1.1.5 — External plug-in package mechanism (~1 week) — **CLOSED**

Foundation for the rest of phase 1. Independent of scheduler /
state-store changes; gets the SWE-bench Verified plug-in (P1.4)
shipped as an external package from day 1.

**Closed in P1.1.5** (commit fc8528e):

- **B11.1** — filesystem-path discovery via `XRLENV_TEMPLATE_DIRS`
  env var (`os.pathsep`-separated, `~`-expansion, non-existent
  entries dropped with a warning). New
  `extra_template_dirs_from_env()` helper; both `LocalRuntime`
  and `DistributedRuntime` append the parsed list to their
  `template_dirs` chain. **Audit M2 follow-up (2026-05-02)**:
  the env var registers the manifest only; it does not add
  external code roots to the sandbox import path. Adapters
  must already be reachable via the in-tree mount or via the
  Path-3 image-bundled pattern. Lifting that limitation is
  D22 (slotted end of P1 alongside B11.6).
- **B11.2** — Python entry-points discovery under the
  `xrlenv.benchmarks` group via
  `find_entry_point_manifest_files()`. Each entry-point loads to
  a callable returning `Path | Iterable[Path]` to manifest files;
  per-plug-in failures are logged + isolated so one broken plug-in
  cannot block the catalog.
- **B11.3** — `xrlenv_plugins/` and `xrlenv_plugins/benchmarks/`
  are now PEP-420 namespace packages (top-level `__init__.py`
  files dropped). The leaf `terminal_bench_2/__init__.py` stays
  as a regular package. External pip packages contribute new
  subdirectories under the same namespace via hatchling's
  `packages = ["xrlenv_plugins"]`.
- **B11.4** — copy-paste skeleton at
  `templates/external_benchmark_package/` with `pyproject.toml`
  (declaring the entry-point), `xrlenv_plugins/benchmarks/example_bench/`
  (adapter + manifest + plugin.py callable), `scripts/build-task-images.sh`,
  and `tests/test_smoke.py`. Test in-suite asserts the skeleton's
  manifest is YAML-valid and the entry-point group matches.
- **B11.5** — `docs/integration/index.md` (consolidated) ships the
  publishing recipe across all three distribution paths,
  namespace-package gotchas, the `importlib.resources.files()`
  path-resolution pattern, and the failure-isolation contract.
  Wired into the integration toctree. (The earlier separate
  `byob-as-a-package.md` was folded into `byob.md` in commit
  e863e36 during the role-based docs restructure.)

B11.6 (validation by use — ship `xrlenv-swebench-verified` as an
external package) lands in P1.4 alongside the SWE-bench plug-in
itself; the act of building it externally proves B11.1-5 work
end-to-end.

### Slice P1.2 — Image distribution (~3 weeks)

The structural work that unblocks SWE-bench Verified at scale.
**Redis StateStore deferred to phase 2** (Q4 revised 2026-05-02):
SQLite WAL handles the phase-1 100-200 concurrent envelope
comfortably (~1-3k writes/sec ceiling). The phase-1 scale gate
(Gate 2) runs on SQLite-backed control plane.

Sub-slice ordering:

- **P1.2.a — Image distribution umbrella (A1 / D18+D19+D20)**
  [~2 weeks]: **CLOSED**. Shipped in five commits:
  `b82e7ec` (image_pin_mode schema), `8b083dc` (QueryImage RPC),
  `c163ed2` (image-affinity scheduling + pre-flight check),
  `27aff7e` (technical/scheduling.md + technical/images.md), and
  `0ec6b40` (audit response — H3 mode-gate + M3 partial:
  per-placement `placement.image_check` audit event +
  `deploy/ship-images.sh`). Per-mode operator runbooks under
  `docs/deployment/` slip to P1.2.b.
- **P1.2.b — Image-cache eviction (A6 / D16) + D17 stage 2 +
  per-mode operator runbooks** — **CLOSED**. Three-slice landing:
  (1) `a3cdf83` tier-ordered LRU eviction with pluggable
  `tier_classifier` defaulting to the harbor / tb2 convention;
  (2) `d56eb78` D17 stage 2 — per-call HTTP timeout proto field
  on EnvSetup/EnvStep/EnvTeardown commands, decoded by the
  node-side dispatcher and applied as an aiohttp ClientTimeout
  override per call (the original deferral note bundled this
  with D16 to avoid reshape thrash, but in practice the two
  surfaces don't overlap so they shipped separately); (3) the
  consolidated `docs/deployment/images.md` runbook covering all
  three `image_pin_mode` topologies, the `ship-images.sh`
  build-once-ship-many helper, eviction tunables, and the
  troubleshooting matrix.
- **P1.2.c — Admin `/images` view (B7.6; first shipped at `/blobs`, renamed 2026-05)** — **CLOSED**. New
  spec-21 `ReportImagesCommand` proto + node-side dispatcher
  expose `ImageCacheManager.report()` over the wire; admin
  fans out via `cfg.node_lookup` on each render and aggregates
  a cluster-wide tier histogram + per-node breakdown (free
  disk, pinned set, drill-down per-image table). Standalone
  admin (no live runtime) shows an explanatory hint instead
  of partial data.

### Slice P1.3 — Trainer integration foundation (~3 weeks)

Slime + verl adapters that make phase-1's correctness signal
real.

- B1.1 — `xrlenv/adapters/slime.py` (`make_rollout_fn`).
- B1.5 — sink-aware readers for `slime-sample`.
- B3.7 — `client.warmup(...)` SDK (consumed via Slime's layer-2
  prewarm).
- B1.3 — `xrlenv/adapters/verl.py` (parallel within the slice;
  secondary signal).
- B1.5 — sink-aware reader for `verl-dataproto`.

### Slice P1.4 — SWE-bench Verified plug-in + correctness gate (~3 weeks)

The first new benchmark plug-in, shipping as an external
package, with the gate driver as its acceptance proof.

- B2.1 — `xrlenv-swebench-verified` external pip package
  (validates B11 end-to-end).
- B9.3 — `EnvAdapter.enumerate_tasks()` contract (the plug-in's
  image-build script needs to enumerate the ~500 instances).
- B5.4 — `userns-remap` default (defense-in-depth for B5.7).
- B5.7 / A4 / D12 stage 2 — UID separation IF SWE-bench tasks
  declare non-None user fields; check upstream task fixtures.
- B1.2 — `examples/slime/swebench_verified_smoke.py` —
  **correctness gate driver**. 8 oracle-driven rollouts via
  Slime adapter across 2 VMs.
- B1.4 — `examples/verl/swebench_verified_smoke.py` (best-
  effort, NOT gating).

### Slice P1.7 — Slim pivot: cluster substrate for evaluation harnesses (~2-3 weeks) — **NEXT**

**Why**: phase-0 + P1.4 shipped per-benchmark in-tree plug-ins
(`xrlenv_plugins/benchmarks/{tb2, swebench_verified}/`) that wrap
upstream harnesses with xrlenv-specific `EnvAdapter` /
`InstanceResolver` shapes. Forcing benchmark code through
xrlenv's gym shape (a) loses upstream artifact shapes — swebench
writes `report.json` + per-instance test outputs, harbor writes
`result.json` + verifier dirs; (b) requires xrlenv to track each
upstream's contract drift; (c) breaks the user UX (operators
have to learn xrlenv-specific config instead of using the
harness's own extension mechanism). The slim pivot reverses this:
**xrlenv exposes a cluster-routed sandbox primitive (exec /
upload / download against a remotely-scheduled container, scoped
to a rollout), and benchmark frameworks plug in via THEIR
existing extension shapes.** No benchmark-specific code in xrlenv
core. Drop-in UX: `docker.from_env() → xrlenv.from_env()` for
swebench-shaped tools; `environment.import_path:
xrlenv_plugins.harbor:XrlenvHarborEnvironment` in `job.yaml` for
harbor-shaped tools (matching harbor's own E2B/Modal/Daytona
backend-pick UX).

Phase-1 evaluation goal: seamless onboarding of swebench-verified
+ tb2 to the cluster topology without users changing their
harness code. Trainer integration (Slime, verl) for RL training
follows P1.7.

**Realistic topology assumed**: consumer talks to control plane
only; control plane reaches nodes via internal IPs or ssh
tunneling. Consumer cannot directly reach nodes (so options like
"hand the consumer a remote docker daemon endpoint" don't fit —
all routing goes through the control plane).

**Decisions locked (2026-05-06 design conversation)**:
- **Two integration plug-ins, one primitive underneath.** Drop-in
  UX is the user-facing surface; the primitive is internal.
- **`xrlenv.from_env()` already scaffolded** in
  `xrlenv/compat/docker_client.py` with a `LocalDocker` mode
  (works today) and a `Cluster` seam at the
  `ContainerControl` Protocol layer. P1.7.B fills in the cluster
  branch.
- **`XrlenvHarborEnvironment` already exists** at
  `xrlenv_plugins/harbor/environment.py`; subclasses harbor's
  `DockerEnvironment`. P1.7.C extends it to use the primitive
  (sets `capabilities.mounted=False`, routes upload/download/exec
  via gRPC) instead of `docker cp` + bind mounts.
- **Per-API translation, not byte-forwarding** (decided
  2026-05-06 after looking at the existing scaffolding).
  `XrlenvAPIClient` overrides each docker-py wire method and
  translates the call into a spec-21 command — `containers.create`
  → `CreateSandboxCommand`, `container.exec_run` →
  `RunInSandboxCommand`, etc. Most commands already exist (the
  spec-21 surface is well-developed); gaps fill in as new
  command types. **Per-rollout scoping comes free** because every
  command already carries a rollout_id and the node-side
  dispatcher already enforces ownership. The earlier "byte-
  forwarding" framing was rejected because adding label-
  injection / response-filtering for multi-tenancy puts byte-
  forwarding at the same effective complexity as per-API anyway,
  and per-API reuses existing audit events + dispatcher
  infrastructure end-to-end. The one real engineering item is
  **long-lived hijacked `exec_start` streams** (swebench uses
  these for test_output, can run 30+ min) — which the existing
  spec-21 bidi stream already supports per `RunInSandboxCommand`.
- **Image distribution stays node-side**, NOT consumer-side. The
  expected flow is node pulls images from a registry (Docker Hub
  for swebench's `is_remote_image` mode, our own image-
  distribution channel from P1.2 for ours). Consumer's harness
  never ships a build context from its host to the remote
  daemon. (`client.api.build(path=...)` *would* upload a tar
  body if a harness called it, but it's not the primary path —
  swebench's default mode pulls + tags rather than building, and
  P1.6's control-plane-driven build coordinator is the answer
  for "actually build images on nodes" anyway.)
- **swebench is unusually proxy-friendly** — research showed no
  bind mounts, no get_archive, no host filesystem coupling, no
  inbound network. The only daemon-on-same-host assumption is
  `cleanup_container`'s `os.kill(daemon_side_pid, SIGKILL)`
  fallback, which we guard for the cluster path.
- **harbor's bind-mount story** doesn't survive remote daemon,
  but harbor already handles this for its E2B / Modal / Daytona
  backends via `capabilities.mounted=False` + post-trial
  download. We follow the same pattern.

**Slices** (each landable independently, main stays green):

- **P1.7.A — Cluster-routed sandbox primitive** (~1 week, split
  into two for landability after looking at the existing
  scaffolding 2026-05-06):

  - **P1.7.A.1 — raw-container minimum** — **CLOSED 2026-05-06**.
    New spec-21 commands `AcquireContainer`, `ContainerExec`
    (batched), `ContainerDestroy` that bypass the in-sandbox stub
    and talk directly to docker. Full vertical lands in 8 commits:
    proto skeleton (`6fa03df`); consumer-facing rollout_control
    RPCs + node-side `RawContainerManager` (`139fe5d`); NodeAgent
    delegate methods + grpc_link dispatcher cases (`b484ea0`);
    NodeTransport Protocol + RemoteNodeTransport (`c58aa08`);
    audit-response tightening (Raw-Scoping-M1 + Raw-API-M1 +
    Raw-Policy-M1 in `ac518eb`); control-plane fan-out via
    `RawContainerCoordinator` + `CoordinatorRolloutService`
    methods + servicer real handlers + LocalRuntime/DistributedRuntime
    wiring (`b3c6ef4`); `ClusterContainerSession` SDK +
    `Client.acquire_container` (`799d778`); end-to-end smoke at
    `tests/smoke/raw_container_smoke.py` (this commit). Suite at
    1144/1144 with ~43 new unit tests covering manager
    behaviour, NodeAgent delegation, dispatcher serialization,
    coordinator routing, session ergonomics. The end-to-end
    smoke supports three modes (in-process / embedded /
    connect) for laptop dev + cluster validation.

  - **P1.7.A.2 — archives + streaming exec + Raw-GC-M1** —
    **CLOSED 2026-05-06** (commits ``e5efd7a`` archives,
    ``488b671`` Raw-GC reconciler, ``0ce733d`` audit response,
    ``d5071e5`` streaming-exec node-side, this commit
    streaming-exec end-to-end). All three pieces now flow end-
    to-end at the wire level: archives via
    ``ContainerPutArchive`` / ``ContainerGetArchive``; streaming
    exec via ``StreamContainerExecCommand`` with multi-reply
    queue-based fan-out (``RemoteNodeTransport._send_and_stream``);
    raw-GC via ``RawGCReconciler`` + privileged
    ``ForceDestroyContainerCommand``. Suite at 1174/1174 with
    a focused test trail at each layer.

    **Original scope (preserved for reference)** — narrowed
    2026-05-06 after a re-read
    of the existing image / archive infrastructure — earlier
    framing overclaimed new surface that's actually already
    served by the P1.2 + P1.6 image-cache layer. Three real
    items:

    1. **`RawPutArchiveCommand` + `RawGetArchiveCommand`** —
       `container_id`-keyed (case-2/3 raw containers don't have
       a `SandboxHandle`). Note: the existing `PutArchiveCommand`
       is **already direct-to-daemon** (it calls
       `self._client.containers.get(sb.backend_ref).put_archive(...)`
       — no stub involvement); the gap is identity, not data
       plane. swebench doesn't need GetArchive (its bytes ride
       the exec stream), but harbor's `download_dir` for
       non-mounted backends and OSWorld's `/file` analog do.

    2. **`StreamContainerExecCommand` + `ContainerExecChunk`
       reply** — genuinely new wire surface. Required for both
       swebench (30+ min test runs) AND tb2 (tasks running
       1-2 hours per the operator brief 2026-05-06). Reasons
       streaming wins over "batched with a long timeout":

       - **Idle-timeout safety along the full path.** Cloud LBs
         / NAT / SSH tunnel hops can drop TCP after 60s-10min
         of no traffic; gRPC HTTP/2 keepalive helps but isn't
         universally honored. Periodic chunked replies keep
         every hop provably alive — the stream's flowing
         because bytes are flowing.
       - **Memory.** 1-2 hour runs can produce hundreds of MB
         of output. Batched buffers it all in node memory +
         ships it as one gRPC message at the end. Streaming
         flushes per chunk.
       - **Watchdog-compatibility.** swebench's
         `exec_run_with_timeout` reads off a streaming socket
         to enforce real-time cancellation. Drop-in
         translation can satisfy that contract directly when
         the wire is already streaming.

       Implementation shape: the existing
       ``RemoteNodeTransport._send_and_wait`` is single-reply
       (``self._pending[cmd_id] = Future``). Streaming needs a
       parallel ``_send_and_stream`` that uses an
       ``asyncio.Queue[CommandReply]`` instead of a Future.
       Node side: dispatcher chunks the exec's stdout+stderr
       into multiple ``CommandReply`` messages all sharing the
       same ``command_id``, with the final one carrying
       ``exit_code`` + a terminator flag. Consumer side
       yields chunks as they arrive; total wall time bounded
       by the exec's ``timeout_s``, not by any keepalive.

    3. **Raw-GC-M1 reconciler** (audit carry-forward from
       P1.7.A.1 close). The existing GC layer-3 reconciler
       filters on `xrlenv.sandbox_id` — raw containers carry
       only `xrlenv.session_kind=raw` + `xrlenv.rollout_id` so
       a failed raw-destroy leaks until node-agent restart or
       manual `docker container prune --filter
       label=xrlenv.session_kind=raw`. A.2 wires a parallel
       reconciler: a new `ListRawContainersCommand` spec-21 RPC
       returns label-filtered `docker ps` results; the
       coordinator cross-checks against its in-memory session
       set and issues `DestroyContainerCommand` for orphans.

    **Explicitly NOT in A.2** — image ops as new RPCs (`Pull`,
    `List`, `Get`, `Remove`, `History`). The harness-side
    `client.images.*` calls translate to existing
    infrastructure: `pull` → `EnsurePresentCommand` (P1.6.g);
    `get` (existence) → `QueryImageCommand` (P1.2.a); `list` →
    `ReportImagesCommand` (P1.2.c); `remove` → bump LRU touch,
    don't actually remove (cache eviction owns lifetime);
    `history` → empty / minimal. Those translations live in
    **P1.7.B** as the `xrlenv.from_env()` Cluster-mode adapter
    that overrides `XrlenvAPIClient`'s docker-py manager
    methods. We don't reinvent the image-cache wheel.

  Token-scoped multi-tenancy applies to both A.1 and A.2 — every
  command carries a rollout_id, the node-side dispatcher
  enforces ownership, consumer A cannot touch consumer B's
  containers. No user-facing surface; this is internal
  infrastructure that P1.7.B (docker drop-in) and P1.7.C
  (harbor plug-in) call into.

- **P1.7.B — `xrlenv.from_env()` cluster mode** —
  **CLOSED 2026-05-06** (commits ``48b89cf`` lifecycle,
  ``e825923`` exec triple + archives, ``a61471c`` streaming
  exec via sync↔async bridge, ``7fd3476`` image-ops adapter
  with operator-pre-pulled contract, ``9340386`` audit response
  closing Cluster-Dropin-M1/M2 + Raw-Stream-M1, plus this
  commit's end-to-end smoke). The drop-in's docker-py manager
  surface (containers.create / start / stop / remove + exec
  batched + exec streaming + put_archive / get_archive +
  inspect_image / pull / images / remove_image / history) is
  fully wired, with 30 unit tests covering routing, ownership
  enforcement, the sync↔async streaming bridge, and the
  unwired-method safety net. End-to-end smoke at
  ``tests/smoke/dropin_cluster_smoke.py`` validates the full
  vertical (create + put_archive + batched exec + streaming
  exec + get_archive + remove) against in-process /
  embedded / connect topologies. Suite at 1205/1205.

  **Original scope (preserved for reference)** — decided
  2026-05-06 (after looking at the existing scaffolding):
  per-API translation, NOT byte-forwarding. The
  scaffolded `ContainerControl` / `XrlenvAPIClient` seam in
  `xrlenv/compat/docker_client.py` gets a `Cluster` mode that
  overrides each docker-py manager method and routes to a
  matching gRPC RPC:

  - **Container methods** → P1.7.A.1's raw-container RPCs
    (`Client.acquire_container` / `session.exec` /
    `session.destroy`) plus P1.7.A.2's
    `RawPutArchive` / `RawGetArchive` / streaming exec.

  - **Image methods** (this slice owns the image-ops adapter):
    `client.images.pull(name)` → `EnsurePresentCommand`
    (P1.6.g); `client.images.get(name)` → `QueryImageCommand`
    (P1.2.a); `client.images.list(...)` →
    `ReportImagesCommand` (P1.2.c); `client.images.remove(...)`
    → bump LRU touch only (cache eviction owns lifetime);
    `image.history()` → empty / minimal. **No new image-ops
    RPCs** — the existing image-cache layer is the canonical
    strategy and this slice plugs the harness's docker-SDK
    surface into it. Mechanism not policy: harness keeps using
    the docker SDK it already knows; xrlenv routes the calls
    through the cluster.

  After this: swebench's `run_evaluation.main()` works
  unchanged under the cluster topology — same monkey-patch
  pattern as today's `tests/smoke/test_swebench_drop_in.py`
  (`docker.from_env = xrlenv.from_env`), retargeted at
  `xrlenv up` instead of local docker. Validation: 1 instance
  through cluster on 2 nodes; retarget the existing smoke as
  the gate.

- **P1.7.C — `XrlenvHarborEnvironment` cluster mode** (~1
  week). Extend the existing plug-in's `upload_dir /
  download_dir / exec` to call the primitive instead of `docker
  cp`. Set `capabilities.mounted=False` so harbor's `trial.py`
  takes its existing post-trial download branch. After this: tb2
  works unchanged under the cluster topology; harbor users add
  our `import_path` in their job.yaml exactly as they would for
  E2B / Modal. Validation: 1 task (`fix-git`) through cluster
  on 2 nodes; retarget `tests/smoke/test_terminal_bench_2_drop_in.py`.

- **P1.7.D — In-tree plug-in deletion + audit** (~3 days).
  Delete `xrlenv_plugins/benchmarks/tb2/` and
  `xrlenv_plugins/benchmarks/swebench_verified/` (the in-tree
  EnvAdapter-shaped plug-ins shipped in phase 0 + P1.4). Their
  roles are subsumed by the drop-in approach. Update spec 06
  (templates) to acknowledge the substrate-vs-EnvAdapter split:
  case-2/3 benchmarks plug in via their harness's own extension
  mechanism, NOT via xrlenv's `EnvAdapter` Protocol. The
  EnvAdapter contract stays for case 1 (RL training, Pattern A
  templates like hello-shell + future per-trainer rollout
  shapes). Cleanup of associated docs / tests / templates.

**Out of scope for P1.7 (deferred)**:
- Auto-discovery of harness-native artifact paths (case 3
  installed-agent's trajectory log paths vary per agent — punt).
- Trainer integration (Slime / verl) — P1.3 follows P1.7.
- OSWorld plug-in — P1.5; re-scope under the slim pivot at slice
  start.
- Multi-tenant ACL on the primitive beyond per-token rollout
  scoping — phase-2 territory.

**P1.7 acceptance gate** (new shape, replaces the original Q1
gold-patch-oracle gate's plumbing — same intent, different
mechanism):

- 1 swebench-verified instance through `xrlenv up` cluster
  topology, sealed `finished` with `resolved=True`. Same
  smoke-test code path as today's
  `tests/smoke/test_swebench_drop_in.py`, just with the docker
  client routed through cluster mode.
- 1 tb2 task (`fix-git`) through `xrlenv up` cluster topology,
  sealed with all rewards positive. Same smoke as today's
  `tests/smoke/test_terminal_bench_2_drop_in.py` retargeted.
- Both via the consumer-on-laptop / control-plane-on-laptop /
  nodes-on-2-VMs topology (matches the realistic case from
  Section A above).
- In-tree benchmark plug-ins deleted; xrlenv core has zero
  benchmark-specific code under `xrlenv/`.

After P1.7 the platform supports both the original phase-1
Gate-1 spirit (real benchmarks running through xrlenv on a real
cluster) and the broader "case 2/3 evaluation works seamlessly"
goal — without forcing the EnvAdapter shape onto every benchmark.
The original Slime-driven correctness gate (Q1) becomes a
post-P1.7 P1.3 deliverable with the trainer integration.

### Slice P1.6 — Control-plane-driven image builds (~2 weeks)

**Why**: today the operator SSHes into every VM and runs
``build-task-images.sh`` by hand. Doesn't scale past a couple of
benchmarks × handful of VMs. Phase-1 ships an `xrlenv build apply
build-plan.yaml` flow that the operator runs once from the control
plane; control plane bin-packs (image, node) assignments respecting
disk budget + replication, dispatches to each node-agent over the
spec-21 stream, persists snapshot + drift, surfaces in admin.

**Decisions locked (2026-05-04 design conversation)**:
- **Architecture**: build-on-each-assigned-node (not build-once-ship-many) — phase-2 can revisit if bandwidth hurts.
- **Plan format**: separate `build-plan.yaml` (NOT extend spec-16's per-benchmark `plan.yaml`); cluster-wide.
- **Imperative + declarative**: declarative is source of truth. Imperative shorthand `xrlenv build apply --benchmark X --smoke` lowers in the CLI to a transient `build-plan.yaml` and feeds the same RPC.
- **State**: applied snapshot persisted in state.db (`build_plans` + `build_plan_assignments`); operator's file is the input.
- **Node-side execution**: Python in-process on the node-agent. Each plug-in ships a `BenchmarkImageBuilder` Protocol implementation next to its adapter — same plug-in mechanism `EnvAdapter` / `InstanceResolver` already use. Manifest gains optional `image_builder: {module, class}` block.
- **Replication**: R=1 default (per-image override allowed); per-plan and per-image override knobs in schema.
- **Bin-packing**: FFD (largest-first; pick the R nodes with most free disk; spread tie-break). Static per-builder size hints (`Tb2ImageBuilder.IMAGE_SIZE_HINT_BYTES = 1 GB`, swebench = 3 GB).
- **Idempotency**: 5 layers — plan_id (sha256 of canonicalised plan), per-assignment row, node-side skip-if-tag-exists, R-residual replanning, permissive supersession (cache LRU evicts; planner doesn't actively delete).
- **Node-loss**: manual reconcile (R=1 → operator re-applies); concurrency control rejects second `apply` while one is `in_flight`; failures fail-fast and re-applies retry.
- **Bash scripts**: become thin shims that lower to `xrlenv build apply` against an ephemeral LocalRuntime — same Python builder runs in either case.
- **Disk budget defaults**: `reserved_runtime_gb=30`, `buffer_gb=10` (cloud VMs typically 200-500 GB → ~160-460 GB available for image cache).

**Slices** (each landable independently, main stays green):

- **P1.6.a** — Plug-in builder Protocol + manifest field (~3 days). Core: `xrlenv/node/image_builder.py` (Protocol + `BuilderRegistry`). Manifest schema + catalog plumbing for the `image_builder:` field. Plug-in side: `Tb2ImageBuilder`, `SwebenchVerifiedImageBuilder` ported from bash to Python. Bash scripts stay primary in this slice — Python builders are wired only to a unit-test entry point.
- **P1.6.b** — Proto + state-store + planner + LocalRuntime end-to-end (~4 days). `BuildImagesCommand` proto. State tables. `xrlenv/control/image_planner.py` (FFD). `xrlenv/control/build_coordinator.py`. `xrlenv build apply [--dry-run]` CLI. LocalRuntime path complete; fake-Docker tests gate.
- **P1.6.c** — Distributed dispatch (~3 days). Spec-21 wire-up. Heartbeat extension (`disk_used_bytes`, `disk_capacity_bytes`, `disk_reserved_bytes`). `xrlenv build apply` against connect-mode token. `xrlenv build status [--plan PLAN_ID]` showing snapshot-vs-actual drift.
- **P1.6.d** — Admin UI + bash shim + docs (~2 days). `/builds` admin page. Bash `build-task-images.sh` becomes `xrlenv build apply` shim. Docs: `docs/operator/build-plans.md`, top-level operator guide section "Building images: control-plane vs SSH", per-benchmark `operator.md` Step-4 update. Algorithm-deep prose (bin-packing, idempotency layers) forks to `docs/technical/build_plan_<topic>.md` if it grows past ~150 lines (per the user's existing technical-docs rubric).
- **P1.6.e** — Hardening (~2 days). `xrlenv build cancel`. Concurrency control. Failure-surface polish. Edge-case tests (Dockerhub 503, node disconnect, insufficient capacity).

**Out of scope (phase-2)**: registry mirror / P2P (Dockerhub rate-limit decision deferred), demand-weighted replication, auto-rebuild on heartbeat-lost, strict supersession (planner-driven cache eviction).

### Slice P1.5 — Scale gate + remaining surface (~3-4 weeks)

The Redis-backed load-test gate + the remaining phase-1 features
that aren't gated on the correctness smoke.

- **Scale gate driver**: 500-1k concurrent `hello-shell` rollouts
  on a Redis-backed control plane; latency targets met.
- B4.2 — Function-Call mode + `client.invoke()` (Q5).
- B4.3, B4.4 — warm pools + 24h ring buffer.
- B6.2, B6.3 — active+standby control plane, pool-accounting
  persistence.
- B6.4, B6.5 — signed audit archive, object-store integration
  (opt-in).
- B7.1–B7.12 — observability + admin panel + trajectory viewer
  extensions.
- B5.2, B5.3 — token rotation, signed manifests.
- B5.5, B5.6 — egress-allowlist networking + audit (required for
  B2.3 web-search).
- B2.2 — OSWorld plug-in (Pattern B; first asset-fetcher
  exercise).
- B2.3 — web-search plug-in.
- B3.5 — eStargz lazy-load image support (Docker only).
- B8.1 — `xrlenv bootstrap` subcommand.
- B8.3 — optional cluster-wide registry mirror.
- B9.1, B9.2 — `xrlenv analyze` CLI + `--remote` mode.
- B1.6 — reward modes `external_final` and `token_level`
  (lands with B7.8 — the trajectory viewer's token overlay).

### Slice P1.x — Hardening (post-acceptance)

Lands as a separate cycle once both gates pass:

- mTLS for node-control stream (Q7 — B5.1).
- Consumer liveness stream for raw sessions — replace the control plane's
  infer-death-from-silence with a transport-level signal. Design + decision record
  in `notes/design-consumer-liveness-contract.md` (option D); touches specs
  03/05/19/21 + the consumer SDK.

  **Re-evaluate the premise before building this.** It was slotted when the
  reaper destroyed a session after 120 s of silence, so it carried two benefits:
  a dead consumer reclaimed immediately, and a frozen-but-alive one never
  destroyed. The C+E quarantine has since shipped and delivered the second —
  measured consumer stalls in the 2026-08-19 incident were median 199 s, max
  946 s, so the 900 s horizon survives 16 of 17 — and a session that does outlast
  the horizon is now retried as infra rather than scored as a content failure.

  What remains is the first benefit alone: reclaiming a genuinely dead consumer's
  capacity in seconds instead of ~15 minutes. That is a capacity-efficiency
  argument, not a correctness one, and it should be weighed against a new bidi
  RPC surface across four specs. It may still be worth it — a short horizon with
  no false positives is strictly better than a long one — but it is a materially
  weaker case than the entry originally implied, and whoever picks this up should
  not inherit the stale premise.
- Any deferrable items from P1.5 that didn't fit.

---

## Section D — Acceptance criteria detail

(Final form lives in `notes/phase-1-acceptance.md`.)

### Gate 1 — Correctness gate

| Check | How to verify |
|---|---|
| 8 oracle-driven SWE-bench Verified rollouts seal `finished` | Driver prints `8 / 8 rollouts sealed as finished`; or `xrlenv rollouts --template swebench-verified --status finished` shows 8 |
| Distribution across both VMs | Driver prints `across 2 node(s)`; per-rollout `node_id` shows split |
| Slime adapter is real (not stubbed) | Code path: `xrlenv/adapters/slime.py::make_rollout_fn` is invoked; only the policy is replaced with the gold-patch oracle |
| Per-task image overlay worked | `xrlenv events --rollout <id> --kind rollout.start` payload shows the per-instance `docker_image` |
| Image affinity verified | `xrlenv events ... --kind placement.image_present` shows `true` for each rollout |
| Pre-flight image check works | Integration test: instance image only on one node, schedules to bare node, fails with `reason="image_missing"` |
| Node-token auth gated the bidi stream | `xrlenv audit --kind auth.token_used` shows ≥2 entries; `auth.denied` = 0 |
| External plug-in package mechanism works | `xrlenv-swebench-verified` is `pip install`-able from a separate directory and registers in the catalog without source edits |
| Trajectory viewer renders SWE-bench rollouts | Open `/rollouts/<id>` in admin; step list, actions, observations all visible |

### Gate 2 — Scale gate

| Check | How to verify |
|---|---|
| 500-1k concurrent rollouts sustained for ≥10 min | Load-test driver maintains target concurrency on Redis-backed control plane, ≥2 VMs |
| Median rollout-creation latency < 1 s | p50 measured by the driver |
| 99p rollout-creation latency < 5 s | p99 measured by the driver |
| No transient-state pinning >5 min | Periodic check: 0 rows in `cancelling`/`finishing` for >5 min during the load test |
| Redis StateStore correctness preserved | Throughout the load test: 0 lost rollout records, 0 inconsistent state transitions, audit log clean |

---

## Section E — Open question (one remaining)

Q1, Q3, Q4, Q5, Q6, Q7 all closed. One residual:

### Q8. SWE-bench Verified scale-gate dataset

**The question:** Gate 2's load test uses `hello-shell` mocks
(load on the bookkeeping, not the sandboxes). Should we *also*
require Gate 2 to use a SWE-bench Verified subset (e.g., 32 or
64 instances at sustained concurrency)? Cost is real: 500 SWE-
bench images × 2 VMs at ~1 GB/instance ≈ 1 TB per VM.

**Options:**

- (a) Gate 2 uses cheap mocks only. Validates the control plane's
  bookkeeping under load; doesn't validate the data-plane's
  per-image overhead at scale.
- (b) Gate 2 uses a SWE-bench Verified subset. Validates both the
  control plane's bookkeeping AND the image-cache's eviction
  behavior under real per-image disk pressure.
- (c) Both: Gate 2a is mocks-at-1k (correctness of scale); Gate
  2b is SWE-bench-at-32 (image-cache discipline at real load).

**Recommendation:** (c). The load-vs-image-cache tests are
orthogonal; both are scope. Gate 2a runs in CI-like loops; Gate
2b is the operator-driven cousin of phase-0's tb2 acceptance.

---

## Section F — Out of scope for phase 1

Recorded so reviewers don't ask. These all ship in phase 1.x or
phase 2:

- **Phase 1.x hardening** (post-correctness-gate):
  - mTLS for node-control stream (Q7, B5.1).
- **Phase 2** (CubeSandbox-dependent or autoscale):
  - CubeSandbox backend (Q3, B4.1).
  - CubeSandbox bootstrap (B8.2).
  - Mixed-backend capacity accounting (A8 / D14).
  - overlaybd lazy-load image support (B3.5 partial).
  - k8s/MIG/ASG autoscale.
  - Sandbox checkpoint/branch.
  - Predictive capacity (EMA + load forecasting).
  - Multi-tenant isolation.
  - **Redis StateStore (B6.1) + Redis bootstrap recipe (B8.4) +
    active+warm-standby control plane (B6.2)** — moved out of
    phase 1 on 2026-05-02 (Q4 revised). Phase 1 stays SQLite at
    100-200 concurrent; phase 2 lifts the target alongside the
    other scale-out work. Pre-baked design answers (StateStore
    URL scheme, schema-mirror approach, atomicity strategy, test
    strategy, migration-tool deferral) recorded in
    `notes/deferred_audit_todos.md` so phase-2 doesn't relitigate.
- **Phase 3:**
  - Sandbox sessions with preemption-safe resume (the design
    hooks B10.x stay; execution lands phase 3).
  - Extreme density (KSM, overcommit, 3FS).

---

## Bottom line

**Updated 2026-05-06 for the slim pivot.** Phase 1 sequencing as
of today:

| # | Slice | Status | Cost |
|---|---|---|---|
| 1 | **P1.1 — Phase-0 carry-forward** | CLOSED | ~2 weeks |
| 2 | **P1.1.5 — External plug-in mechanism** | CLOSED | ~1 week |
| 3 | **P1.2 — Image distribution** | CLOSED | ~3 weeks |
| 4 | **P1.4 — SWE-bench Verified in-tree plug-in** | CLOSED, **superseded by P1.7** | (~3 weeks shipped) |
| 5 | **P1.7 — Slim pivot: cluster substrate for evaluation harnesses** | **NEXT** | ~2-3 weeks |
| 6 | **P1.3 — Slime + verl adapters** | deferred (post-P1.7) | ~3 weeks |
| 7 | **P1.6 — Control-plane-driven image builds** | queued, **re-scope at start** | ~2 weeks |
| 8 | **P1.5 — Scale gate + remaining surface** | queued | ~3-4 weeks |
| 9 | **P1.x — Hardening (mTLS, …)** | post-acceptance | varies |

CubeSandbox + microVM + **Redis StateStore** stay in phase 2.

**Acceptance shape** (slim-pivot reframing):

- **P1.7 gate** (new, replaces the Q1 plumbing): swebench-
  verified single-instance + tb2 single-task each run end-to-
  end through `xrlenv up` cluster topology, via the user's
  unmodified harness code (drop-in `xrlenv.from_env` for
  swebench, `import_path` plug-in for harbor). In-tree
  benchmark plug-ins deleted; xrlenv core carries zero
  benchmark-specific code.
- **Gate 1 — Slime correctness** (deferred to post-P1.7 P1.3):
  Slime-adapter-driven oracle rollouts on 2 VMs, all
  `finished`. Same intent as the original Q1 gate; the
  swebench plumbing now goes through P1.7's drop-in path
  rather than an in-tree EnvAdapter.
- **Gate 2 — Scale**: 100-200 concurrent rollouts sustained on
  the SQLite-backed control plane with median creation
  latency <1 s, 99p <5 s. Unchanged.
