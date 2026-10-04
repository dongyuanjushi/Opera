## Deferred items from prior audits — status roll-up

Single source of truth for the D-numbered backlog. `notes/phase-1-to-do.md`
references these IDs but does not duplicate the description; come here for
the per-item rationale and closing-commit ref.

| Status | Items |
|---|---|
| **CLOSED** | D1 (`bd5c42b`), D2 (`bd5c42b`), D3 (`bd5c42b`), D4 (`bd5c42b`), D6 (`d162256`), D7 (`bd5c42b`), D9 (`bd5c42b`), D11 (closed earlier), D12.1 (closed earlier), D13 (P1.1, commit c5226ff), D15 (P1.1, commit 82c914e), D16 (P1.2.b primitive, commit `a3cdf83` — soak-test acceptance still open), D17 stage 1 (P1.1, commit 2bb8edc), D17 stage 2 (P1.2.b, commit `d56eb78`), D18 + D19 + D20 (P1.2.a image distribution umbrella, see commit list under D18 below), D21 (`d4bc66e`), D22 (sandbox import-path extension; runtime fix in `44fd81b`, B11.6 worked examples in `ac8ff84` — extra_plugin_roots + PYTHONPATH layering), D-AR-2026-05-05-H1 (P1.6.f, commit `cb99f7d`), D-AR-2026-05-05-M1/M2a/M3 (P1.6.b/c follow-ups), D-AR-2026-05-05-H3 (P1.6.g umbrella — `78d6242` / `84a85e0` / `ec8d3b4` / `6f05eb7` / `be58db8`), D-AR-2026-05-05-H2 (P1.6.g routing — broadcast + soft preferred_home bonus) |
| **DEFERRED-BY-DESIGN** | D5, D8 (sandbox-stub / CLI entry-point smoke — not worth subprocess test cost until those grow real CLI surface) |
| **DEFERRED to phase 2** | B6.1 (Redis StateStore) + B6.2 (active+standby control plane) + B8.4 (Redis bootstrap recipe) — phase-1 stays SQLite at 100-200 concurrent; full design-answer pre-bake recorded under "B6.1 (deferred to phase 2)" below so phase-2 doesn't relitigate. |
| **OPEN — slotted into phase-1 plan** | D10 (tb2 adversarial reward isolation), D12.2 (UID separation), D14 (DEFERRED to phase 2 with CubeSandbox), D-AR-2026-07-07-B (cross-node re-admit for saturated-node create overflow — phase-1+, needs a multi-node sysbox pool) |

Keep this list as historical follow-up; the phase-1 plan is the active
work-tracking surface, this file is the per-item detail.

### D1. Reward-mode edge: `in_sandbox_final` on a cancelled rollout — CLOSED

- Closed in `bd5c42b` (2026-04-29).
- `tests/unit/test_reward_modes.py::test_in_sandbox_final_skipped_when_cancelled_before_done`
  pins the contract: cancel-before-done bypasses the grader entirely
  (no `grader.sh` invocation, `final_reward == 0.0`).

### D2. `LocalRuntime.start()` with `metrics_port` set — CLOSED

- Closed in `bd5c42b` (2026-04-29).
- `tests/unit/test_runtime_and_cli.py::test_build_local_runtime_metrics_port_lifecycle`
  drives the start/serve(/metrics)/stop cycle with kernel-assigned port
  (`metrics_port=0`).

### D3. `hw_probe.probe_hardware()` direct test — CLOSED

- Closed in `bd5c42b` (2026-04-29).
- `tests/unit/test_node_agent.py` adds four direct tests covering the
  Linux `/proc/meminfo` parse, the Darwin `sysctl hw.memsize` parse,
  the 4 GiB fallback floor, and the full `probe_hardware()` record
  assembly (all monkeypatched, deterministic).

### D4. `api/converters.py` resource/mount round-trip pairs — CLOSED

- Closed in `bd5c42b` (2026-04-29).
- New `tests/unit/test_api_converters.py` pins the
  `resource_usage_to/from_proto` and `mount_spec_to/from_proto`
  round-trips directly (separate from the transitive coverage in
  `test_grpc_link.py`).

### D5. CLI entry point smoke tests

- Partially covered historically: `xrlenv-node serve` has observability
  coverage. The sandbox-stub `python -m xrlenv.sandbox_stub` argv path still
  appears deferred.
- Keep deferred until the stub entry point grows non-trivial options or a
  subprocess integration test boots it directly.

### D6. Shutdown drain tests use hardcoded `/tmp/` paths — CLOSED

- Closed in `d162256` (2026-04-29).
- The two shutdown-drain tests in `tests/unit/test_runtime_and_cli.py`
  now use the `tmp_path` fixture; no literal `/tmp/_xrlenv_shutdown_test_*`
  paths remain.

### D7. Shutdown-with-grace-expired final-state assertion — CLOSED

- Closed in `bd5c42b` (2026-04-29).
- `test_shutdown_cancels_pending_and_tears_down_watchers_when_grace_expires`
  in `tests/unit/test_runtime_and_cli.py` now also pins the surviving
  rollout's terminal state: `LocalRuntime.shutdown` does NOT seal
  in-flight rollouts; they remain `RUNNING` so the next-process
  recovery loop can reclaim them (spec 03 + spec 20).

### D8. `xrlenv/sandbox_stub/__main__.py` entry-point smoke

- Still deferred. `tests/unit/test_sandbox_stub_server.py` covers the server
  directly, but the `__main__.py` argv parser / transport selection path is not
  exercised as a standalone entry point.
- Defer until the entry point gains meaningful CLI behavior or an integration
  subprocess test is worth the cost.

### D9. SQLite migration coverage for `effective_resources_json` — CLOSED

- Closed in `bd5c42b` (2026-04-29).
- `tests/unit/test_sqlite_state.py::test_sqlite_migration_adds_effective_resources_json_and_image`
  pre-seeds a database with a pre-9b `sandboxes` table, opens it via
  `SqliteStateStore`, and asserts the additive migration adds both
  `effective_resources_json` and `image` columns; legacy rows return
  `None` and new rows round-trip the snapshots.
  `test_sqlite_migration_is_idempotent_when_columns_already_present`
  pins the second-open contract (no error, columns stay).

### D10. terminal-bench-2 adversarial reward-wrapper coverage

- Partially closed by `d075cf4`: tests now cover pre-seeded `reward.json` and
  `reward.txt` being wiped before final reward.
- The chmod-based mitigation that landed in `d075cf4` was *removed* in the
  follow-up audit cycle (the final image runs as root, so the chmod was
  theater). The wipe contract stands; the isolation property does not.
