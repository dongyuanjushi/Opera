# Benchmark Image-Cache Improvements — Implementation Plan

Original plan date: 2026-06-04.

> ## STATUS — as built (read this first)
>
> This document is the original design plan. **Phase A has since SHIPPED and the
> as-built details differ from the plan prose below** — for the live, authoritative
> picture use **`deploy/registry/README.md`** and **`notes/phase-a-results.md`**,
> not the Phase A sections here.
>
> - **Phase A — SHIPPED.** Pull-through mirror is live. As-built deltas vs the plan
>   text below:
>   - **`registry:3`** (distribution 3.x), not `registry:2` — so `proxy.ttl`
>     (90-day retention) is honored from `config-mirror.yml`.
>   - port **`:5010`**, not `:5000` (an unexplained service held `:5000`).
>   - workers are pointed at the mirror **once** via
>     `scripts/configure_docker_registry.sh` (a `daemon.json` merge); `refresh.sh`
>     deliberately does NOT touch `daemon.json`.
>   - warming is **`scripts/warm_images.py`** — pulls manifests+blobs **through
>     the mirror's registry API** (no `docker pull`, no local extraction, never
>     bypasses the mirror); idempotent (skips already-cached blobs); the
>     `warm_images.sh` wrapper was removed. The "`docker pull` / `xrlenv images
>     warm`" wording below is superseded.
>   - upstream auth comes from repo **`.env` `DOCKERHUB_USER`/`DOCKERHUB_TOKEN`**,
>     not a hand-made `/etc/xrlenv/registry-proxy.env`.
> - **Phase B — deferred (skip for now).** The proxy + full warm already give
>   lazy Docker-Hub independence + config-driven 90-day retention; revisit only if
>   guaranteed completeness / push-based reproducibility is needed. Phase B is the
>   *operator* bulk-build-and-push path (`:5011`). The *self-service, user-Dockerfile*
>   successor — build-on-demand into a quota-bounded, GC'd scratch registry (`:5012`)
>   — is designed in **`notes/scratch-registry-build-on-demand.md`** (also fixes the
>   unbounded `XRLENV_PRIVATE_REGISTRY_STORAGE` growth called out in Part 2's risks).
> - **Phase C — pending the full 731 eval metrics** (`scripts/eval_metrics_sampler.sh`).
>   Decide size-aware/repo-affinity scheduling from real per-node disk data; the
>   mirror + I/O-aware throttle may already have tamed the heavy-tail pressure.

## 0. Problem recap (measured on HyperPod, 128-job SWE-bench-pro wave)

- Workload = ~128 **distinct** per-instance images (`jefzda/sweap-images:<repo>-<sha>`),
  **0 redundant re-pulls** → unique-image streaming, ~0 image-level cache hits.
- Heavy-tailed sizes: 16 images ≥10 GB, `protonmail.webclients` at **15–20 GB**.
- Pulls go to **Docker Hub** (`registry-mirrors: None`); disk 95–100 % full;
  eviction maxed; disk-bound pull ceiling self-throttles to ~2 → running stuck ~43.
- Root issue: **eviction is catastrophic** because a re-pull = cold Docker Hub
  fetch, and the heavy tail crushes the per-node pull ceiling and disk.

## 1. Key codebase facts the plan relies on (grounded)

| Fact | Where | Consequence |
|---|---|---|
| Node pulls via `docker-py` `client.images.pull(image)` → through **dockerd** | `xrlenv/backends/docker.py:534` | `/etc/docker/daemon.json` `registry-mirrors` applies **transparently — zero code** |
| Image refs are used **verbatim**, no rewrite/prefix anywhere | `image_cache.py:724`, `build_coordinator.py:1096`, `docker.py:278` | A Docker-Hub mirror needs no ref changes; a *private* registry does |
| **No registry push step exists** ("build-once-push glue gap" confirmed) | grep: zero `push` sites | Private-registry option must add push glue |
| Scheduler **already** scores image-affinity | `scheduler.py:184-249` `weighted_sum_score` = `2/3·resource + 1/3·image_affinity` (present=1.0, preferred_home=0.5, else 0.0) | Part 3 = *extend* scoring, not build from scratch |
| Image presence queried per-placement (fan-out RPC) | `image_presence.py:31-93` | Already feeds the scheduler |
| Per-node image report carries name+size+tier (no digest) | `image_cache.py:369-392`, `report()` 1048-1082 | Size-load + repo-affinity can be computed control-plane-side from the **existing report**, no new RPC |
| Disk-pressure gate excludes nodes <5 GB / <5 % free | `scheduler.py:515` | Heavy-tail co-scheduling already partially guarded |
| `preferred_home` from FFD build-plan bin-packing | `image_planner.plan_opportunistic_placements`, `state.find_registered_preferred_home` | Pre-pull-only-assigned is already the model |
| Digest pinning exists at template-register | `template_catalog._maybe_pin_image:512-615` (`image_pin_mode="registry_digest"`) | Reproducibility hook already present |
| daemon.json is **operator-merged**, not autogen | `scripts/set_docker_data_root.sh:34-50` | Reuse this Python-merge pattern for `registry-mirrors` |
| **dockerd data-root can't live on FSx/Lustre** (overlay) | `set_docker_data_root.sh:57-69` rejects Lustre/NFS | But a **registry bind-mount** to FSx is plain file I/O → fine |
| Spec 09 already designs an optional pull-through mirror; spec 15 says mirror + per-node cache **compose** | `specs/09:193-228`, `specs/15:503-505` | We are implementing existing design intent |

**The pivotal simplification:** because `jefzda/sweap-images` lives on Docker Hub
(`docker.io`), and Docker's `registry-mirrors` apply *only* to `docker.io`, a
pull-through mirror catches the benchmark images **with no ref rewriting and no
code changes**. Cache misses fall back to Docker Hub automatically.

## 2. Collocation decision (answer to "can the mirror live on the control plane?")

**Yes — recommended for phase A, with two hard constraints.** The control-plane
box (ml.m7i.48xlarge = 192 vCPU, ~768 GiB RAM, ~37.5 Gbps NIC) is ~idle for the
control plane and trivially absorbs a registry. Constraints:

1. **Registry storage → FSx, NOT the 500 GB EBS.** The EBS is already the
   collocated work-node's docker data-root (the thing that hit 100 %). Registry
   blobs go on an FSx bind-mount; the registry uses the `filesystem` storage
   driver = plain file writes, which Lustre handles (this is *not* the
   overlay-on-Lustre problem that blocks data-root).
2. **Run it in pull-through *proxy* mode first** so that if the box is busy or the
   registry is down, dockerd's `registry-mirrors` **falls back to Docker Hub
   automatically** — the mirror is a bounded-risk accelerator, not a hard SPOF.

Caveats to accept: all worker pulls funnel through that one NIC reading from FSx
(still ≫ Docker Hub for a 3–4 node cluster); a *private* (non-proxy) registry
holding retagged refs **is** a hard SPOF (named refs have no fallback) — mitigate
with 2 replicas or by keeping proxy-mode as the catch-all. Note: something
already answers on `:5000` (catalog empty, not a docker container on the worker)
— **audit/decommission that before binding the port.**

---

## Part 1 — FSx-backed pull-through registry mirror

### Where it runs
- A `registry:2` container on the **control-plane box**, `--restart=always` (or a
  systemd unit `xrlenv-registry.service` for parity with `xrlenv-node`).
- **Proxy mode** pointed at Docker Hub:
  `REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io`.
- Authenticated upstream with a **rotated** Docker Hub PAT
  (`REGISTRY_PROXY_USERNAME/PASSWORD`) to dodge anonymous rate limits. *(The
  previously-leaked PAT `dckr_pat_NdV-…` must be rotated and only ever placed
  here / in a root-only env file — never committed.)*

### FSx mount + storage backend
- Bind-mount an FSx path as the registry root:
  `-v /fsx/registry/proxy:/var/lib/registry`,
  `REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/var/lib/registry`.
- Ensure the FSx (Lustre) mount has the **`flock`** option (registry blob-upload
  locking). Content is sha256-addressed (write-temp-then-rename) → safe for a
  **single** writer.
- Size FSx for the full compressed set: ~731 images, compressed ≈ on-disk ÷ ~2.5
  (registry-probe showed ~1 GB compressed vs ~2.4 GB on-disk avg). Budget
  **~1.5× ≈ 1.2–1.5 TB** to hold the set + churn headroom.

### Worker-node configuration
- Extend the existing merge helper (`scripts/set_docker_data_root.sh` pattern)
  into `scripts/configure_docker_registry.sh` that merges into
  `/etc/docker/daemon.json`:
  ```json
  {
    "data-root": "/opt/sagemaker/docker/data-root",
    "registry-mirrors": ["http://<cp-ip>:5000"],
    "insecure-registries": ["<cp-ip>:5000"]
  }
  ```
- Wire it into `deploy/bootstrap-common.sh` / `xrlenv/cli/bootstrap.py` (a new
  `XRLENV_REGISTRY_MIRROR` knob → written to daemon.json at bootstrap), and add a
  `deploy/refresh.sh` step so existing nodes pick it up. `systemctl restart docker`
  after the merge. **No node-agent code changes** (pulls already go via dockerd).

### TLS / insecure handling
- **Phase A (now):** HTTP on the trusted VPC. `registry-mirrors` entry as
  `http://…` **requires** the host:port also in `insecure-registries` (Docker
  refuses plain-HTTP mirrors otherwise). Matches the existing `:5000` posture.
