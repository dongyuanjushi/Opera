# P1.7.C.2 — Cluster-mediated image builds (generic primitive)

**Status**: PLANNED 2026-05-08. All forks locked; ready to execute
in two PRs (B.1 + B.2).

**Why**: original P1.7.C.1 deferred build-on-acquire on the
assumption that case-2/3 benchmarks would always pull from a
registry. Empirical 2026-05-08:

- terminal-bench-2 catalog (89/89 tasks): ✓ all Docker Hub-resolvable
  via `alexgshaw/<task>:20251031` (probed all 89 via Docker Hub
  v2 API; 200 OK).
- camel-ai/seta-env Harbor-Dataset: **no** ``docker_image`` field
  in any task.toml; ships ``Dockerfile`` + ``build_timeout_sec=600``.
  Cluster mode currently fails fast on ``ImageNotFound``.

So build-on-cluster IS load-bearing for seta-env and any future
benchmark that ships Dockerfiles instead of registry-pushed
images. The slice revives the P1.6 control-plane build flow but
in a benchmark-agnostic shape.

## Locked decisions (2026-05-08)

| Fork | Lock |
|---|---|
| 1. Asset transport | (b) consumer-shipped tarball + (a) operator-driven git-clone, both with size caps. (c) registry-only is now part of the same plan schema (entries with `context_source: type: registry` = pure prefetch). |
| 2. Mechanism | Generic primitive — proto/RPC/server unaware of harbor or tb2. Adapters opt in. |
| 3. Operator UX | `xrlenv build --plan <build_plan.yaml>`. |
| 4. Harbor on-demand | `xrlenv_build_on_acquire=True` constructor kwarg. |
| O1 | Ship per-benchmark generators for swebench-verified, tb2, seta-env. Registry-pullable benchmarks emit `registry`-source plans (= cluster-side prefetch with FFD bin-pack), not "no plan." |
| O1 — idempotency | Per-image: if N copies already resident on nodes that meet `preferred_home_count`, skip rebuild. Both server-side (`BuildImageCommand` checks `QueryImageCommand` first) and CLI-side (`xrlenv build --plan` reports `skipped` for already-satisfied entries). |
| O2 | Harbor on-demand: try local-tarball if `$XRLENV_BENCHMARK_CACHE/<task>/environment/` populated; else git-source clone. Cluster-side git-clone cache so repeat builds don't re-pull. |
| O3 | Rate limit default 100,000 builds / hour / token. Token needs `build` capability. |
| O4 | `Client.build_image()` is part of **B.2**, not B.1. B.1 ships only the operator CLI. |

## O5 — Build plan is "warm to sweet spot," not "build everything"

Added 2026-05-08 after operator-pressure call-out: a build plan
may cover MORE images than the cluster has disk for, and builds
must not starve ongoing rollouts. The plan is **a warmup target,
not a hard contract**. The cluster fills toward it up to a
configured comfort threshold; surplus entries are deferred (to
build-on-acquire OR a future re-run with a different subset).

Three rules that feed this:

### 1. Per-node "image cache budget" — bin-pack against this, not raw free disk

Every `xrlenv-node` advertises an **image cache budget** distinct
from raw free disk. Defaults:

- ``--image-cache-budget-fraction 0.6`` — fraction of total disk
  reserved for image cache (default 60%).
- ``--image-cache-headroom-bytes 30G`` — minimum bytes kept free
  for sandbox runtime + working files (default 30 GB).
- The effective budget per node = ``min(budget_fraction × total,
  total - headroom)``.

The FFD bin-packer in `xrlenv build --plan` respects this budget,
not raw `df`. A node with 500 GB free but a 60% budget gets
treated as having 300 GB of image-cache space.

### 2. Build concurrency yields to rollouts

The node-side `BuildImageCommand` handler:

- **Per-node max-concurrent-builds**: ``--max-concurrent-builds 2``
  on `xrlenv-node serve`. Cap on parallel `docker build` /
  `docker pull` operations per node.
