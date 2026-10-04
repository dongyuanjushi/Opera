# 15 — Image Cache Management

## Purpose

Per-node sandbox-image disk is finite. The total set of images a
training run might need can comfortably exceed it (SWE-bench's per-
instance images alone: hundreds × 1–4 GB). A naive
"`docker pull` everything, prune oldest" loop wastes bandwidth, fills
disk at unpredictable times, and forces blocking pulls during
rollouts.

This spec describes the **Image Cache Manager** — a per-node module
(co-located with the node agent, spec 04) that decides *which* images
live on the node at any moment, plus a small control-plane surface
that lets the trainer or operator say "I'll need these soon, get them
ready." It works the way a CPU cache + prefetcher works, applied to
multi-GB images.

This is also where **image-affinity scheduling** lives (control-plane
side): when image presence varies by node, the scheduler prefers
nodes that already have the right image rather than forcing a fresh
pull.

## The problem, concretely

A SWE-bench-Lite training run on a node with 200 GB free:

- 300 instance images × 2 GB avg = **600 GB**, 3× the node's disk.
- A training iteration touches ~64 instances. Cold pulls during the
  iteration block sandbox creation and bottleneck the GPU.
- Iteration N+1 will touch a *different* 64 instances. We want them
  warm by the time iteration N+1 starts.
- Iteration N+10 won't touch the iteration-N set anymore. Holding on
  to those images wastes disk.

Naive solutions break: LRU alone doesn't pre-fetch; pre-fetch alone
doesn't evict; per-template pre-pull doesn't scale to per-instance
images. We need a small system that does both, driven by signals
from the trainer.

## Architecture

```
trainer SDK                            control plane
  client.warmup(templates,             ┌─────────────────────────────┐
                instance_ids,          │  CacheCoordinator           │
                horizon=K)  ─────────► │   • cluster image map       │
                                       │   • image-affinity scoring  │
                                       │   • directive fan-out       │
                                       └─────┬───────────────────────┘
                                             │ ImageDirective per node
                                             ▼
                          ┌─────────────────────────────────────┐
node agent                │  ImageCacheManager (per node)       │
                          │   ├── working_set                   │
                          │   ├── priority_tiers                │
                          │   ├── prefetch_queue (IO-budgeted)  │
                          │   └── eviction_loop                 │
                          └─────────────┬───────────────────────┘
                                        │ docker pull / docker rmi
                                        ▼
                                  local image store
```

## Working-set definition

At any moment, an image on a node is in one of five tiers:

1. **In-use** — at least one running sandbox depends on it. *Never
   evict* (would crash live rollouts).
2. **Pinned** — operator-pinned (shared base images, frequently-used
   templates). *Never evict.*
3. **Soon-needed** — explicitly named in the active warmup directive
   (the trainer announced it's coming up in the next K iterations).
   Evict only as a last resort.
4. **Recently-used** — used within the last `recent_window`
   (default 30 min) but no current rollout. Evictable when needed.
5. **Cold** — neither in-use nor recently-used nor announced.
   First to evict.

## Sources of signal

The cache manager builds the working set from three inputs:

1. **Trainer warmup directive** (the strongest signal; phase 1):
   ```python
   await client.warmup(
       templates=["swebench-base"],
       instance_ids=upcoming_iter_instances,   # next K iters
       horizon_iters=3,
       deadline_s=120,                          # have everything ready in 2 min
   )
   ```
   The control plane fans this out as `ImageDirective` messages to
   each node, biased by the scheduler's expected per-node assignment
   so each node only pre-fetches what it's likely to run.

2. **Recent rollout history** (phase 0): a rolling window of which
   `(template, instance_id)` pairs ran in the last 30 minutes per
   node. Drives the "recently-used" tier and a baseline LRU.

3. **Operator pin list** (phase 0): config file
   (`/etc/xrlenv/image-pins.yaml`) listing images that must always
   be present. Default contents include the shared base images
   (spec 06) and any template the operator marks as
   `image.pin: true`. Phase 1 adds: every consolidated runtime named
   in an active `plan.yaml` (spec 16) is auto-pinned for the duration
   of that plan being active — they are the hottest images on the
   cluster once a consolidated training run is underway.

## Priority and eviction

Each cached image has a score:

```
score = w_inuse  · 1{in-use}                    # ∞ if running
      + w_pin    · 1{pinned}                    # very high
      + w_soon   · soon_needed_weight(image)    # depends on horizon
      + w_recent · exp(-Δt / recent_halflife)   # decay since last use
```

When free disk drops below `evict_threshold` (default 15 GB free or
10% of disk, whichever larger), the manager evicts in ascending order
of score until free disk reaches `evict_target` (25 GB / 15%). It
never selects an in-use or pinned image.

Eviction is `docker image rm`. Layers shared with other resident
images aren't actually freed by Docker until the last referencing
image is removed — this is fine; we measure free disk, not freed
layer count, so the loop converges naturally.

Eviction is **best-effort but synchronous w.r.t. the next pull**: a
pull request that would push disk over the threshold first runs
eviction, then proceeds, then aborts with `OutOfDiskAfterEviction` if
even after eviction the image won't fit (operator misconfiguration:
pinned set is too large for the disk).