- After D12 stage 1: timing isolation makes the property finally testable.
  Add a Docker-level test that boots the per-task image, asserts
  `/tests/test.sh` and `/opt/xrlenv/run-task-tests.sh` do NOT exist before
  reward time, then runs `compute_in_sandbox_final_reward` with the
  resolver's verifier_uploads and asserts the grader runs successfully and
  the files exist *only* during the reward phase. Test-coverage follow-up to
  this slice — not blocking phase-0.

### D11. Adapter timeout integration coverage — CLOSED

- Process-tree half closed by `d075cf4`: the descendant-cleanup test covers
  shell process groups on timeout.
- Control-plane half closed by the second-pass audit response: coordinator now
  seals `RolloutStatus.TRUNCATED` with `reason="step_timeout"`, destroys the
  sandbox, and skips `in_sandbox_final` reward.
  `tests/unit/test_coordinator.py::test_step_truncated_seals_as_truncated_with_step_timeout`
  and `::test_step_truncated_skips_in_sandbox_final_reward` pin both halves.

## Open phase-0-acceptance follow-ups

These are NOT test-coverage gaps; they are acknowledged design/runtime gaps
that phase-0 ships with explicit operator-facing caveats. Each one needs a
dedicated post-phase-0 slice.

### D12 stage 1. Timing-isolated grader injection — CLOSED

Borrowed harbor's pattern (see
`harbor.verifier.verifier.Verifier.verify`): tarballs of `tests/`
and the wrapper script are uploaded into the sandbox at reward
time via the new `PutArchiveCommand` bidi primitive, never baked
into the per-task image. The agent's `step()` loop runs against
a filesystem that does not contain the grader, closing H1's
"agent reads /tests/test.sh to learn the checks" + "agent
modifies the wrapper" + "agent pre-seeds reward.txt" attacks at
once.

Implementation:

- New `BackendAdapter.put_archive` + Docker `put_archive` helper
  (uses Docker's existing put_archive API + a root mkdir -p).
- New proto `PutArchiveCommand` / `PutArchiveReply` in
  `node_control.proto`; node-side dispatch in `grpc_link.py`;
  in-process and remote `NodeTransport.put_archive`.
- New `VerifierUpload` model + `ResolvedInstance.verifier_uploads`
  field. The tb2 resolver tarballs the per-task `tests/` dir and
  the static wrapper script at resolve time.
- Coordinator threads `verifier_uploads` from start_rollout into
  `compute_in_sandbox_final_reward`, which `rm -rf`s the target
  as root, then `put_archive`s, then runs the grader.
- `build-task-images.sh` runs two stages: stage 1 builds the
  upstream task env (tagged `terminal-bench-2-base/<task>:0.1`),
  stage 2 layers the platform's stub-runtime via
  ``xrlenv stub-runtime layer`` (apt-installs python+pip when the
  base lacks them, then pip-installs pydantic/aiohttp/pyyaml).
  Final tag `terminal-bench-2/<task>:0.1` is what the resolver
  returns. The stub-runtime layer is centralized (single
  ``xrlenv/sandbox_stub/Dockerfile.stub-runtime``) and preserves
  the upstream image's USER so installing as root for
  apt+pip doesn't permanently flip the runtime user — closes the
  audit-M1 (post-9948609) follow-up. Grader assets (``/tests/``
  and ``run-task-tests.sh``) are still platform-uploaded at reward
  time, NEVER baked into the image.
- Tests in `tests/unit/test_reward_modes.py` pin the
  upload-before-grade contract; new tb2 resolver tests pin the
  tarball contents and the missing-tests graceful path.

D10 (the adversarial Docker-level test that proves an agent step
cannot read or modify `/tests/test.sh`) is now feasible because
the file isn't there during step(); follow-up.

### D12 stage 2. UID separation between agent and verifier

- Stage 1 closes timing isolation. Identity isolation (agent and
  verifier running as different UIDs) is still open. fix-git and
  the rest of the phase-0 task set declare `[agent].user = None`
  and `[verifier].user = None` in `task.toml`, so the verifier
  runs as the image's USER (often root) just like the agent did.
- Implementation sketch: extend the backend `exec` primitive with
  an optional `user` parameter (Docker maps directly to
  `docker exec --user`); extend the resolver to read
  `[agent].user` and `[verifier].user` from `task.toml` and stash
  on `ResolvedInstance` (or directly on a SandboxRecord field for
  per-step user context vs reward user context); thread through
  `compute_in_sandbox_final_reward` so the wrapper exec runs as
  the verifier user.
- Acceptance: a backend test that proves a sandbox started with
  `agent.user = "sandbox"` and `verifier.user = None` lets the
  reward exec do things the agent couldn't (read a root-owned
  `/etc/shadow`-style sentinel file).

### D13. Scheduler placement scoring uses count, not effective-resource load — CLOSED

Closed in P1.1 (commit c5226ff). `CapacityEstimator` gained
`slack_after_placement(node, candidate, running, *, backend) -> float`
that returns the minimum-axis remaining slack fraction (CPU + mem +
sandbox-writable disk) after hypothetically placing the candidate;
`Scheduler.place()` now scores each fitting node as
`round(slack * 1000)` instead of the pre-D13
`max_concurrent - same_template_count`. Higher = more headroom across
all axes the candidate actually requests (axes with `request <= 0`
are excluded so a CPU-only template doesn't see disk slack drag the
score artificially).

Tests pin (a) the heterogeneous regression case — node already
running a heavy *different* template loses to an unloaded node, the
exact case the count-based metric got wrong, (b) round-robin on
identical-size nodes via the natural slack-shrinks-after-placement
property, (c) opt-out semantics for templates that request only one
axis, (d) the 0.0 floor when the candidate would overshoot a
requested axis, and (e) the all-axes-opted-out fallback to 1.0.

The `Placement.score` field stays `int` for compatibility with
synthetic test fixtures (`Placement(score=1)`); semantically it now
carries the load-vector score in 0-1000 instead of slots-remaining.

### D14. Mixed-backend capacity accounting

- Phase-0 cluster is single-backend (Docker everywhere), and the capacity
  estimator charges running-sandbox overhead with the *candidate* backend's
  overhead figure rather than the per-sandbox-actual backend overhead. Fine
  while every node runs only Docker.
- Post-phase-1 (when CubeSandbox lands): `_PendingPlacement` and
  `SandboxRecord` need to carry their backend tag, and the capacity load
  computation needs to charge the matching overhead per running sandbox.
- Acceptance: a mixed Docker/Cube cluster fixture in
  `tests/unit/test_capacity.py` that exercises the per-sandbox accounting.

### D16. Automatic image-cache eviction policy (phase-1) — **PRIMITIVE CLOSED**

