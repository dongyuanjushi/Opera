# Source-build dispatch plan

**Status**: PLANNED 2026-05-09. All forks locked; ready to execute
in three sub-slices.

**Why**: today's per-image-ref dispatch only handles
`context_source: type: registry`. Plans whose entries declare
`type: git` or `type: tarball` (the canonical case is
`xrlenv_plugins/benchmarks/seta/build_plan.yaml`) get
rejected at apply time with an operator-friendly `ManifestInvalid`.
This slice closes that gap by shipping the node-side
clone+build pipeline plus the eviction-side polish that makes
recovery from a build-then-evict cycle work.

## Locked decisions (2026-05-09)

| Fork | Lock | Reasoning |
|---|---|---|
| F1: tarball wire transport | gRPC bidi stream with raised limits | Symmetry with everything else on spec-21; operator-controllable cap via ``xrlenv up --build-tarball-max-bytes`` |
| F2: build-context cache | Persistent, 5 GB total cap, LRU evict; per-context >5 GB falls back to ephemeral | Predictable disk footprint; common case (small contexts) re-builds for free |
| F3: rebuild-cost auto-labeling for pulled images | Implicit convention (no label = ``registry-pull`` tier in eviction) | Avoids 1-layer rebuild bloat; eviction loop already needs a default |
| F4: pin-budget enforcement | Hard reject at apply time | Silent over-pinning bites weeks later when unrelated work hits the threshold |
| F5: calibrate scope | Operator-driven CLI (``xrlenv build calibrate``); auto-on-apply deferred | Explicit operator action; documented in the build-plan tech docs so operators know to invoke |
| F6: slice cut | Three sub-slices, in order | Each lands a coherent operator capability |

## Sub-slice 1 — Node-side git builder + dispatch

After this lands the seta-env smoke's happy-path apply works
end-to-end against a real cluster: every entry's git context gets
cloned, built, tagged, and the per-entry assignment reaches
``done``. Tarball support is **out of scope for sub-slice 1**;
shipped in a follow-on if there's operator demand.

**Coordinator side** (``xrlenv/control/build_coordinator.py``):

- Remove the source-type gate's git rejection. Tarball stays
  rejected with the same operator-friendly message until
  sub-slice 1.b ships.
- New optional ``build_image_fn`` constructor param:
  ``Callable[(node_id, image_ref, GitSource, timeout_s),
  Awaitable[(status, error)]]``. Mirrors the existing
  ``ensure_present_fn`` shape but for builds.
- ``_apply_per_image_ref`` branches per entry type:
  - ``RegistrySource`` → ``ensure_present_fn`` (existing path).
  - ``GitSource`` → ``build_image_fn`` (new path).
  - ``TarballSource`` → reject (sub-slice 1.b).

**Wire** (``xrlenv/api/proto/node_control.proto`` + bindings):

- New ``BuildImageCommand`` message in the spec-21 command stream.
  Fields:
  - ``image_ref: string`` — final tag.
  - ``source: oneof { GitSource, TarballSource }``.
  - ``timeout_s: float``.
  - ``labels: map<string, string>`` — operator-set labels passed
    through to ``docker build --label``.
- ``GitSource``: ``repo``, ``ref``, ``subdir``, ``dockerfile``.
- ``TarballSource``: defined now but not wired to dispatch in
  sub-slice 1; placeholder for 1.b.
- Response: ``BuildImageResult { status: enum, error: string }``.
- Server-side: raise ``grpc.max_send_message_length`` and
  ``max_receive_message_length`` to the configured tarball cap
  (default 100 MB; operator-tunable). This is the F1 commitment;
  takes effect now even though tarball dispatch ships in 1.b.

**Node side** (new ``xrlenv/node/source_builder.py``):

- ``GitSourceBuilder.build(image_ref, source, timeout_s, labels)``:
  1. Resolve the build-context cache path
     (``~/.xrlenv/build-context-cache/<sha256(repo)[:12]>/<ref>/``).
  2. If cached: ``git fetch + checkout`` to update.
     If not: ``git clone --depth=1 <repo> --branch <ref>``.
  3. ``docker build -f <subdir>/<dockerfile> -t <image_ref>
     --label xrlenv.image.rebuild-cost=local-build-expensive
     [+ operator labels] <subdir>``.
  4. Return ``("ok", None)`` on success;
     ``("failed", str(exc))`` on any failure.
- Build-context cache (F2): tracks total size; evicts
  least-recently-used contexts to stay under cap. Per-context
  >5 GB skips the cache (clones into a tempdir, deletes after).
- Concurrency: shares the existing ``ImageCacheConfig.pull_concurrency``
  knob (default 2 per node). Builds are CPU+IO heavy; same
  semantics as pulls.
- Auto-labeling: ``--label xrlenv.image.rebuild-cost=local-build-expensive``
  per F3 mapping.

**Runtime wiring**:

- ``xrlenv/control/runtime.py`` (LocalRuntime): supply
  ``build_image_fn`` that calls the in-process node's
  ``GitSourceBuilder``.
- ``xrlenv/control/distributed_runtime.py`` (DistributedRuntime):
  supply ``build_image_fn`` that resolves the node's transport
  via the registry and dispatches the spec-21
  ``BuildImageCommand``.

**Tests**:

- ``tests/unit/control/test_build_coordinator.py``: unit tests
  for the coordinator branching path (registry → ensure_present_fn,
  git → build_image_fn, tarball → reject). Existing tests stay
  green.