- **Phase B (hardening, aligns with CLAUDE.md "mTLS hardening, phase 1"):** issue
  an internal TLS cert (reuse the mTLS CA), serve registry over HTTPS, distribute
  the CA to each node at `/etc/docker/certs.d/<host:port>/ca.crt`, drop the
  `insecure-registries` entry.

### Validation that pulls hit the mirror
1. **Registry access log**: tail the registry container; a node pull shows
   `GET /v2/jefzda/sweap-images/blobs/sha256:…` on the mirror.
2. **Negative test (definitive)**: on one node, firewall-block egress to
   `registry-1.docker.io`, then pull an already-cached image → still succeeds ⇒
   served by the mirror. (Uncached image then fails ⇒ proves the path.)
3. **FSx growth**: `/fsx/registry/proxy` blob dir grows on first pulls, flat on
   repeats.
4. **Latency delta**: time a cold-then-evicted re-pull pre/post mirror; expect
   re-pull p95 to drop from Docker-Hub-bound (53–245 s) toward LAN/FSx-bound.
5. **Node-agent log**: `image_cache: pulling …` still fires (unchanged), but the
   underlying transfer is now LAN.

### Risks (Part 1)
- **Availability**: proxy-down ⇒ auto-fallback to Docker Hub (bounded). Acceptable.
- **GC**: proxy caches accumulate; distribution proxy has limited retention
  control. Mitigate by sizing FSx for the full set and *not* GC'ing aggressively;
  if needed, periodic offline GC in registry read-only mode.