- Eviction primitive closed in `a3cdf83` (P1.2.b [1/3]). The
  `ImageCacheManager._cold_lru_order` cold-image sort now keys on
  `(eviction_tier, lru_ts)` instead of pure LRU; final task tags
  evict before stub-runtime layers, which evict before base images.
  The classifier defaults to recognising the harbor / tb2 convention
  (`<bench>-base/<task>:V` is base; everything else is final) and is
  pluggable via `ImageCacheManager(tier_classifier=...)` for external
  benchmarks with non-harbor tag conventions.
- Operator runbook authored in `a3cdf83`'s P1.2.b [3/3] commit at
  `docs/deployment/images.md` ("Cache eviction tuning" section).
- Soak-test acceptance ("constrained-disk VM that runs rollouts
  across more tasks than fit; the platform builds, evicts, and
  rebuilds without operator intervention") **remains open** — it
  needs a multi-task tb2 build recipe that drives cache turnover at
  scale, which slots into the broader scale-gate work in P1.5.

- **Goal**: when a VM's disk fills, image eviction happens
  automatically. Operator should never have to ``docker rmi`` /
  ``docker image prune`` by hand unless they want to debug.
- Phase-0 ships manual eviction (operator-run ``docker rmi`` /
  ``docker image prune`` when needed); fine because the 8-task
  acceptance set fits comfortably on any reasonable VM. The
  "Eviction order" runbook section the audit had pointed at was
  never authored — phase-1 should produce one alongside this fix.
- Phase-1 expectation: when a rollout requires a task image that
  isn't in the cache, the platform either
  (a) runs ``build-task-images.sh <task>`` on demand, or
  (b) pulls from a registry if one is wired (post-phase-0 image
      distribution work),
  and may evict the least-recently-used **final** tag if disk slack
  drops below a configurable threshold. The base tag
  (``<bench>-base/<task>:0.1``) is preserved as long as possible
  because losing it forces a full upstream Dockerfile rebuild —
  by far the most expensive layer to recreate.
- Implementation outline:
  - Existing scaffolding: ``xrlenv/node/image_cache.py``,
    ``xrlenv warmup``, image-pin list. Extend with disk-pressure-
    driven eviction.
  - Eviction tier order (cheap-to-recreate first):
    1. Final ``<bench>/<task>`` tags (rebuild = retag or one
       ``RUN pip install`` layer).
    2. Stub-runtime layers (rebuild = one ``RUN apt+pip`` layer
       per base flavor; cached if a sibling image still pinned).
    3. Base ``<bench>-base/<task>`` tags (rebuild = full upstream
       Dockerfile build, minutes per task).
  - Surface: ``image_cache.evict_to_target(free_bytes_min, ...)``
    primitive triggered on rollout-create when the per-image
    cache detects insufficient slack.
  - Integrate with the existing image-cache LRU + pin list so
    operator-pinned tasks don't get evicted.
- Acceptance: a soak test on a constrained-disk VM that runs
  rollouts across more tasks than fit; the platform builds,
  evicts, and rebuilds without operator intervention. The base
  tag survives across multiple eviction passes (validates the
  tier ordering).

### D17. Per-call HTTP timeout plumb-through (stub_client) — **STAGES 1 + 2 CLOSED**

**Stage 2 (closed in P1.2.b, commit `d56eb78`)**: each
`EnvSetupCommand` / `EnvStepCommand` / `EnvTeardownCommand` proto
now carries its own `request_timeout_s` field (default 0.0 = unset,
falls back to the per-sandbox stage-1 cap). The control plane
derives the per-call value from the matching manifest phase budget
+ 60 s buffer (`_per_phase_http_cap` in
`xrlenv/control/coordinator.py`); the node-side dispatcher
(`_per_call_cap` in `xrlenv/node/grpc_link.py`) decodes the field
and forwards it as a kwarg through `NodeAgent.env_*` →
`StubClient.env_*` → an `aiohttp.ClientTimeout` override on that
single HTTP request. Backward-compatible with old control planes
(field defaults to 0.0 → decoder maps to `None` → per-sandbox
stage-1 cap continues to apply). Tests pin the StubClient aiohttp
override (slow-server fixture), the NodeAgent kwarg flow, the
gRPC wire round-trip both with and without the field set, and the
coordinator's per-phase derivation for all three calls. Layer-3
description in `docs/developer/timeouts_internals.md` updated to
match.



**Stage 1 (closed in P1.1, commits 2bb8edc initial + 38b0bb3 audit
response)**: per-sandbox HTTP cap derived from the manifest's max
phase timeout + 60 s buffer. The coordinator passes the cap as a
``stub_request_timeout_s`` kwarg through
``NodeTransport.create_sandbox`` (proto field 7 on
``CreateSandboxCommand``); :py:meth:`NodeAgent.create_sandbox`
stages it on the per-sandbox record BEFORE returning the handle so
the very first stub-touching call (``init_cmd``, ``env_setup``, …)
sees the manifest-derived cap rather than the 1 h
``NodeAgentConfig.stub_request_timeout_s`` default.

Audit H2 response: the earlier path injected the cap through
``env_setup``'s ``init_params``, which got bypassed by manifests
with ``init_cmd`` because ``run_in_sandbox`` triggered ``_stub_for``
(building the ``StubClient`` with the 1 h default) before
``env_setup`` could stage the cap. The create-sandbox-time path
closes that gap. ``_set_stub_request_timeout`` is also defensive —
late-stage cap changes close the existing stub so the next
``_stub_for`` rebuilds with the new cap.

Tests pin: helper formula (``_http_cap_from_manifest``),
coordinator → ``create_sandbox`` kwarg propagation, the H2 init_cmd
regression case (cap is staged before init_cmd runs), the wire
round-trip on `CreateSandboxCommand.stub_request_timeout_s`,
``_stub_for`` honouring the staged override, fall-back to the 1 h
default when no override is staged, and the rebuild-stub-on-cap-
change defensive path.

- **Original goal**: the ``StubClient`` aiohttp request timeout
  matches the underlying step's actual budget (the adapter's
  ``step_timeout_s``, itself derived from per-task
  ``[agent].timeout_sec``) instead of being a hardcoded 1-hour floor.
- **Phase-0 baseline**: 1-hour ``request_timeout_s`` default in
  ``xrlenv/node/stub_client.py``. Closes the obvious TimeoutError-on-
  slow-task bug — the cap is invisible during normal operation and
  only matters as a safety net for an absent-stub failure mode.
- **Stage 1 outcome**: the 1 h cap drops to roughly
  ``max(init,setup,step,teardown) + 60 s`` per sandbox.