- **Backpressure on rollout pressure**: pause accepting new build
  commands when the node's running-rollout count exceeds
  ``--build-pause-rollout-threshold 0.8`` of capacity. New build
  RPCs queue at the control-plane side; node consumes them when
  pressure drops.
- **Lower-priority subprocess**: `docker build` invoked with
  `nice -n 10` + `ionice -c2 -n7` so concurrent rollout exec /
  archive operations don't get starved on CPU / disk IO.

### 3. Plan exceeds budget → partial fill + clear report

`xrlenv build --plan` output for an over-sized plan:

```
Plan: 200 entries, 580 GB total uncompressed
Cluster: 4 nodes, 280 GB total image-cache budget
WARNING: plan exceeds cluster budget by 300 GB

Bin-pack:
  95/200 fit (priority order: registry-resident first, then
            git-source, then tarball; ties broken by entry order)
  105 deferred — left for a later pass. Operator options:
    - rerun `xrlenv build --plan` after eviction frees space
    - subset via `--max-builds` / `--priority-tag`
    - rely on build-on-acquire (a generic primitive any
      framework adapter or direct-API caller can opt into;
      not adapter-specific)

Building... [████████░░] 87/95 done · 6 in flight · 2 queued
Final:
  87 built (median 4m12s, p99 9m38s)
  6  skipped (already present on a preferred-home node)
  2  failed (see audit log: build.image_failed)
  105 deferred
```

Operator can override:

- ``--strict`` flag: refuse to dispatch any build if plan exceeds
  budget. Operator must explicitly subset before retry.
- ``--priority-tag <label>``: only build entries tagged with the
  given label first; ignore others. (Plan schema gains a
  ``priority`` field per entry.)
- ``--max-builds <N>``: cap absolute count regardless of fit.

### 4. Pin / warm-pool: opt-in per entry

Plan schema gains an optional ``pinned: true`` flag per entry:

```yaml
- image_ref: alexgshaw/fix-git:20251031
  context_source: { type: registry }
  pinned: true                  # never evict during a run
  placement:
    preferred_home_count: 2     # warm two copies for HA
```

Pinned images never evict; the LRU layer respects this flag. For
unpinned plan entries: the cluster fills toward the plan, but
hot rollouts pulling different images can evict earlier-built
plan entries; build-on-acquire backfills if those tasks come up
again. **Plans don't fight eviction — they augment it.**

## O6 — `size_hint_bytes` accuracy + ad-hoc calibration

User pushback 2026-05-08: "How do we get `size_hint_bytes`
accurate, and ad-hoc calibrate?" Wrong hints → bad bin-pack
(some nodes overfilled, others underused) or false
"plan exceeds budget" warnings.

Three sources of truth, in order of accuracy:

| Source | What it gives | When available |
|---|---|---|
| Registry manifest probe | Sum of unique layer sizes from registry API (Docker Hub v2, OCI). Accurate. | `type: registry` entries, at generation time. |
| Cluster-resident size | `ReportImagesCommand` returns per-node `ImageStateEntry.size_bytes` for each present image. Authoritative. | Any image already built/pulled on a node. |
| Heuristic | Base image size (registry probe of `FROM`) + conservative overhead (e.g. ×1.3) OR a generic default (5 GB). | First-time build of `git` / `tarball` entries before any cluster member has built them. |

### Generation phase — `xrlenv build plan-gen`

For each entry the generator decides hints by source type:

- **`type: registry`** → probe the registry manifest API at
  generation time, sum layer sizes, write
  `size_hint_bytes: <accurate>`.
- **`type: git`** / **`type: tarball`** → if the cluster
  is reachable AND already has the image (e.g. a previous
  build), use the cluster-reported size. Otherwise emit a
  `size_hint_bytes: <heuristic>` with a comment marking it
  estimated:

  ```yaml
  - image_ref: hb__seta-task-0
    context_source: { type: git, ... }
    placement:
      preferred_home_count: 1
      size_hint_bytes: 4500000000   # estimate (FROM-base 3.5GB ×1.3)
      size_hint_source: heuristic   # `registry-probe` | `cluster-reported` | `heuristic`
  ```