## Pre-fetch (warmup)

When the cache manager receives a directive to warm an image set:

1. Filter to images not already present.
2. Sort by directive priority × estimated pull time (smaller / more
   urgent first) so progress is visible early.
3. Push into `prefetch_queue` with a configurable concurrency cap
   (default 2 concurrent pulls, IO-budgeted so concurrent rollouts
   don't see disk-bandwidth starvation).
4. Pulls run async; the directive's `deadline_s` is honored — anything
   not pulled by then is reported back to the control plane as
   `warmup.partial`.
5. Pulls also run during idle time (no in-flight rollouts on the
   node) at higher concurrency.

The manager exposes:

```python
class ImageCacheManager(Protocol):
    def status(self) -> NodeImageStatus:
        """Per-image: tier, size, last-used, in-use refcount, score."""

    async def directive(self, d: ImageDirective) -> None:
        """Accept a warmup or pin directive from the control plane."""

    async def ensure_present(self, image: str, deadline_s: float) -> None:
        """Block until image is local; trigger eviction if needed."""

    def report(self) -> NodeImageReport:
        """Periodic snapshot pushed up in the heartbeat for cluster view."""
```

## Prewarm without modifying trainer source code

A core constraint: Slime's `generate_rollout_async`, verl's
`RolloutWorker.generate`, and any other upstream trainer code cannot
be modified to call `client.warmup(...)`. Prewarm has to fire from
inside the platform / adapter layer, transparent to upstream. We
support this with **three layered strategies**, none of which
requires an upstream change.

### Layer 1 — implicit warmup at `batch_rollout` entry (always on)

When the trainer SDK receives a `client.batch_rollout(template, ...,
inits=[...])` call, it derives a per-rollout **cache key** from each
init payload and **fires `warmup(template, instance_ids=[...])`
fire-and-forget** concurrently with submitting the rollout starts.
The directive flows to the cache manager on the relevant nodes
while the scheduler is still placing sandboxes; image pulls overlap
with scheduling. By the time `backend.create` runs, the image is
local (or actively downloading on a node the scheduler can target).

Cache-key derivation is template-driven, not `task_key`-driven:

- For templates with an `instances:` resolver (Pattern A, spec 06):
  the SDK calls `resolver.cache_key(init)` (default implementation
  returns `init["instance_id"] ?? init["task_id"]`). The resolver
  is the only component that knows which init field maps to a
  distinct image/asset.
- For templates without per-instance images: the SDK skips Layer 1
  — the template-level image is already pre-pulled (`pre_pull:
  true`) or pinned, so there is nothing per-rollout to warm.
- `task_key` is **not used** as a cache key. It is the algorithm-
  driven fairness/anti-affinity tag (spec 02), opaque to the
  cache. It often collides with `instance_id` for SWE-bench
  (where the same string serves both roles) but the equality is
  incidental, not contractual. Custom trainers that hash prompts
  into `task_key` would otherwise pollute the cache key space.

Slime and verl adapters pass `inits=[...]` to `batch_rollout`
exactly as today; Layer 1 fires automatically against the
template's resolver, with zero upstream code change.

The implicit warmup uses a short `deadline_s` (default 60) and is
silent on failure — if the image already exists or the warmup misses
its deadline, the scheduler still works (it falls back to a blocking
pull on whichever node it places). Layer 1 is purely an optimization,
never a correctness dependency.

### Layer 2 — adapter-side data-source peek (lookahead)

Layer 1 hides pulls behind scheduling latency, but cannot prefetch
*the iteration after next*. For that, the platform needs to know
which prompts are coming up — which Slime's `data_source` and verl's
`Dataset` already expose, just not directly to the platform.

The trainer adapter (XRLEnv code, not upstream code) **wraps the
data source before handing it to the trainer**:

```python
# xrlenv/adapters/slime.py
class WarmingDataSource:
    """Slime sees this; calls .get_samples like normal. Side effect:
    peeks ahead K iterations, fires warmup for upcoming task_ids."""

    def __init__(self, inner, sdk_client, template, lookahead_iters=3):
        self._inner = inner
        self._client = sdk_client
        self._template = template
        self._k = lookahead_iters
        self._fired = set()

    def get_samples(self, n):
        groups = self._inner.get_samples(n)
        for offset in range(1, self._k + 1):
            try:
                preview = self._inner.peek(n, offset=offset)
                ids = [_extract_task_id(s) for g in preview for s in g
                       if _extract_task_id(s) not in self._fired]
                if ids:
                    asyncio.create_task(self._client.warmup(
                        template=self._template,
                        instance_ids=ids,
                        deadline_s=300,
                    ))
                    self._fired.update(ids)
            except (NotImplementedError, DataSourceNotPeekable):
                break
        return groups
```

The adapter swaps `args.data_source` for the wrapper at init.
Slime's `generate_rollout_async` calls `data_source.get_samples(n)`
exactly as before; the wrapper's `get_samples` first warms next-K's
images via `asyncio.create_task` (fire-and-forget), then returns the
current batch's groups untouched. **Slime never observes anything
different.**

This works whenever the upstream data source is **enumerable and
pure** — `dataset.samples[i]` deterministic, peekable by index.
Slime's `slime.utils.data.Dataset` and verl's standard datasets
satisfy this. For stateful / curriculum-driven sources that depend
on prior outcomes, the wrapper falls back gracefully (Layer 1 still
fires).

The same pattern applies to verl: the verl adapter wraps the
incoming `DataProto` prompt iterator (or its underlying dataset
object) and peeks ahead in the same fire-and-forget way.

### Layer 3 — control-plane Markov prediction (phase 2)

For trainers that pass opaque task data the adapter can't peek (a
custom user trainer with a closed-over generator function, no
`task_keys` set), the control plane records `task_key` annotations
across recent rollouts and fits a simple frequency / Markov model:
"after task_key X is requested, often Y comes within K iterations."
Speculative prefetches fire from the model's predictions, IO-budgeted
on idle node-disk bandwidth.