- **Stage 2 outcome**: each per-phase call (env_setup / env_step /
  env_teardown) now carries its OWN cap derived from the matching
  manifest phase budget + 60 s. For a tb2 task with
  ``step_timeout_s = 30 s``, env_step's HTTP cap is ~90 s instead
  of the wider per-sandbox stage-1 cap. The per-sandbox cap remains
  as the floor for non-phase-specific calls (init_cmd,
  run_in_sandbox, healthz polling).
- See ``docs/developer/timeouts_internals.md`` for the
  developer-facing description of the two-layer (per-sandbox stage 1
  + per-call stage 2) model.

### D15. GC layer 3 (control-plane reconcile via bidi) — CLOSED

Closed in P1.1 (commits 82c914e initial + 38b0bb3 audit response).
Spec-21 gained `ListSandboxesCommand` (variant 29) +
`ListSandboxesReply` (payload 17) so the control plane can ask each
connected node for the IDs in its in-memory sandbox table; a new
`xrlenv/control/gc_reconciler.py` runs periodically (default 60 s)
against the registry, diffs the returned set against
`state.list_sandboxes()` bucketed by status (`running` vs
`destroy_pending`), and acts on each side's orphans:

  - **Node-only genuine orphan** (node has, no state row): emits
    `gc.reconcile.orphan_sandbox` and issues `destroy_sandbox` on
    the node (likely CP-crash leftover).
  - **Destroy-pending, node still has it** (audit H1 fix): emits
    `gc.reconcile.destroy_pending_retry`, retries `destroy_sandbox`,
    drops the state row on success. Fixes the leak where
    `_terminate`'s timed-out destroy left a row at
    `status='destroy_pending'` and the scheduler kept counting it
    against capacity forever.
  - **Destroy-pending, node has cleaned up** (audit H1 fix): emits
    `gc.reconcile.destroy_pending_cleared`, drops the state row.
    Covers the case where the node's destroy actually succeeded
    after the CP timed out.
  - **State-only running** (state has, node lost): emits
    `gc.reconcile.lost_sandbox` and seals the owning rollout
    `failed/sandbox_lost` via the new
    `coordinator.handle_sandbox_lost`.

Per-node failures (RPC raised, RPC hung past `per_node_timeout_s`)
are isolated — the failing node is skipped for the sweep, others
proceed. Tests pin all four diff buckets, the destroy-failure
isolation, the per-node timeout, the in-sync no-op, the
sandbox-lost handler's idempotency, and the H1 regression cases
(retry-with-still-present, retry-failure-keeps-row,
cleared-when-node-already-gone, destroy-pending-not-classified-as-
genuine-orphan).

Wired into `build_distributed_runtime` with
`gc_reconcile_interval_s=60.0` default; tests / operators can pass
`None` to disable. `LocalRuntime` is intentionally NOT wired — the
in-process node and CP share the same Python sandbox table so drift
isn't possible.

Caveat: the node-side `list_sandbox_ids()` reports the agent's
in-memory `_sandboxes` table only. Out-of-band containers created
by an operator's manual `docker run` outside the agent's lifecycle
are visible to the node-startup GC layer 2 sweep
(`backend.list_owned_sandboxes()`) but not to layer 3's reconciler.
This is a deliberate bound — layer 2 already handles those at
process start.

### D18. Image-affinity scheduling (per-node image awareness)

- **Symptom that surfaced this:** during the multi-VM tb2 acceptance
  smoke (2026-04-30), only the AWS node had a complete set of
  `terminal-bench-2/<task>:0.1` images. The scheduler placed
  `dna-insert` on `gcp-osworld-exp-1` (the GCP node, which lacked
  the image), the Docker backend fell through to a registry pull,
  and the rollout failed with `sandbox_create_failed` →
  `pull access denied for terminal-bench-2/dna-insert ...
  repository does not exist or may require 'docker login'`
  (locally-built tags have no upstream). The smoke driver surfaced
  it as `RolloutFailed: rollout ... failed during startup`.
- The platform's image-cache manager (spec 15) already tracks
  per-node tags as part of the LRU bookkeeping. The scheduler
  doesn't currently consume that signal — placement is purely
  capacity-based. Adding image-affinity (prefer nodes that already
  have the image; spill to others only on capacity pressure) would
  have masked this misconfiguration *and* is the canonical pattern
  for benchmark plug-ins where every node carries a large-but-finite
  image set.
- Phase-1 work (touches `xrlenv/control/scheduler.py` +
  `xrlenv/node/image_cache.py`'s reporting surface). Acceptance: a
  scheduler unit test that pins "node A has image X, node B doesn't
  → rollout for X lands on A even when B has more free slots".
- Cross-cuts D13 (scheduler placement scoring uses count, not
  effective-resource load). When that lands, image-affinity becomes
  one more weighted signal in the scoring function rather than a
  hard pre-filter.

### D19. Pre-flight image check before placement

- Companion to D18. Even with image-affinity scheduling, a node can
  fall behind on image rebuilds (operator forgot to re-run
  `build-task-images.sh` after a Dockerfile bump). Today the
  coordinator finds out by running `docker run` and catching
  `ImageNotFound` post-hoc — the rollout is recorded as
  `failed/sandbox_create_failed` and the operator has to read the
  state-store event payload to learn the cause.
- Cleaner: the coordinator asks the candidate node "do you have
  this image tag?" (cheap bidi RPC against the cached image
  manifest) before sending the create. On miss, either fail-fast
  with a clear `image_missing` reason or trigger a warm-up via
  `xrlenv warmup` if image affinity wasn't used. The image-cache
  manager already has the data; just needs a reverse query in the
  bidi protocol.
- Phase-1 slice. Acceptance: an integration test that puts a
  rollout's image only on one of two nodes, schedules to the
  bare node, and asserts the rollout fails with
  `reason="image_missing"` (not `sandbox_create_failed` from a
  registry pull error) before any sandbox creation begins.

### D20. Image distribution + digest-pinning, end-to-end

Umbrella todo for the image lifecycle. D18 + D19 + the buildx-RepoDigests
fix in commit ``5a38e78`` are local mechanism pieces; this entry
captures the architecture they need to fit into so the next slice
that touches scheduler placement, image cache, or template-catalog
digest pinning can land them coherently.

**Surfaced by:** the 2026-04-30 multi-VM tb2 acceptance smoke. The
laptop control plane had locally-built images (because the operator
ran ``build-task-images.sh`` during dev); buildx populated their
``RepoDigests`` with local-only digests; the catalog's opportunistic
digest_resolver pinned the resolver-supplied tag to that local
digest; the GCP node received the pinned form, found nothing
matching its own (different) local digest, fell through to
``docker pull`` against a registry that doesn't host the tag, and
the rollout died with ``sandbox_create_failed``. The symptom fix
(skip RepoDigests where digest == Id) is correct but the underlying
question the user surfaced is bigger: **in distributed mode the
control plane shouldn't be in the digest-pinning loop at all for
per-node-built images**.