The `size_hint_source` field lets the bin-packer add safety
margin to heuristic-tagged entries (e.g. pad +20% during
packing) and the calibration command know which entries to
re-measure.

### Build phase — actual size captured

`BuildImageReply` includes `bytes_built` from
`docker image inspect <ref> --format '{{.Size}}'`. The CLI
captures these during the build run.

`xrlenv build --plan plan.yaml --update-hints`:
- After successful build of each entry, write the actual size
  back to the plan file in-place.
- Promotes `size_hint_source: heuristic` →
  `size_hint_source: cluster-reported`.
- Idempotent: re-running with `--update-hints` against a fully
  calibrated plan is a no-op (sizes already accurate).

### Ad-hoc calibration — `xrlenv build calibrate`

Separate subcommand for re-measuring without dispatching new
builds:

```bash
# Reads cluster's current image state, updates any plan entries
# whose images are already resident somewhere with the
# cluster-reported size:
xrlenv build calibrate plan.yaml

# Same but for a single entry (handy after hand-editing):
xrlenv build calibrate plan.yaml --image-ref hb__seta-task-7

# Dry-run — print what would change, don't write:
xrlenv build calibrate plan.yaml --dry-run
```

`xrlenv build calibrate` does NOT trigger builds. It only:
1. Asks the cluster `ReportImagesCommand` for present sizes.
2. For each plan entry whose image is present somewhere,
   updates `size_hint_bytes` + sets
   `size_hint_source: cluster-reported`.
3. For entries that aren't anywhere, leaves the hint alone +
   prints a warning.

### Generator integration