- ``tests/unit/node/test_source_builder.py``: new file. Mock
  ``docker build`` via subprocess.run / docker-py SDK fake.
  Tests: cache hit, cache miss → clone, oversize context →
  ephemeral, label propagation, build failure surfaces.
- ``tests/smoke/test_build_plan_dispatch_seta_env.py``: replace
  ``test_apply_rejects_git_source_with_operator_friendly_error``
  with ``test_apply_seta_env_starter`` that actually applies the
  starter plan and verifies all entries reach ``done``. The old
  rejection test moves to a smaller "tarball still rejects"
  variant since tarball isn't shipping in 1.

**Docs**:

- ``docs/technical_details/images/build_plan.md``: update the
  status admonition — git dispatch is live; tarball waits on
  1.b. Move calibrate / pin-budget / auto-labeling notes from
  "planned" to "shipped" once sub-slices 2-3 land.
- ``tests/smoke/groups/build_plan.md`` seta-env section: rewrite
  with the new happy-path semantics.

**Wall-clock estimate**: ~2-3 days of focused work. The
node-side git pipeline is the bulk; coordinator + wire changes
are mechanical.

## Sub-slice 2 — Build-on-acquire + eviction polish

After sub-slice 1, a built image that gets evicted disappears
permanently — the next ``acquire_container`` for that ref will
fail. Sub-slice 2 closes the loop.

**Build-on-acquire hook**:

- ``xrlenv/node/image_cache.py``: when ``ensure_present(image_ref)``
  finds the image absent, look up the registered ``BuildEntry``
  in a local cache (persisted via spec-21 dispatch). If the
  entry is git-source, invoke ``GitSourceBuilder``; if registry,
  the existing pull path. If no entry registered, fail as today.
- The legacy benchmark path already has a
  ``register_lazy_image_builders`` hook; extend it for
  per-image-ref entries.

**Build-time grace window**:

- ``xrlenv/node/image_cache.py``: track ``_built_at: dict[ref, monotonic]``
  separately from ``_last_used``. Cache state computation:
  an image inside its grace window (default 10 min) sorts as
  ``recently_used`` even without a touch. Window expires on
  first ``ensure_present`` hit, after which standard LRU
  semantics apply.
- Default grace window configurable via ``ImageCacheConfig``.

**Eviction tier defaults (F3)**:

- Eviction loop: an image with no ``xrlenv.image.rebuild-cost``
  label sorts in the ``registry-pull`` tier (cheapest to
  recover). Built images carry the explicit
  ``local-build-expensive`` label and sort as the most expensive
  tier.

**Tests**:

- ``tests/unit/node/test_image_cache.py``: build-on-acquire
  fires, grace-window protection, registry-pull default
  inference.
- ``tests/smoke/groups/build_plan.md`` seta-env section: add a
  test that triggers eviction (under simulated disk pressure)
  + re-acquires + confirms rebuild fires.

## Sub-slice 3 — Pin-budget enforcement + calibrate

Independent polish; either could ship before the other if
priorities shift.

**Pin-budget enforcement (F4)**:

- ``xrlenv/control/build_coordinator.py``: in
  ``_apply_per_image_ref``, after step 4 (budget snapshot) and
  before step 5 (placement), compute per-node pinned-bytes
  totals. For each node: sum ``size_hint_bytes`` of every
  entry whose ``pinned: true`` AND whose
  ``preferred_home_count`` could land it on this node (the
  bin-packer hasn't run yet, so be conservative: every pinned
  entry counts toward every node's projected pinned total).
  If any node's projected pinned bytes > available bytes,
  raise ``ManifestInvalid`` with a clear message naming the
  offending node + the over-budget delta.

**Calibrate flow (F5)**:

- ``xrlenv build calibrate --plan-id <id> --output <yaml>`` (new
  CLI subcommand).
- Reads the plan + all assignments from state.db, queries each
  node's ``docker image inspect`` via a new spec-21
  ``InspectImageCommand`` (or piggyback on the existing image
  cache reporting), aggregates: for each ``image_ref``, take
  the **max** ``Size`` across nodes (safest for FFD).
- Writes the calibrated YAML next to the input with
  ``size_hint_source: cluster-reported`` on every entry the
  cluster has materialized at least once.
- **Important UX note**: the operator must explicitly invoke
  this. Docs in the build-plan tech-details page (sub-slice 5)
  spell out when to run it (after first cluster build, then
  on demand) and how to promote the calibrated YAML into the
  canonical one (copy + commit).

**Tests**: unit tests for pin-budget violations, calibrate
end-to-end via a fake state.db + fake node-image listings.

## Out of scope (not this slice)

- **Tarball dispatch** — covered by F1's wire commitment but
  not delivered in sub-slice 1. Adds operator-side bytes
  loading + chunked transmission. Sub-slice 1.b if demand
  surfaces.
- **Auto-calibrate-on-apply** (F5 part b) — convenience
  workflow; defer until calibrate-via-CLI is established.
- **Dynamic eviction toggle** (``xrlenv images eviction
  disable/enable``) — independent feature; can land any time.
- **Cluster-reported size in BuildPlanRecord** — calibrate
  writes a side-artifact YAML, not state.db. State.db's
  ``plan_json`` keeps the original (registry-probe) sizes.
- **Build retry on transient failure** — git fetches /
  ``docker build`` can fail on flaky network. Out of scope
  for sub-slice 1; phase-2 polish.