**Topologies the platform must support cleanly**:

1. **Fully local debug.** Control plane + node-agent + sandboxes
   all on one machine (laptop). One Docker daemon serving everyone.
   Today: works; the opportunistic resolver happens to be querying
   the same daemon that will run the sandbox. Should remain
   zero-friction.
2. **Clean-room control plane.** Control plane on a VM with no
   Docker installed (the phase-1-canonical shape). Today:
   ``_try_local_docker_digest_resolver`` returns None on
   ``_client.ping()`` failure, catalog falls back to the unpinned
   warning path, manifests register tag-only. Works correctly —
   the destination node is the source of truth for image identity.
3. **Distributed with image diversity.** Many nodes, where any
   given image may live on a subset of them. Sub-cases below.

**Image distribution strategies** (orthogonal to the topology):

| Strategy | Where the image lives | Cross-host digest? |
|---|---|---|
| Registry pull | Every node pulls from a shared registry on first use. | Yes — manifest hash is canonical. |
| Per-node build | Each node runs ``build-task-images.sh`` (or equivalent). | No — each host has its own ``Id``. |
| Build-once, ship-many | One node builds, ``docker save`` + transfer + ``docker load`` on the rest. | Sort of — same ``Id`` (content-addressed), but no ``RepoDigests`` from a registry. |
| Hybrid | Some images registry-pulled (a public base), others built per-node (the plug-in's per-task images). | Per-image. |

The user's deployment matters: **operators without registry access
can't use Strategy 1** (the simplest case). Operators with
hundreds-of-instances benchmarks (SWE-bench-Lite-style) **can't
afford Strategy 2** as a uniform policy (every node × every image
is impractical disk and time). They need affinity-aware placement +
some way to distribute images to a *subset* of nodes.

**Architecture the platform should converge on**:

1. **Digest-pinning is per-image, not platform-wide.** Each
   ``ResolvedInstance`` (Pattern A) and each manifest (Pattern B)
   declares an ``image_pin_mode``:
   - ``"registry_digest"`` — image is registry-published; the
     digest is the same on every node; pin at register-time using
     the registry resolver.
   - ``"per_node_local"`` — image is locally built or shipped per
     host; digest is per-node; **don't pin centrally**, defer all
     identity to the node.
   The catalog respects the mode; for ``per_node_local`` it skips
   ``_maybe_pin_image`` regardless of whether the control plane
   has a daemon. Removes the buildx trap by construction.
2. **The control plane has no authoritative view of image
   identity for ``per_node_local`` images.** All digest checks at
   placement / sandbox-create time are delegated to the destination
   node. Spec 19 audit events for those carry
   ``digest_source=per_node`` so the audit trail is honest about
   what was verified where.
3. **Per-node image registry feeds the scheduler.** The image-cache
   manager (spec 15) already tracks tags per node. Promote that to
   a control-plane-visible signal so the scheduler can do
   image-affinity placement (D18) and the coordinator can do
   pre-flight existence checks (D19) without flying blind.
4. **Operator-driven image distribution recipes**, each documented
   under ``docs/deployment/``:
   - **Registry**: operator points the resolver at a registry URL;
     every node pulls; ``image_pin_mode="registry_digest"``.
   - **Per-node build**: operator runs ``build-task-images.sh`` on
     each node (or a scheduled subset); affinity scheduling routes
     rollouts to the nodes that built;
     ``image_pin_mode="per_node_local"``.
   - **Build-once-ship-many**: a new ``deploy/ship-images.sh``
     script that ``docker save``s on a builder node + scps + ``docker
     load``s on each receiver. Same ``Id`` everywhere, but
     ``image_pin_mode="per_node_local"`` because no registry digest
     exists.

**Concrete implementation knobs (phase-1 work)**:

- ``ResolvedInstance.image_pin_mode: Literal["registry_digest",
  "per_node_local"]`` (default ``registry_digest`` for backward
  compat).
- ``TemplateManifest.image_pin_mode`` (same enum, for Pattern B).
- ``catalog._maybe_pin_image`` short-circuits when mode is
  ``per_node_local``.
- New bidi RPC: ``QueryImage(tag) -> { present: bool, digest: str
  | None, last_used_at: float | None }``. Feeds D18 (affinity), D19
  (pre-flight), and per-node digest verification.
- Scheduler grows an ``image_aware_placement`` flag (default off
  for backward compat); when on, ``Placement.score()`` adds a
  positive term for nodes that already have the image.
- ``deploy/ship-images.sh`` for the build-once-ship-many recipe.
- Operator docs split per topology: which strategy, which knobs,
  which failure modes, how to debug each.

**Acceptance for the umbrella slice**:

- Unit test: catalog with ``digest_resolver`` wired but a
  ``per_node_local`` overlay → image stays tag-form (no
  ``@sha256:`` rewrite), no spec-19 ``template.image_unpinned``
  audit event (or a different one with
  ``mode=per_node_local``).
- Integration test: 2-node distributed harness, image only on node
  A, ``image_aware_placement=True`` → 5/5 rollouts land on A even
  when B has more free slots.
- Integration test: same setup, image_aware_placement=False
  (default), ``image_pin_mode=per_node_local`` → rollouts spill to
  B but pre-flight check (D19) trips with ``image_missing`` and
  exposes a clear remediation.
- End-to-end smoke recipe per topology, in
  ``docs/deployment/``.

**References**:

- D13 (scheduler placement scoring) — image-affinity becomes one
  weight in the scoring function rather than a hard filter.
- D18 (image-affinity scheduling), D19 (pre-flight image check) —
  the mechanism pieces this umbrella names.
- ``5a38e78`` — buildx local-only RepoDigests fix (the immediate
  symptom this umbrella structurally resolves).
- spec 15 (image cache management), spec 19 (image and asset supply
  chain).

**Update (2026-08-21) — scratch-registry build-on-demand.** The
"build-once, ship-many" strategy above (``deploy/ship-images.sh``,
``docker save`` + scp) is **superseded as the recommended build-once
path** by a new **scratch registry** (``:5012``): a quota-bounded, GC'd
third registry that holds build-on-demand images and distributes them by
registry pull (layer dedup + ranged pulls + affinity + calibrate all
apply), instead of tarball scp. It also fixes the unbounded growth of
``XRLENV_PRIVATE_REGISTRY_STORAGE`` (build-on-demand no longer lands in
``:5011``), and lets users self-serve their own Dockerfiles without an
operator build step. ``ship-images.sh`` is retained only as the
no-registry / air-gapped fallback. Full design + open sub-decisions
(new ``scratch_build`` pin mode, singleflight lease, ``durable_to``
bring-your-own-registry): **`notes/scratch-registry-build-on-demand.md`**.

### D21. SDK ``Client.list_nodes()`` / cluster-status RPC — CLOSED

Closed in `d4bc66e` (P1.1 / A2). The phase-1 plan tracks any further
follow-on work; this is the original deferred-item entry kept for
historical context.

**Symptom (historical):** the connect-mode tb2 smoke driver
(``xrlenv_plugins/benchmarks/terminal_bench_2/examples/tb2_acceptance_smoke.py --connect-host``) had no way to
ask the control plane "are nodes attached yet?" before dispatching
rollouts. Restarting ``xrlenv up`` then immediately firing the
smoke produced a wave of ``BackendCapabilityMissing: no node
supports backend 'docker'`` because the gRPC streams from the cloud
nodes hadn't reconnected. The current workaround in the driver
(commit XXX) is a probe-and-retry: start one rollout, retry on
``BackendCapabilityMissing`` for ``--restart-grace`` seconds. Works
but is asymmetric with the embedded mode (which has direct access
to ``runtime.scheduler.nodes`` and waits explicitly).

**Real fix:** add a ``ListNodes`` (or ``ClusterStatus``) RPC to
``xrlenv/api/proto/rollout_control.proto`` so the SDK has a clean
"how many nodes are attached, with which backends?" surface. Then:

  - ``Client.list_nodes() -> list[NodeInfo]`` (NodeInfo carries
    node_id, status, supported_backends, attached_at,
    last_seen_at).
  - ``Client.wait_for_nodes(min_nodes=N, timeout_s=...)`` helper
    that polls the new RPC.
  - Smoke driver and any other consumer-side caller drops the
    probe-and-retry hack and calls the helper.

The control plane already exposes the same data internally
(``state.list_nodes()``); it's just not on the consumer-facing
proto. Phase-1 work — touches the proto + servicer + client +
SDK. Acceptance: a unit test that asserts a freshly-started
control plane with no nodes raises ``Timeout`` after the grace
period; an integration test that asserts ``wait_for_nodes(2)``
succeeds once the second node attaches.

References: spec 05 (Trainer/Consumer SDK), spec 19 (auth scopes —
the new RPC needs a read scope).

### D22. B11 sandbox import-path extension — CLOSED

**Symptom (historical):** Audit M1 (2026-05-02).
``XRLENV_TEMPLATE_DIRS`` registered manifests at directories the
operator pointed it at, but the Docker backend's bind-mount +
``PYTHONPATH`` only carried the *one* ``xrlenv_plugins`` sibling
next to the imported ``xrlenv`` package. An external manifest at
``/some/other/path/xrlenv_plugins/benchmarks/foo/manifest.yaml``
registered fine; the first ``env_setup`` then failed to import
``foo.adapter`` inside the sandbox. The Python entry-point
discovery had the same gap: a pip-installed plug-in's adapter only
imported inside the sandbox if the wheel was installed on every
node host.

**Closed (2026-05, runtime fix `44fd81b`; B11.6 worked examples `ac8ff84`):**

  - :class:`xrlenv.control.template_discovery.DiscoveredManifest`
    pairs each external manifest path with its *plug-in root* —
    the directory whose immediate child is named ``xrlenv_plugins``
    (i.e. what to put on PYTHONPATH).
  - :func:`find_external_template_dir_manifests` (B11.1) and
    :func:`find_entry_point_manifest_files` (B11.2) both produce
    :class:`DiscoveredManifest` and resolve plug-in roots via
    ancestor walk, with a system-path guard dropping ``/etc``,
    ``/proc``, ``/sys``, ``/dev*``, ``/var/run/docker.sock``, and
    ``/`` (exact match) before they reach the docker backend.
  - :class:`xrlenv.backends.docker.DockerBackendConfig` grew an
    ``extra_plugin_roots: tuple[Path, ...]`` field. Each entry
    bind-mounts read-only at ``/opt/xrlenv-extras/<idx>`` (positional
    index, never basename-derived) and prepends to ``PYTHONPATH``.
    PEP-420 namespace-package semantics merge multiple roots'
    ``xrlenv_plugins/`` contributions, so the adapter imports
    natively from any registered plug-in.
  - The node bootstrap (:mod:`xrlenv.node.cli`),
    :func:`xrlenv.control.runtime.build_local_runtime`, and
    :func:`xrlenv.control.distributed_runtime.build_distributed_runtime`
    all derive ``extra_plugin_roots`` from the same env-var +
    entry-point inputs the discovery layer consumes — single source
    of truth, no separate operator switch.

**Acceptance — runtime side:** ``tests/unit/test_template_discovery.py``
covers the discovery shape change + system-path guard;
``tests/unit/test_docker_extra_plugin_roots.py`` pins the volumes
dict + PYTHONPATH shape including the backwards-compat regression
(empty ``extra_plugin_roots`` produces the byte-identical pre-D22
config).

**Acceptance — end-to-end:** the B11.6 worked example packages at
``examples/pip_new_datasets_or_benchmark/`` (echo_bench and
byo_dataset_harbor) install via the entry-point mechanism; their
smoke drivers seal ``finished`` with the expected reward. Without
D22 those rollouts seal ``setup_failed: ModuleNotFoundError`` —
each run of either smoke is a lived end-to-end validation of the
runtime fix.

References: audit M1 (2026-05-02), B11.1, B11.6.

### B6.1 (deferred to phase 2) — Redis StateStore design pre-bake

**Status:** deferred to phase 2 on 2026-05-02 (Q4 in
`notes/phase-1-to-do.md`). Phase-1 target is 100-200 concurrent
containers, which SQLite WAL handles comfortably (~1-3k writes/sec
ceiling). Redis StateStore + the 500-1k+ concurrent target lift
together when phase-2 scale-out work justifies the operational
overhead of running a second service.

**Design answers locked in 2026-05-02 (so phase-2 doesn't
relitigate):**

1. **Selection mechanism** — both `XRLENV_STATE_STORE_URL` env
   var (default) and `--state-store-url` CLI flag on `xrlenv up`
   (overrides). Mirrors the `XRLENV_TEMPLATE_DIRS` pattern.
   `LocalRuntime` always uses SQLite (Redis is silly for
   in-process); `DistributedRuntime` defaults to SQLite, switches
   to Redis when the operator opts in.
2. **Schema shape** — mirror SQLite tables literally: each row →
   one Redis hash, plus separate sorted-sets / sets for the
   indices SQLite carries today (`rollouts_status_idx`,
   `rollouts_task_key_idx`, etc.). Code-shape parity with SQLite;
   easy to reason about. Redis-idiomatic redesign (streams, etc.)
   is a phase-2 optimisation if (i) doesn't hit the latency
   targets — not a phase-1 prereq.
3. **Atomicity** — mix of Lua scripts (`EVAL`) for the
   multi-key transitions that must move together (rollout-row +
   sandbox-row updates, ~5-6 hot operations) and Redis built-in
   single-command atomicity (`SETNX`, `HSETNX`, etc.) where it
   suffices.
4. **Test strategy** — both `fakeredis` (in-process Redis double,
   ~95% coverage, fast) for unit tests AND one or two integration
   tests against a real Redis daemon (operator-run, not part of
   every PR). Same shape as the phase-0 Docker integration tests.
5. **Migration tool** — defer. `xrlenv state migrate sqlite://
   path redis://host` is post-phase-2-acceptance polish; the
   load-bearing case is fresh deployments choosing Redis at
   bring-up, not in-place migration.

**Why SQLite stays in phase 2 too:** even after Redis lands as
the production-deployment default, SQLite remains as the
`LocalRuntime` / dev-workflow / small-deployment / test-suite /
operator-recovery store. Reasons (ranked):

1. `LocalRuntime` / `Client.in_process()` shouldn't require a
   Redis daemon for plug-in authors / `tests/smoke/single_rollout.py`
   / unit tests / CI.
2. Test surface — hundreds of tests use `SqliteStateStore` for
   the persistence + restart-recovery code paths
   `InMemoryStateStore` doesn't cover.
3. Small deployments (10-50 concurrent, internal evals, research
   teams) don't need the operational overhead of Redis.
4. Operator recovery — `cp state.db backup.db && sqlite3 state.db`
   is a real procedure used during phase-0 multi-VM smoke
   recovery (referenced in `coordinator.py`'s "SQL surgery"
   comments).
5. Air-gapped / no-network setups where running an extra service
   is friction.
6. `state.db` is referenced in operator runbooks + spec 20.

**Slot:** phase 2, alongside CubeSandbox / microVM backend, mixed-
backend capacity accounting (D14), k8s/MIG/ASG autoscale, and
durable trajectory storage. The whole "lift from 100-200 to
500-1k+ concurrent" requires coordinated work across these,
not just a state-store swap.

**Acceptance:** phase-2 gate equivalent of phase-1's Gate 2a, but
at 500-1k concurrent on Redis-backed control plane with the same
latency targets. Plus a soak test verifying `WATCH/MULTI/EXEC`
or Lua-script atomicity holds under contention.

References: spec 03 (control plane), spec 20 (state + storage),
phase-1-to-do.md Q4 (revised 2026-05-02), phase-1-acceptance.md
"Why 100-200, not 500-1k" rationale block.


## Audit-response items (2026-05)

### D-AR-2026-05-05-H1. `xrlenv build apply` cluster-RPC — CLOSED

Closed by the P1.6.f-cluster-rpc commit (2026-05-05):

- Admin server gains `POST /api/build/apply` (kicks off background
  asyncio task; returns 202 + plan_id) and `GET /api/build/plans/<id>`
  (per-status rollup + per-assignment rows).
- Operator-role bearer-token auth wired via the existing TokenStore.
  Endpoints accept anonymous loopback requests when no token store
  is configured.
- DistributedRuntime constructs the BuildCoordinator with
  GrpcNodeBuilder + _DistributedBudgetProvider, hands it to the
  admin server alongside the token store.
- CLI gains `--connect-host HOST [--connect-port PORT]` +
  `--operator-token`. When set, the CLI POSTs the plan, polls the
  status endpoint every 3s, prints per-image results. Connection
  failures + auth errors surface with clear pointers at xrlenv up
  + token resolution.
- 7 new admin endpoint tests + 2 new CLI tests (dry-run roundtrip
  + unreachable-admin error path). Verified end-to-end via the CLI
  smoke at `xrlenv build apply --connect-host 127.0.0.1 --connect-port 1`.

Original-OPEN body retained below for traceability:

---

The 2026-05-05 audit (notes/audit.md) flagged that `xrlenv build apply`
unconditionally runs an in-process `LocalRuntime`; there is no
operator-facing path from the CLI to a running `DistributedRuntime`'s
`build_coordinator`. The proto + node-side handler + `GrpcNodeBuilder`
exist (P1.6.c) but only code that already holds a `DistributedRuntime`
object can reach them.

**Partial fix shipped (audit-response commit, 2026-05-05):**

- `build_local_runtime(skip_stale_node_sweep=True)` — the CLI passes
  this so a one-shot apply against a state.db shared with a live
  control plane no longer marks connected nodes as `lost`.
- `cmd_build_apply` refuses (rc=2) when `state.db` shows nodes with
  heartbeats <30s old, with a clear message + pointer to this
  deferred item.
- `docs/deployment/build_plans.md` + the per-benchmark operator
  pages were updated to be honest: the CLI is local-only today.

**Open work:**

1. Add an admin REST endpoint `POST /api/build/apply` (or a dedicated
   gRPC method on rollout-control) that the CLI hits when targeting
   a running control plane. The endpoint accepts the plan YAML +
   force/dry_run/applied_by, dispatches via the live
   `runtime.build_coordinator`, and streams per-image results back
   so the CLI can print them as they arrive.
2. CLI gains `--connect-host` / `--connect-port` (or auto-detect from
   `XRLENV_CONSUMER_TOKEN` + a default admin URL) to pick the
   cluster vs local path.
3. Authentication: operator-role token, same shape spec 19 already
   uses for `xrlenv up` and admin auth.
4. Lift the safety guard once the cluster path exists — multi-VM
   plans against a live cluster become the default.

**Slot:** P1.6.e or a follow-on P1.6.f. The audit-response partial
fix unblocks the existing local-only flow (which is what the bash
shim drives anyway), so the cluster-RPC piece is not gating the
P1.6 acceptance.

### D-AR-2026-05-05-M1/M3. Coordinator force / parallel — CLOSED

Closed in the same audit-response commit:

- M1: `BuildCoordinator.apply(force=True)` now bypasses the
  `no_op_already_completed` short-circuit on completed plans. New
  test `test_coordinator_force_rebuilds_completed_plan`.
- M3: per-node dispatch is now parallel via `asyncio.gather` over
  per-node tasks; a shared `asyncio.Lock` serializes the state-
  store writes. New test
  `test_coordinator_dispatches_per_node_in_parallel` proves two
  50-ms node jobs finish in <90 ms wall-clock.

### D-AR-2026-05-05-M2a. Partial-failure retry after replanning — CLOSED

The audit-response commit closes the stable-placement M2 case:
re-applying a `partial_failure` plan dispatches only failed rows
when placement is unchanged, and preserves existing `done`
timestamps.

The auditor's follow-up rebuttal review caught a real edge case:
retry recomputes placement, so if the failed image gets replanned
onto a different node (because the live-budget probe returned
different free-disk numbers), the `residual_only` branch included
the new `(node_id, image_ref)` in the dispatch set but didn't
insert a corresponding `build_plan_assignments` row first. The
per-node task's first `update_assignment_status(... building)`
call raised `KeyError` and left the plan stuck at `in_flight`.

**Fix (audit-rebuttal commit):** the residual-only branch now
materializes a `pending` row for any `(node_id, image_ref)` not
already in the snapshot before dispatch begins. Old failed rows on
the now-vacated node are preserved as placement history; a phase-2
follow-on may add a `superseded` status to clean up the rollup
arithmetic when an image moves between applies.

**Regression:** `test_coordinator_residual_retry_handles_replanned_node`
exercises a two-node setup where the failed node is evacuated
between applies, forcing FFD to replan the failed image onto the
other node. Verified failing without the fix (KeyError) and
passing with it.


### D-AR-2026-05-05-H2. terminal-bench-2 cluster builds need pre-populated harbor cache — OPEN

When ``xrlenv build apply --connect-host`` dispatches tb2 builds
to a node, the in-process Python builder fails-fast with "task not
found in harbor cache" if ``$XRLENV_BENCHMARK_CACHE`` (default
``~/.cache/harbor/tasks``) is empty on that node. Today's bash
shim auto-runs ``populate-harbor-cache.sh`` when it sees an empty
cache; the Python ``Tb2ImageBuilder`` does not.