Per-benchmark generators (`xrlenv_plugins/images_build/<name>/build_plan_gen.py`)
get the registry-probe accuracy for free for `type: registry`
entries (the generator's responsibility is to probe). For
`type: git` / `type: tarball` entries they emit heuristic +
`size_hint_source: heuristic`; operators call
`xrlenv build --plan ... --update-hints` once to land actual
sizes, then commit the calibrated plan back to the repo.

This means **the canonical `build_plan.yaml` files we ship in
`xrlenv_plugins/images_build/<name>/` are calibrated** — we run
`--update-hints` ourselves after the first successful cluster
build and commit the result. Operators downloading the plan get
accurate hints out of the box.

## O7 — Build strategy ↔ eviction strategy: how they complement

User pushback 2026-05-08: "Does our build strategy work well with
the eviction strategy? They should complement to each other."

Yes, and the integration must be explicit. The existing
``xrlenv/node/image_cache.py`` already has the right primitives
(`ImageTier` = in_use|pinned|recently_used|cold; `EvictionTier`
= rebuild-cost dimension; pluggable `tier_classifier`;
`_last_used` timestamps). The build flow extends them rather
than replacing.

The shared lifetime model:

```
[plan-build]  →  [recently_used (grace window)]
                          │
                          ▼
[acquire]     →  [in_use]  →  [released]  →  [recently_used]  →  [cold]  →  [evicted]
                                                                                 │
                                                                                 ▼
                                                                       [build-on-acquire
                                                                        re-builds when
                                                                        next needed]
```

Five integration points B.1 must implement:

### 1. Build flow tags images → eviction reads tags

Each `BuildImageCommand` tags the resulting image with rebuild-
cost labels the existing `default_tier_classifier` already
consumes:

| Context source | Label | Eviction tier |
|---|---|---|
| `type: registry` | `xrlenv.image.rebuild-cost: registry-pull` | cheap (evicts first under pressure — fast to re-pull) |
| `type: tarball` | `xrlenv.image.rebuild-cost: local-build-cheap` if base image is cached, else `local-build-expensive` | medium (after registry-pull) |
| `type: git` | `xrlenv.image.rebuild-cost: local-build-expensive` (clone + build) | expensive (evicts last; rebuilding is costly) |

This means under disk pressure, the cluster evicts `registry-pull`
images first (fast to re-pull from Docker Hub) and keeps locally-
built images warm longer.

### 2. Plan-built-but-never-acquired: grace window

Build flow updates `_last_used` to "now" at build completion —
but the image hasn't been acquired by any rollout yet. Without
care, an operator who pre-warms 200 images and sees only 5 used
would have all 200 holding "recently_used" status indefinitely
under existing LRU.

Resolution: a separate `_built_at` timestamp distinct from
`_last_used` (= last-acquire timestamp). The tier classifier
treats plan-built images as `recently_used` for a configurable
**warmup grace window** (default 1 hour) post-build, then they
fall to `cold` if never acquired.

```python
# Conceptual (extends default_tier_classifier):
if image in self._last_used:
    return "recently_used" if (now - last_used) < window else "cold"
elif image in self._built_at:
    if (now - built_at) < warmup_grace:
        return "recently_used"   # plan warmup, give it a chance
    return "cold"                # never used after grace; freely evict
```

Effect: pre-warmup respects the operator's intent for an hour;
unused entries don't permanently squat on cache after.

### 3. Build dispatcher pauses on disk pressure

The `BuildImageCommand` handler defers accepting new builds when
the node is in active eviction (free disk between
`eviction_start_threshold` and `eviction_stop_threshold`). The
build coordinator queues commands; nodes drain them once disk
pressure clears.

This prevents the build-vs-eviction fight where builds are
writing layers while eviction is removing them, both fighting
for IO bandwidth.

### 4. `pinned: true` reuses existing pin mechanism

`ImageCacheManager` already has a `pin` set. Plan entries with
`pinned: true` call `manager.pin(image_ref)` after successful
build. The existing eviction layer already treats pinned images
as never-evictable.

Sanity check at plan-apply time: if total pinned bytes per node
exceeds image-cache budget, refuse to dispatch (operator must
unpin some entries or shrink the plan). Error includes the
node + the pinned bytes that exceed budget.

### 5. `preferred_home_count` is a soft preference, not a pin

`preferred_home_count: N` says "build on N nodes for affinity";
it does NOT pin those copies. Under pressure, an unpinned plan
entry's copies evict freely — this is correct behavior:
build-on-acquire backfills if the image is needed again.

Operators who want N copies to survive pressure use both:
```yaml
- image_ref: my-bench/critical:1
  context_source: { type: git, ... }
  pinned: true
  placement: { preferred_home_count: 2 }
```

### 6. Build-on-acquire feeds the same lifetime cycle

When build-on-acquire builds an image, it goes through normal
LRU after the acquiring rollout releases. No special handling
needed — the build flow uses the same `_built_at` timestamp.
Unused-after-grace = freely evictable.

### Sanity matrix

| Scenario | Behavior |
|---|---|
| Plan builds image X; no rollout ever acquires it; cluster runs other workloads | After grace (1h), X falls to `cold`; under pressure it evicts first within its rebuild-cost tier. |
| Plan builds image X; rollout uses it; rollout ends | Standard LRU: `in_use` → `recently_used` → `cold` after window → evictable. |
| Plan exceeds budget; 105 deferred | Cluster fills to budget; deferred entries arrive via build-on-acquire if needed. |
| Operator pins 250 GB on a 200 GB node | Plan-apply refuses with clear error; operator must reduce or move to a bigger node. |
| Disk eviction triggered while build dispatcher tries to build new image | Dispatcher defers the new build until eviction_stop_threshold; rollouts unaffected. |
| Image evicted, rollout requests it again | Build-on-acquire re-builds (if the framework adapter or direct-API caller opted in); falls back to fail-fast otherwise. |

### What this means for B.1's implementation footprint

- Existing `ImageCacheManager` extended:
  - New `_built_at` dict alongside `_last_used`.
  - `default_tier_classifier` reads `_built_at` + warmup-grace-window.
  - `report()` exposes both timestamps.
- Existing `default_tier_classifier` extended to read
  `xrlenv.image.rebuild-cost` label (already mostly does — it
  reads harbor / tb2 conventions; we add the explicit label
  contract).
- New `BuildImageCommand` handler tags images with the right
  label before reporting completion.
- Bin-packer in `build_coordinator` queries node tier state
  before dispatching (skip to next node if target is in active
  eviction).
- Plan-apply CLI does pin-budget sanity check upfront.

Adds ~150 LOC to B.1 estimate. Stays within the ~1 week scope.

## O8 — Dynamic eviction toggle + idle-cluster behavior

User pushback 2026-05-08:

1. Operator should be able to enable/disable eviction
   dynamically; if off, the cluster fails fast on disk full.
2. If all images are cold but cluster is idle (no active
   rollouts), don't evict them.

### Q2 first — already correct in the existing design

Verified by reading `xrlenv/node/image_cache.py`:
``_evict_if_needed()`` is called **only** inside
``ensure_present()``, after the image-present check fails. There
is **no background sweep, no timer, no periodic task**.

Behavioral consequences (already true today, confirmed not
broken by the build flow):

| Cluster state | Eviction behavior |
|---|---|
| Idle (no acquires, no pulls, no builds) | Never fires regardless of cold-count or disk pressure. Images stay. |
| Active + free disk above threshold | Never fires; images promoted/demoted in cache state but nothing is removed. |
| Active + new acquire/pull/build needs space | Just-in-time: evict the coldest image(s) by rebuild-cost tier until target headroom reached, then proceed with the new request. |
| Active + new request + everything is cold-expensive | Eviction still fires (cheapest expensive evicts first within tier), but if the surviving free-disk after eviction still can't fit the new image, the original `ensure_present` call raises `OutOfDiskAfterEviction`. New request fails fast. |

So **idle cluster never evicts**. Cold images sit on disk until
something asks for new space. This is the correct behavior and
the build flow doesn't change it — `BuildImageCommand` is just
another consumer of `ensure_present`'s shape, not a new
trigger.

What B.1 must guarantee: **the build coordinator never calls
`_evict_if_needed` directly**, only through `ensure_present`.
Documented as an invariant in the build_coordinator docstring.

### Q1 — dynamic eviction toggle

Genuinely new functionality. Three parts:

**(a) Runtime-mutable flag on `ImageCacheManager`:**

```python
class ImageCacheManager:
    def __init__(self, ..., eviction_enabled: bool = True):
        self._eviction_enabled = eviction_enabled

    def set_eviction_enabled(self, enabled: bool) -> None:
        old = self._eviction_enabled
        self._eviction_enabled = enabled
        LOGGER.info("image_cache: eviction %s → %s",
                    "on" if old else "off",
                    "on" if enabled else "off")

    async def _evict_if_needed(self) -> None:
        if not self._eviction_enabled:
            return                                # no-op, fail fast on next pull
        # ... existing logic ...
```

When disabled, `_evict_if_needed()` returns immediately. The
subsequent pull/build inside `ensure_present` then either fits
(no problem) or fails with `OutOfDiskAfterEviction` (clear
fail-fast).

**(b) New spec-21 RPC `SetCachePolicyCommand`:**

```protobuf
message SetCachePolicyCommand {
    CommandHeader header = 1;
    optional bool eviction_enabled = 2;
    // future: budget knobs, threshold knobs, all runtime-tunable
}
message SetCachePolicyReply {
    bool eviction_enabled = 1;
    // current effective config snapshot
}
```

Generic shape so future runtime tunables (budget fraction,
headroom, thresholds) ride on the same RPC without proto
churn.

**(c) Operator CLI:**

```bash
# Cluster-wide:
xrlenv eviction disable          # sets all nodes to eviction_enabled=False
xrlenv eviction enable
xrlenv eviction status           # prints current per-node state

# Per-node:
xrlenv eviction disable --node gcp-node-1
xrlenv eviction enable --node aws-node-3
```

Audit log entries on every toggle:
``cache.eviction_disabled`` / ``cache.eviction_enabled``
(token, node_id, reason from `--reason "freezing for debug"`).

### Behavior matrix when eviction is disabled

| Scenario | Result |
|---|---|
| New `ensure_present(X)`, X already present | OK (cache hit, no disk pressure path) |
| New `ensure_present(X)`, X missing, fits | OK (pull/build succeeds) |
| New `ensure_present(X)`, X missing, doesn't fit | **Fail fast**: `OutOfDiskAfterEviction` (eviction was the last resort; with eviction off, the image just doesn't get pulled). Rollout / build dispatch surfaces this as `image_pull_failed` with `reason: out_of_disk, eviction_disabled: true`. |
| `xrlenv build --plan` against full cluster | Plan-apply still bin-packs against budget; entries that don't fit are reported `deferred`. Builds that DO get dispatched fail-fast on out-of-disk if the live state moved between plan-apply and dispatch. |
| Re-enable eviction | Next `ensure_present` call runs `_evict_if_needed` normally; cluster recovers. |