- **Concurrency / FSx**: single writer + `flock` = safe. Do **not** run 2 proxy
  replicas over one FSx path (GC/scheduler races). For HA use 2 replicas with
  *separate* storage paths (loses dedup) or a fronting load balancer with sticky
  storage — defer.
- **Lustre semantics**: ensure `flock`; watch metadata-op latency under many tiny
  manifest files (acceptable at this scale).

---

## Part 2 — Full benchmark image set in a local FSx-backed registry

### Pull-through only, private retagged, or both? → **Both, phased**
- **Part 1 proxy** already makes repeated runs Docker-Hub-independent *lazily*
  (after first pull of each image). For most needs this is enough.
- Add a **private (writable) registry** only when you need **guaranteed
  completeness before a run, retention control, and digest-locked
  reproducibility** decoupled from Docker Hub. The proxy can't be `docker push`ed
  to; a private registry can.
- They compose: **proxy = catch-all for `docker.io` (base images, cache misses);
  private registry = curated, pinned benchmark set.** Run as two registries on the
  CP box (`:5000` proxy, `:5001` private) or accept proxy-only for phase A.

### Populate the registry with the full set
- **Proxy (lazy/warm)**: a one-shot `xrlenv images warm --plan <plan.yaml>` that
  iterates the plan's `image_ref`s and `docker pull`s each **through the mirror**
  once (bounded concurrency), warming the FSx blob store. Zero new infra.
- **Private (explicit)**: add the missing **push glue** — a
  `xrlenv images mirror --plan <plan.yaml> --to <cp-ip>:5001` that for each entry:
  `pull (via proxy) → retag jefzda/sweap-images:<tag> → <cp-ip>:5001/sweap/<tag>
  → push by digest`. Run from the CP box (LAN). Idempotent (skip if manifest
  already present). This is the durable answer to "build-once-push gap".