Operator workflow today: SSH into each VM and run
``populate-harbor-cache.sh`` manually, OR rely on the bootstrap
scripts (``deploy/bootstrap-{aws,gcp}.sh``) to populate the cache
during VM provisioning. swebench-verified is unaffected — its
upstream IS Docker Hub, so ``docker pull`` works from any VM with
internet, no per-VM cache state needed.

Phase-2 fix paths (none shipped yet):

1. Have ``Tb2ImageBuilder`` auto-shell-out to
   ``populate-harbor-cache.sh`` when the cache is empty (lift the
   bash shim's behavior into the Python path; touches the cluster
   path automatically).
2. Add a ``BuildPlanPrepare`` RPC that dispatches a per-node
   pre-build step (run script X, fetch dataset Y) before the build
   phase. More general — handles future plug-ins that need similar
   per-node setup.
3. Move the harbor cache to shared storage (NFS / S3-FUSE / object
   mount) so it lives once cluster-wide. Operator policy decision;
   not a primitive change.

**Slot:** P1.6.g or alongside the asset-fetcher producer (whichever
introduces a clean per-node prepare hook).


### D-AR-2026-05-05-H3. SWE-bench `cache_level` optimization for `--all` workflow — CLOSED

Closed by P1.6.g (`78d6242`, `84a85e0`, `ec8d3b4`, `6f05eb7`, `be58db8`).