### Use cases

- **Frozen-state debugging**: operator wants to inspect exactly
  which images are on a node without anything getting evicted
  mid-investigation.
- **Strict reproducibility runs**: pre-warm a specific image
  set, disable eviction so nothing churns during a
  reproducibility window, run the experiment, re-enable.
- **Disk-budget enforcement**: operator wants strict "you fit or
  you fail" semantics for a regulated workload — no
  ambiguity from eviction-eats-things-I-wanted-to-keep.

### Implementation footprint

- `ImageCacheManager`: ~20 LOC for the flag + setter + early-return.
- Spec-21 RPC: ~40 LOC proto + dispatcher + handler.
- CLI: ~80 LOC for `xrlenv eviction {enable,disable,status}`.
- Audit log entries: ~10 LOC.
- Tests: ~100 LOC.

Total: ~250 LOC. Folds into B.1's ~1 week scope.

## Convergence with existing `xrlenv images plan`

The existing `xrlenv images plan --refs <file> --eager-prefetch`
is registry-only prefetch with FFD bin-pack. The new
`xrlenv build --plan` is the strict superset (registry + git +
tarball, same bin-packer, same idempotency). **Deprecate
`xrlenv images plan` to a thin alias** that emits a deprecation
warning + forwards to `xrlenv build --plan` after auto-upgrading
the simple `image_ref` list to the new schema (all entries
synthesized as `type: registry`).

