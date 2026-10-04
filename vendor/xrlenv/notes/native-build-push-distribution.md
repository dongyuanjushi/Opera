# Native distributed build+push (`xrlenv build push`)

**Goal.** Replace the Slurm `build_and_push_images.sh` (stale hardcoded nodelist,
checkout, registry — drift-prone) with a control-plane-orchestrated build+push:
the CP shards a build plan across the **currently-connected** node agents over
the spec-21 bidi stream; each node builds a plan entry from its Dockerfile and
pushes it to the private registry; the CP aggregates the pushed digests into a
registry-source plan the fleet then pulls. No sbatch, no nodelist, no drift.

## Why it's mostly built already

| Layer | Reused as-is |
|---|---|
| Plan format (`BuildEntry` = image_ref + git/tarball source + size hint) | `control/build_plan.py` |
| CP fan-out (per-image dispatch, concurrency semaphore, disk-aware pacing, inventory-skip, per-image outcome aggregation) | `control/build_coordinator.py::apply` |
| Node build **+ push + resolve-digest**, singleflight, build-once-for-fleet (registry HEAD skip) | `node/source_builder.py::build_and_push` (scratch feature) |
| CP→node dispatch over the bidi stream | `BuildImageCommand` |

The only gap: the coordinator dispatches build-local / registry-pull; it does
not push built images to the shared registry. `build_and_push` closes it.

## Design — keep `build apply` untouched, add a parallel push path

**Wire (slice 1).**
- `BuildImageCommand.push` (bool, tag 8) — build-and-push vs local-tag build.
- `BuildImageReply.repo_digest` (string, tag 5) — the pushed `<repo>@sha256:...`.
- Node `_exec_build_image`: when `push`, call `build_and_push(check_registry_first=True)`
  (resumable/overlap-safe) instead of `build`; return `repo_digest`.
- Transport: **new** `build_and_push_image(...)` → `(status, error, repo_digest)`
  (shares source-lowering with `build_image`, which stays a 2-tuple). The
  registry-qualified `image_ref` already encodes the push target.

**Coordinator (slice 2).**
- New injected hook `build_push_fn: BuildPushFn | None` (parallel to `build_image_fn`);
  returns `(status, error, repo_digest)`.
- `apply(push=False)`: when `push`, git/tarball entries dispatch via `build_push_fn`
  and the pushed digest lands in `BuildOutcome`. `push=False` = today's behavior,
  byte-identical. Reuses `_await_disk_headroom` + the in-flight semaphore.

**CLI (slice 3).** `xrlenv build push --plan <plan.yaml> --registry <host:port>`:
load the plan, registry-qualify each `image_ref`, submit to the running CP,
stream per-image progress, and (optionally) emit a registry-source plan of the
resolved digests for pinning. Modeled on `cmd_build_apply`.

**Retire (slice 4).** Delete `slurm_scripts/build_and_push_images.sh`; point its
docs/refs at `xrlenv build push`. `scripts/build_and_push_images.py` stays only
as the `LocalSource` build-host fallback (documented as legacy).

## Invariants preserved
- Build-once fleet-wide: `build_and_push`'s registry HEAD skip + the coordinator
  inventory provider → re-runs are cheap, interrupted runs resume, overlapping
  dispatch never double-pushes (same guarantees the sbatch version got from FSx).
- Digest pinning (invariant 4): the resolved `repo_digest` is what the fleet pins.

Each slice is qa-audited before the next. Lands on `feat/scratch-registry`.