The original concern was that operators running ``--all`` on a
small fleet (e.g. 5×200 GiB VMs) hit ``InsufficientCapacity``
against the conservative ``IMAGE_SIZE_HINT_BYTES`` even though
Docker layer dedup would let the cluster accommodate the workload.

P1.6.g addresses this without needing a SWE-bench-specific
``cache_level`` knob — by making *opportunistic* the apply default
across every plug-in:

- ``BuildCoordinator.apply()`` defaults to ``eager=False``.
  The bin-packer fits what it can into the budget; everything that
  doesn't fit is recorded as ``status=registered`` (deferred), with
  its preferred-home node persisted alongside.
- The deferred ref's builder mapping is pushed to the preferred-home
  node via ``BuildImagesCommand.lazy_registrations`` (audit response
  `6f05eb7`), so the node knows how to materialize that image even
  though no synchronous build was dispatched.
- The first rollout against a deferred ref triggers
  ``ImageCacheManager.ensure_present()``, which consults the lazy-
  builder hook and invokes the benchmark's ``BenchmarkImageBuilder``.
- The cache eviction loop frees space on the same node before the
  next deferred ref is materialized, so density is bounded by
  *concurrent* working-set rather than total catalog size.

Net result: ``--all`` no longer rejects placements the cluster could
serve under realistic concurrent load. The conservative size hint
stays conservative-by-design — it just stops being a hard cap.