## Plug-in directory layout (new)

```
xrlenv_plugins/
├── harbor/                                  # existing — harbor cluster Environment
└── images_build/                            # NEW — per-benchmark plan generators
    ├── swebench_verified/
    │   ├── __init__.py
    │   ├── build_plan_gen.py                # 50-100 LOC generator
    │   └── build_plan.yaml                  # committed canonical plan (registry-only)
    ├── terminal_bench_2/
    │   ├── __init__.py
    │   ├── build_plan_gen.py
    │   └── build_plan.yaml                  # committed canonical (registry-only)
    └── seta_env/
        ├── __init__.py
        ├── build_plan_gen.py
        └── build_plan.yaml                  # committed canonical (git-source)
```

The committed `build_plan.yaml` files are the result of "operator
ran the generator once + committed the snapshot." Operators can
use them directly OR re-run the generator on a different
ref/subset. Each generator is registered via the existing
`xrlenv.benchmarks` entry-point group (or a parallel
`xrlenv.image_builders` group — TBD at impl time).

## build_plan.yaml schema (proposal)

```yaml
version: 1
entries:
  # Pure prefetch — image lives on a registry, just bin-pack + pull.
  - image_ref: alexgshaw/fix-git:20251031
    context_source:
      type: registry
    placement:
      preferred_home_count: 1
      size_hint_bytes: 1500000000

  # Build from a git repo.
  - image_ref: hb__seta-task-0
    context_source:
      type: git
      repo: https://github.com/camel-ai/seta-env
      ref: main
      subdir: Harbor-Dataset/0/environment
      dockerfile: Dockerfile
    placement:
      preferred_home_count: 1
      size_hint_bytes: 2000000000

  # Build from a local tarball (operator's CLI ships bytes to chosen nodes).
  - image_ref: my-bench/task-1:0.1
    context_source:
      type: tarball
      path: ./contexts/task-1.tar.gz   # operator-local; CLI loads + ships
    placement:
      preferred_home_count: 1
      size_hint_bytes: 800000000
```