Lower hit rate than Layers 1 or 2; useful as a backstop for the
opaque-data-source case. Phase 2.

### How the layers compose

```
trainer code (UNCHANGED — Slime / verl / custom)
       │
       │  data_source.get_samples()  ──►  WarmingDataSource peeks   (Layer 2, ahead K iters)
       ▼
trainer adapter (XRLEnv code)
       │
       ▼
client.batch_rollout(inits=[...])  ─►  SDK derives cache_keys via resolver,
                                       fires warmup(instance_ids=[...]) (Layer 1, this iter)
       │
       ▼
control plane (records task_key history) ──► Markov prefetch        (Layer 3, phase 2)
       │
       ▼
ImageCacheManager (per node):
       pulls in priority order, IO-budgeted, evicts cold to make room
       │
       ▼
sandbox creation finds image already local
```

- **Layer 1** is the floor — always fires for any caller using
  `batch_rollout` against a template with an `instances:` resolver.
  Catches "warm just before use." Zero adapter or upstream change.
- **Layer 2** is the lookahead — fires when the adapter can peek the
  data source. Lifts hit rate from "concurrent with creation" to
  "ready before creation." Adapter-only change; upstream untouched.
- **Layer 3** is the backstop for opaque sources. Phase 2.

For Slime and verl as the primary phase-1 trainer adapters: Layers 1
and 2 together give effectively-perfect prefetch in normal
conditions, with no upstream code modification required.

## Image-affinity scheduling (control-plane side)

The control plane keeps a `(node, image) → present?` map populated
from each node's `report`. When the scheduler places a rollout, it
adds an *image-affinity* term to the existing fits-and-largest-
remaining algorithm (spec 03):

```
node_score = capacity_term(node) + α · image_present_term(node, template)
```