### Reference the private set from benchmark runs
- The injection point is **build-plan generation** (`build_plan_gen.py:226`,
  `image_ref = f"{repo}:{tag}"`) — add `--registry-prefix <cp-ip>:5001/sweap`
  so the emitted plan references the private registry. Keep a vanilla
  (Docker-Hub-ref) plan for portability; the on-cluster plan uses the prefix.
- Add `<cp-ip>:5001` to nodes' `insecure-registries` (named refs don't use
  `registry-mirrors`). Cache misses for these refs have **no Docker-Hub
  fallback** → completeness check (below) is mandatory before a run.

### Verify completeness before a run
- `xrlenv images verify --plan <plan.yaml> --registry <cp-ip>:5001`: HEAD
  `GET /v2/<name>/manifests/<tag>` for every entry; assert all present **and**
  digest matches the plan's recorded digest. Fail-fast with the missing list.

### Tags / digests for reproducibility
- At populate time record each image's **Docker-Hub digest**; push to the private
  registry **by digest** (digest is content-addressed, preserved across
  registries). Emit plans that pin `…/sweap/<tag>@sha256:<digest>`.
- Reuse the existing `template_catalog._maybe_pin_image` (`image_pin_mode=
  registry_digest`) so the catalog resolves+pins against the **private** registry
  endpoint — honoring invariant 4 (manifests immutable for a run).

### Avoid storing everything on every worker node
- This is the central win: the **full set lives once on FSx** (served by the
  registry). Worker nodes keep only their **hot set** (per-node LRU,
  `ImageCacheManager`). The FFD planner assigns each image a `preferred_home`
  (1–2 nodes) and pre-pulls only there; eviction is now cheap (LAN re-pull). No
  node ever needs the full set.

### Risks (Part 2)
- **Private-registry SPOF** (named refs, no fallback) → run the completeness
  check pre-run; consider 2 replicas; or stay proxy-only.
- **Push-glue drift** vs upstream (CLAUDE.md "don't reinvent benchmark wheels"):
  the mirror tool only **retags+pushes bytes** (no parsing/grading), so no
  upstream-contract risk.
- **GC on the private registry** must run read-only (stop pushes) — schedule
  during quiet windows; size FSx to avoid frequent GC.

---

## Part 3 — Repo-affinity and size-aware scheduling

> Sequencing note: **Parts 1–2 remove the *latency* pain** (re-pull = LAN). Part 3
> remains valuable for **node disk pressure** (the 500 GB EBS data-root that hit
> 100 %) and **layer-reuse byte savings** — independent of pull source. Lower
> urgency than the mirror; do it after.

### Strategy
1. **Spread the ≥10 GB heavy tail.** Add a per-node **large-image-load** signal,
   computed control-plane-side from the **existing** `NodeImageReport.images`
   (name+size already reported — no new RPC). Penalize nodes whose summed
   large-image footprint is high.
2. **Avoid co-scheduling several 15–20 GB images.** Same load signal as a *soft*
   score penalty plus a *hard* guard: refuse placement if it would push a node's
   large-image footprint past a fraction of its free disk (extends the existing
   disk-pressure gate at `scheduler.py:515`).
3. **Group same-repo images for base-layer reuse.** Derive a **repo key** from the
   tag's project prefix (e.g. `internetarchive.openlibrary`). Add a soft affinity
   tier: if a node already hosts an image with the same repo key, its base layers
   are cached → cheaper pull. Compute from the per-node report (control-plane
   side, no RPC).
4. **Pre-pull only assigned images per node.** Already the model: FFD
   `preferred_home` + `ensure_present`. Make the FFD planner **heavy-tail-aware**
   (place ≥10 GB images round-robin/spread *first*, then cluster same-repo, then
   FFD the remainder) so assignments themselves spread the giants and cluster
   repos.
5. **Keep node disk under control.** Hard guard (2) + the existing adaptive
   eviction; the mirror makes eviction safe, so the planner can keep per-node hot
   sets smaller.

### Code/config changes (file:line)
- `scheduler.py:130-160` `PlacementFeatures`: add `image_size_bytes: int|None`,
  `node_large_image_load: float` (0–1), `repo_affinity: float` (0–1).