## Slice plan

### B.1 — generic build primitive + operator plan path (~1 week)

**Schema + CLI**:

1. `xrlenv/control/build_plan.py` — pydantic schema for plan
   entries; loader + validation.
2. `xrlenv/cli/build.py` — `xrlenv build --plan <yaml>` subcommand:
   load + validate + bin-pack + dispatch.
3. `xrlenv build plan-gen --benchmark <name> [--repo <url>]
   [--ref <ref>] [--tasks <comma-list>]` — discovers per-benchmark
   generators via entry-point + emits a plan to stdout. Operator
   can pipe to a file + hand-edit before `xrlenv build --plan`.

**Spec-21**:

4. `xrlenv/api/proto/node_control.proto` — new
   `BuildImageCommand` (image_ref, context_source oneof,
   build_args, idempotency_key) + `BuildImageReply` (status:
   `done|skipped|failed`, error, duration_s, bytes_built). Generic
   — no harbor / tb2 in proto.

**Node side**:

5. `xrlenv/node/build_image.py` — three context-source handlers:
   - `git` — clone (cached at
     `~/.xrlenv/build-context-cache/<repo-hash>/<ref>/`) +
     `docker build -f <dockerfile> <subdir>`. Cache hit = no re-pull.
   - `tarball` — receive bytes, extract to tempdir, `docker build`.
   - `registry` — `docker pull` (reuses `EnsurePresentCommand`).
   - Idempotency: each handler checks `docker image inspect
     <image_ref>` first; returns `status: skipped` if already
     present.

**Bin-packing + idempotency at CLI layer**:

6. `xrlenv/control/build_coordinator.py`:
   - Pre-flight: query each node for `present` images; compute
     gap (`preferred_home_count - present_count`) per entry.
   - FFD bin-pack the gap entries against per-node free disk.
   - Dispatch `BuildImageCommand`s in parallel batches respecting
     per-node max-concurrent-builds (default 2).
   - Aggregate results; CLI prints a summary table.

**Per-benchmark generators**:

7. `xrlenv_plugins/images_build/swebench_verified/build_plan_gen.py`
   — emits a registry-only plan from the swebench dataset (one
   entry per instance: `image_ref: swebench/sweb.eval...`,
   `context_source: type: registry`). Reads HF cache.
8. `xrlenv_plugins/images_build/terminal_bench_2/build_plan_gen.py`
   — emits registry-only plan (89 entries, `alexgshaw/<task>:<rev>`).
   Reads `$XRLENV_BENCHMARK_CACHE`.
9. `xrlenv_plugins/benchmarks/seta/build_plan_gen.py` —
   emits git-source plan. For each task `N` under
   `Harbor-Dataset/`, emit `{image_ref: hb__seta-task-N,
   context_source: {type: git, repo: <url>, ref: <ref>,
   subdir: Harbor-Dataset/N/environment}}`.
10. Each `images_build/<bench>/build_plan.yaml` — committed
    canonical snapshot, runnable directly.

**Tests**:

11. Unit: schema validation, FFD bin-packing edge cases,
    idempotency-skip behavior, each context-source handler against
    a synthesized fixture, generator output matches committed
    canonical YAML.
12. Smoke: end-to-end build of 1 Harbor-Dataset task on a real
    cluster via `xrlenv build --plan
    xrlenv_plugins/benchmarks/seta/build_plan.yaml`.