Where `image_present_term` is +1 if the node already has the exact
template (and per-instance image, when applicable) cached, 0 otherwise.
`α` is tuned so capacity always wins over affinity when both nodes
have the image, and affinity wins when capacity is comparable.

This avoids the pathological case where a fresh pull is forced on a
node that doesn't have the image, while another node with capacity
*and* the image cached sits idle.

## Operator surface

```
$ xrlenv images
NODE     CACHED   FREE     IN-USE  SOON  RECENT  COLD  EVICTED-24h
gcp-1    142 GB   58 GB    12      40    23      5     17
aws-1     87 GB   13 GB    8       40    18      0     24

$ xrlenv warmup --templates swebench-base \
                --instances $(cat next_iter.txt) \
                --horizon 3 --deadline 120

$ xrlenv images pin --templates terminal-base
$ xrlenv images unpin --templates terminal-base
```

The admin panel (spec 13) gets a new **Images** view rendering the
same data plus a per-node tier histogram.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: per-node cache manager with LRU eviction, operator
  pin list, `xrlenv warmup` CLI for explicit operator-driven warming
  (no trainer-API integration yet), `xrlenv images` reporting.
  Phase-0 disk-pressure handling is "evict cold by LRU, hold the
  line at `min_free_disk_bytes`."
- **Phase 1**: `client.warmup(...)` SDK API and `ImageDirective`
  fan-out from the control plane; image-affinity term in the
  scheduler; admin-panel Images view.
- **Phase 2**: predictive prefetch using rolling iteration history
  (no trainer announcement needed, fewer surprises); cluster-wide
  layer dedup via shared storage (spec 06 SWE-bench cache pattern)
  becomes a first-class managed feature; image-aware autoscale
  (when an autoscaler adds a node, it boots with a pre-warmed set
  derived from the cluster's hot tier).

## Lazy / on-demand image loading (phase 1)

For large per-instance images (SWE-bench instances at 1–4 GB,
research-env images that include model checkpoints), "full pull
before sandbox start" is the dominant cold-start cost. Lazy
loading addresses this directly: the sandbox starts as soon as
metadata is local; only file blocks the running process actually
reads are fetched, on demand.

Two storage formats, one per backend:

| Backend | Format | What it does |
|---|---|---|
| Docker | **stargz-snapshotter** (eStargz seekable tar.gz) | Image layers in eStargz format mounted via FUSE; metadata local at mount, data chunks fetched lazily over HTTP from the registry |
| CubeSandbox | **overlaybd** | Block-level lazy-loaded disk format; base layer on shared storage (NFS/EFS or 3FS-equivalent), per-sandbox COW on local disk |

Both compose with the existing cache manager:

- The pull replacement is a **mount**, not a download. The cache
  manager's "is this image local?" check now means "is the image's
  metadata + already-fetched chunks local?" — sandbox start succeeds
  as soon as metadata is mounted.
- Eviction works the same way: the eviction unit is the entire
  lazy-loaded layer set; whether 5% or 95% of its blocks are warm,
  it's still one image to LRU.
- Affinity scheduling, warmup directives, and pin lists all apply
  to lazy-loaded images identically to fully-pulled ones.

### Capability advertisement

Each node's hardware probe (spec 04) reports
`supports_lazy_load: bool` plus `lazy_load_format` (one of
`"estargz"`, `"overlaybd"`, or `null`). The cache manager and
the scheduler consult this when honoring template `lazy_load`
modes.

### Three lazy-load modes

Templates declare an explicit mode rather than a boolean:

```yaml
image:
  ref: "..."
  pre_pull: true
  lazy_load: preferred         # disabled | preferred | required
```

| Mode | Behavior on a lazy-capable node | Behavior on a non-capable node |
|---|---|---|
| `disabled` | classical full pull | classical full pull |
| `preferred` (default for big-image templates) | lazy mount; full pull as fallback if mount fails | classical full pull; emit `lazy_fallback` event |
| `required` | lazy mount; abort placement if mount fails | scheduler refuses placement on this node (`backend_missing:lazy_load`) |

Fallback events surface in `xrlenv_image_cache_hit_total{kind="lazy_fallback"}`
and as `image.lazy_fallback` in the audit log. The admin panel
flags nodes whose lazy-fallback rate exceeds 10% so operators can
investigate registry / snapshotter health rather than discover the
issue via slow rollouts.

### Why fallback matters

Earlier drafts said lazy-load "falls back gracefully" without
naming the modes. The problem: silent fallback can turn a
seconds-scale start into a minutes-scale pull, blowing rollout
deadlines and confusing capacity planning. The three-mode design
forces operators to make the trade-off explicit: `required` for
templates whose deadlines assume lazy-load, `preferred` for
templates where falling back is acceptable, `disabled` for
templates that have hit lazy-load bugs.

**Big win** for SWE-bench-scale workloads: a 4 GB instance image's
sandbox can start in seconds instead of waiting for the full
download. Combined with the prewarm layers (above), most rollouts
see effectively zero cold-start image cost.

## Assets (qcow2, model files, dataset shards)

In addition to Docker images, the cache manager tracks **assets** —
arbitrary blobs declared in a template's `assets:` block (spec 06,
"Pattern B"). Examples: OSWorld's 15 GB Ubuntu qcow2, model
checkpoints used by an in-sandbox judge, large dataset shards bind-
mounted into a research env.