The three fix paths the original entry sketched still describe
*possible refinements*; only path 3 (lazy instance build) was
shipped, and it was shipped as a *general-purpose* mechanism, not a
SWE-bench-specific knob. Paths 1 (`cache_level` knob) and 2 (dedup-
aware size hints) remain available as future plug-in-side
optimizations if measured operator pain motivates them; neither is
required to close H3.

Operator-facing docs in `docs/deployment/build_plans.md` describe
the deferred / evicted lifecycle and what the new
``deferred=N`` / ``evicted=N`` rollups in
``xrlenv build status`` and the admin ``/builds`` page mean.

### D-AR-2026-07-07-B. Cross-node re-admit for saturated-node create overflow — OPEN (phase-1+)

- **Context.** Commit `3b06b58` + audit follow-up added node-**local** recovery
  for transient create-saturation faults (approach A: bounded retry-with-backoff;
  approach C: sysbox-specific create cap). See spec 04 §"Acquire retry semantics"
  and `notes/design-node-saturation-recovery.md`.
- **Deferred piece (approach B).** When a create-time node-health fault persists
  past the local retry budget, the coordinator could re-submit the acquire through
  the `AdmissionQueue`, which re-places it — possibly on a *different* node (now
  AIMD-throttled). Today the queue re-places only on a *proactive* `CapacityExhausted`
  from `scheduler.place()`; a *reactive* create-time 5xx never re-enters it, so a
  give-up fails the task on that node with no rebalance.
- **Why deferred.** Cross-node re-admit only pays off with **multiple dedicated
  sysbox nodes** — with one sysbox node there's nowhere to rebalance to, and local
  retry already covers the transient. It's also cross-layer (coordinator + admission
  + error classification) and can re-pick the same saturated node until AIMD catches
  up, so it still needs A's retry cap + backoff underneath. Slotted for when the
  sysbox pool grows past one node.
- **Reuses.** A's retry loop (`_create_with_retry`) and the existing
  `AdmissionQueue` / `HealthAimdController` machinery — B is a re-submit path on top,
  not a new mechanism.