- `scheduler.py:184-249` `weighted_sum_score`:
  `score = w_R·R + w_I·I + w_repo·RA − w_L·L`, defaults e.g.
  `w_R=0.5, w_I=0.25, w_repo=0.1, w_L=0.15` (tunable via constructor/env;
  **set `w_repo=w_L=0` to exactly reproduce today's behavior** = rollback switch).
- New control-plane aggregation: maintain per-node `{repo_key→present, large-image
  footprint}` from the periodic `report()` (already received) — populate
  `PlacementFeatures` at `place()` time. Slight staleness is fine for soft scoring.
- Hard guard: extend the disk gate (`scheduler.py:515`) with
  "would this image + node's large-image load exceed `XRLENV_MAX_LARGE_LOAD_FRAC`
  of free disk?".
- `image_planner.plan_opportunistic_placements`: heavy-tail-first + repo-cluster
  ordering before FFD.
- `build_plan_gen.py`: emit a `repo_key` label per entry (cheap; it already parses
  the tag) so the planner/scheduler don't re-derive it.
- New env knobs: `XRLENV_SCHED_REPO_AFFINITY_WEIGHT`,
  `XRLENV_SCHED_LARGE_LOAD_WEIGHT`, `XRLENV_LARGE_IMAGE_GB` (default 10),
  `XRLENV_MAX_LARGE_LOAD_FRAC`.

### Expected impact
- Heavy-tail no longer collides on one node → fewer node-disk 100 % events; more
  even free-disk across nodes; higher sustained pull ceiling (it's
  `free/(largest×safety)`, so spreading the 20 GB images raises per-node free).
- Same-repo clustering → measurable **pulled-bytes reduction** (base layer pulled
  once per node instead of per instance).
- Combined with the mirror: p95 create drops and running-count climbs closer to
  the run-slot capacity instead of the pull-bound ~43 equilibrium.

### Validation metrics
- Per-node max concurrent ≥10 GB images (target: ≤1–2).
- Cross-node free-disk variance (target: down).
- Total pulled bytes per wave (target: down vs baseline via repo clustering).
- docker-run p95 and running-count equilibrium (target: up).
- No increase in `errors/timeouts` or disk-pressure-skip rate.

### Rollback (Part 3)
- All new weights default-off-able: `w_repo=w_L=0` ⇒ identical to current scorer.
- Hard guard behind `XRLENV_MAX_LARGE_LOAD_FRAC` (set high/∞ to disable).
- FFD heavy-tail ordering behind a flag; revert to current FFD.
- Pure-additive `PlacementFeatures` fields; no schema migration.

---

## 3. Phased rollout

> Superseded by the STATUS banner at the top — Phase A shipped as registry:3 /
> `:5010` / `warm_images.py`; see `deploy/registry/README.md`. The original
> wording below is kept as design history.

- **Phase A (now, zero/low code, highest ROI):** stand up the **proxy mirror** on
  the CP box (FSx storage), merge `registry-mirrors` into node daemon.json,
  restart docker, validate via negative test. Re-pulls become LAN; eviction stops
  being catastrophic. *Then* `xrlenv images warm --plan` to pre-fill FSx.
- **Phase B (durable/reproducible):** add push glue (`xrlenv images mirror`) +
  private registry on `:5001`, `xrlenv images verify`, build-plan
  `--registry-prefix`, digest pinning. Optional if proxy suffices.
- **Phase C (efficiency):** size-aware + repo-affinity scheduling and
  heavy-tail-aware FFD. Tune weights from the validation metrics.

## 4. Cross-cutting risks summary
- **Availability**: proxy degrades gracefully to Docker Hub; private registry does
  not (gate with completeness check / replicas).
- **GC**: proxy auto / private read-only-mode offline; size FSx to avoid it.
- **Concurrency**: single registry writer per FSx path + `flock`; never 2 replicas
  on one path.
- **FSx semantics**: Lustre OK for registry `filesystem` driver (plain files);
  *not* OK for dockerd data-root (unchanged — stays on EBS).

## 5. Open decisions for review
1. Proxy-only (Phase A) sufficient, or do we need the private registry + push glue
   (Phase B) for reproducibility now?
2. One collocated registry instance (accept SPOF for private refs) vs two
   replicas?
3. HTTP+insecure now vs TLS from the start?
4. Do Part 3 now, or defer until the mirror's impact is measured (recommended:
   measure first)?
5. FSx path + size budget to allocate for the registry store.