Assets share the same data structures as images:
- Same five priority tiers (in-use, pinned, soon-needed,
  recently-used, cold).
- Same eviction logic (LRU among low-score, never evict in-use).
- Same `client.warmup(...)` path: warmup directives can name asset
  ids alongside template ids.
- Same operator pin list (extended to include asset ids).

What differs:
- **Fetcher**: HTTP / S3 / GCS download with resume + sha256 check,
  instead of `docker pull`. Configurable per-source (a registered
  `AssetFetcher` per scheme: `https://`, `s3://`, `gs://`, `hf://`).
- **Storage shape**: the asset is a file (or extracted directory) at
  the template-declared `extract_to`. `extract: zip | tar | tar.gz |
  none` controls post-download handling.
- **Eviction unit**: the file or extracted directory, not a Docker
  layer set.
- **Mount-time hook**: the backend resolves asset-id references in
  the template's `MountSpec`s into concrete `host_path` values when
  assembling the `create` call.

`xrlenv assets` operator CLI mirrors `xrlenv images`. The admin-panel
`/images` view gains an asset tab in phase 1 alongside the existing
image tab, with a combined free-disk number per node. (P1.2.c shipped
this view briefly under the path `/blobs`; it was renamed back to
`/images` in 2026-05 after operator feedback.)

This means OSWorld's qcow2 download flow becomes a first-class
warmup directive instead of a one-off Python script. A 200-node
cluster running OSWorld training spends one HTTP fetch per node
(coordinated by the warmup directive's per-node fan-out), not 200
fresh downloads at first sandbox start.

## On-demand build into a scratch registry (phase 1)

When a template ships a **Dockerfile instead of a prebuilt image**
(spec 06's `image_build:` block), the cache manager's on-demand build
hook (`ImageCacheManager.ensure_present` → builder lookup) builds it
**once for the fleet** and distributes it by registry pull from a
dedicated **scratch registry** — a third registry alongside the
pull-through mirror and the private registry, but **quota-bounded and
GC'd** so build-on-demand can never grow the private registry
unboundedly. The scratch image is content-addressed over its build
inputs (so it is built exactly once and drift-free across nodes),
digest-pinned for the run (invariant 4), and evicted from node disk by
the same LRU as any other pulled image. A user who wants the image to
outlive GC supplies their own durable registry.

> **Design:** `notes/scratch-registry-build-on-demand.md`. The scratch
> registry is a distribution + lifetime concern layered on top of this
> spec's cache/affinity/eviction machinery, which it reuses verbatim.

## Non-goals

- Not a replacement for an in-cluster registry mirror (spec 09).
  Mirror = cluster-wide cold-pull dedup; this manager = per-node hot
  set. They compose.
- Not a content-addressed layer store. We don't try to be smarter than
  Docker's layer dedup; we just decide *which whole images* live on
  each node and let Docker handle layer sharing within them. (The
  scratch-registry build-on-demand path content-addresses a *build-input
  tag* — a deterministic image name for build dedup — which is a
  distinct concern from a content-addressed layer store; see
  `notes/scratch-registry-build-on-demand.md`.)
- For assets, not a CDN — single-source download per node. If you
  need cluster-wide asset distribution, point the asset source at
  a shared NFS/EFS mount (spec 06's "shared SWE-bench instance image
  cache" pattern generalizes to any large blob).