Touches: ~7 new files in `xrlenv_plugins/images_build/`, ~4 new
files in `xrlenv/`, ~800 LOC + tests.

### B.2 — `Client.build_image()` + harbor on-demand (~3 days)

13. `Client.build_image(image_ref, context_source) ->
    BuildImageHandle` — async; returns a handle with `wait()` /
    `cancel()`. Internal: dispatches the same `BuildImageCommand`
    RPC as the operator CLI, picking a node via the same
    bin-packer.
14. `XrlenvHarborEnvironmentCluster.__init__` accepts:
    - `xrlenv_build_on_acquire: bool = False`
    - `xrlenv_build_git_repo: str | None = None`
    - `xrlenv_build_git_ref: str = "main"`
    - `xrlenv_build_git_subdir_template: str = "{environment_name}/environment"`
    On `ImageNotFound` from acquire (when `build_on_acquire=True`):
    - **Try local tarball first**: tar
      `<task_path>/environment/` if it exists and is non-empty;
      `Client.build_image(image_ref=hb__<env_name>,
      context_source=TarballContextSource(...))`.
    - **Fall back to git-source**: if `xrlenv_build_git_repo`
      is set, dispatch git-source build instead. Uses the
      cluster-side clone cache; second build of same ref is a
      no-op pull.
    - **Else**: fail with clear error pointing the operator at
      `populate-harbor-cache.sh` OR `xrlenv_build_git_repo`.
    On success: retry `acquire_container` once.
15. Server-side size cap on `BuildImageCommand` with `tarball`
    source: `xrlenv up --build-tarball-max-bytes <N>` (default
    100 MB). Reject with `BUILD_TARBALL_TOO_LARGE` error.
16. Per-token rate limit: `xrlenv up --build-rate-limit-per-hour
    <N>` (default 100,000 — effectively unlimited for now;
    framework in place for future tightening). Token must have
    `build` capability — issued via `xrlenv tokens issue consumer
    --capabilities build`.
17. Audit log entries: `build.image_started` (token, image_ref,
    source_type, node_id), `build.image_throttled` (token,
    reason), `build.image_completed` (status, duration_s,
    bytes_built).
18. Tests:
    - Mocked Client: harbor adapter calls `build_image` on
      `ImageNotFound`, retries acquire on success.
    - Smoke: end-to-end Harbor-Dataset task with
      `xrlenv_build_on_acquire=True` + `xrlenv_build_git_repo`.

Touches: ~3 new methods on `Client`, ~4 new fields on harbor
Environment, ~200 LOC + tests.

## Docs (B.1 + B.2 land together)

- New `docs/observability/capacity.md` section: "Pre-built vs
  build-on-acquire" — frames when operators want each.
- New `docs/technical_details/images/build_plan.md` deep-dive on
  the new schema + generator pattern.
- `docs/supported_benchmarks_and_harnesses/swe_bench.md` adds:
  "Pre-warming images: registry-pullable, run
  `xrlenv build --plan xrlenv_plugins/images_build/swebench_verified/build_plan.yaml`
  to bin-pack across nodes."
- `docs/supported_benchmarks_and_harnesses/terminal_bench_2.md`
  adds the same.
- New page `docs/supported_benchmarks_and_harnesses/seta_env.md` —
  Harbor-Dataset onboarding (build-required path).
- `docs/build_with_xrlenv/work_with_xrlenv_managed_containers/direct_api.md`
  adds a `Client.build_image()` recipe.

## Out of scope for P1.7.C.2

- docker-py drop-in's `client.images.build()` routing to the
  cluster primitive. Doable later as a thin wrapper; not in this
  slice.
- Multi-service compose tasks (the other half of the original
  P1.7.C.2 framing). 0/89 tb2 tasks use it; we'd be building for
  no current consumer. Defer until a real workload appears.
- Image signing / provenance.
- Build cache between nodes (each node has its own docker layer
  cache; cross-node sharing is phase-2-shaped).
